"""Exploratory paired replay on production demo data. Run with python -m qa.hybrid_h4_measure."""
from collections import Counter
import json
from time import perf_counter

import numpy as np

from app.decision_loop.api import ProductionExperiment
from app.decision_loop.candidates import SOLVE_SECONDS, prefix, solve_candidate
from app.decision_loop.evaluation import cvar, dispatch, measure
from app.decision_loop.rolling import rolling
from app.decision_loop.scenarios import Demand, draw_paths
from app.decision_loop.service import TIME_LIMIT, experiment
from app.ingestion.loader import load_demo
from app.optimization.budget import SolveBudget
from app.optimization.production import baseline, prepare, solve
from app.templates_registry import load_templates

COUNT = 24
HOLDOUT_SEED = 709
BOOTSTRAP_SEED = 304


def p95(values):
    return float(np.quantile(values, 0.95))


def paired(before, after):
    differences = np.array(before) - np.array(after)
    indices = np.random.default_rng(BOOTSTRAP_SEED).integers(0, len(differences), (10_000, len(differences)))
    return {'saving_sar': float(differences.mean()),
            'interval_sar': list(np.quantile(differences[indices].mean(axis=1), [0.025, 0.975]))}


def summarize(rows):
    probabilities = [1 / len(rows)] * len(rows)
    return {key: {'mean': sum(row[key] for row in rows) / len(rows),
                  'cvar90': cvar([row[key] for row in rows], probabilities, 0.9)}
            for key in ('cost_sar', 'service_loss_pct', 'peak_credit_sar', 'credit_breach_sar')} | {
        'physical_violations': sum(not row['physical_ok'] for row in rows),
        'credit_violations': sum(bool(row['credit_breach_sar']) for row in rows),
        'qualified': sum(row['physical_ok'] and not row['credit_breach_sar'] for row in rows),
        'max_credit_breach_sar': max(row['credit_breach_sar'] for row in rows),
    }


def run():
    dataset = load_demo('production')
    config = load_templates()['production'].model_defaults()
    parameters, weights = config['parameters'], config['weights']
    base = prepare(dataset, parameters)
    demand = Demand.from_problem(base, 0.10)
    train_trials = [(0.5, 16)] * 8 + [(0, 16), (1, 16), (0.5, 8), (0.5, 32), (0.5, 64)]
    trials = []
    fit_times = []
    default_run = None
    for weight, count in train_trials:
        fitted = experiment(dataset, parameters, weights, ProductionExperiment(
            tail_weight=weight, short_paths=count,
        ), hard_credit=True)
        entry = {'weight': weight, 'count': count, 'full': fitted['selected_id']} | fitted['comparison']
        if (weight, count) == (0.5, 16):
            default_run = fitted
            fit_times.append(fitted['wall_s'])
        else:
            trials.append(entry)
    chosen_id = default_run['selected_id']
    candidates = {row['id']: row for row in default_run['candidates']}
    selected = candidates[chosen_id]['plan']
    central = candidates['central']['plan']
    legacy = solve(dataset, parameters, weights, SOLVE_SECONDS, deterministic=True)['optimized']['plan']
    holdout = draw_paths(demand, COUNT, HOLDOUT_SEED)
    rows = {key: [] for key in ('legacy', 'central', 'scenario', 'rolling')}
    stages = freezes = matching = 0
    partial = Counter()
    oracles = Counter()
    p2_times, best_known, bounds = [], [], []
    per_path = []
    for idx, realized in enumerate(holdout):
        problem = demand.problem(base, realized)
        fixed = [measure(problem, dispatch(problem, plan)) for plan in (legacy, central, selected)]
        started = perf_counter()
        adapted = rolling(base, demand, selected, realized, weights, hard_credit=True, budget=SolveBudget(TIME_LIMIT))
        p2_times.append(perf_counter() - started)
        partial[adapted['status']] += 1
        stages += len(adapted['stages'])
        matching += sum(stage.get('ledger_verified', False) for stage in adapted['stages'])
        freezes += prefix(adapted['committed'], 1) != prefix(selected, 1)
        if adapted['metrics'] is None:
            continue
        replay = [*fixed, adapted['metrics']]
        for key, result in zip(rows, replay):
            rows[key].append(result)
        solver, hindsight = solve_candidate(problem, demand.release, weights, 'cost', SOLVE_SECONDS,
                                            hint=adapted['committed'], hard_credit=True)
        oracles[solver['status']] += 1
        valid = [row for row in replay if row['physical_ok'] and not row['credit_breach_sar']]
        if hindsight:
            valid.append(measure(problem, hindsight))
        best_known.append(min(row['cost_sar'] for row in valid))
        bounds.append(solver['bound'] / 70_000 if solver['bound'] is not None else 0)
        per_path.append({
            'path': idx, 'solver_status': solver['status'], 'bound_sar': bounds[-1], 'best_known_sar': best_known[-1],
            'metrics': {key: {metric: out[metric] for metric in (
                'cost_sar', 'service_loss_pct', 'peak_credit_sar', 'credit_breach_sar', 'physical_ok',
            )} for key, out in zip(rows, replay)},
        })
    costs = {key: np.array([row['cost_sar'] for row in result]) for key, result in rows.items()}
    gaps = np.array(best_known) - np.array(bounds)
    comparison = default_run['comparison']
    return {
        'holdout_seed': HOLDOUT_SEED, 'bootstrap_seed': BOOTSTRAP_SEED,
        'settings': default_run['settings'], 'holdout_count': COUNT, 'paired_count': len(per_path),
        'selection': chosen_id, 'comparison': comparison, 'sensitivity': trials,
        'chosen_short_minus_full': {
            key: candidates[chosen_id]['reduced_assessment'][key]['cost_sar']
            - candidates[chosen_id]['assessment'][key]['cost_sar'] for key in ('mean', 'cvar')
        },
        'p95_fit_s': p95(fit_times), 'p95_rolling_only_s': p95(p2_times),
        'observed_fit_runs_s': fit_times, 'p95_fit_plus_rolling_s': p95([
            fit_times[i % len(fit_times)] + secs for i, secs in enumerate(p2_times)
        ]),
        'stages': stages, 'verified_stages': matching, 'frozen_changes': freezes, 'rolling_states': partial,
        'oracles': oracles, 'avg_bound_gap_sar': float(gaps.mean()),
        'regret_bounds_sar': {key: [float(np.mean(values - best_known)), float(np.mean(values - bounds))]
                              for key, values in costs.items()},
        'p1_vs_central': paired(costs['central'], costs['scenario']),
        'p1_vs_legacy': paired(costs['legacy'], costs['scenario']),
        'p2_vs_p1': paired(costs['scenario'], costs['rolling']),
        'policies': {key: summarize(result) for key, result in rows.items()},
        'frontier': default_run['frontier'], 'attempts': default_run['attempts'],
        'correlation': float(np.corrcoef([row[demand.forecast[0]] for row in holdout],
                                         [row[demand.forecast[-1]] for row in holdout])[0, 1]),
        'paths': per_path,
    }


if __name__ == '__main__':
    print(json.dumps(run(), indent=2, ensure_ascii=False))
