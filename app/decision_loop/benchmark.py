"""Rolling-origin production benchmark with independently dated outcome windows."""
from hashlib import sha256
from itertools import combinations
import json
from statistics import mean
from time import perf_counter

import numpy as np
import ortools

from app.decision_loop import learning, production_case as case

POLICIES = ('current', 'stat', 'winner', 'scenario')
EXPERIMENT = 'spo_plus'
BOOTSTRAPS = 5000
BLOCK_WEEKS = 3


def fingerprint(value):
    return sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def without_timing(value):
    if isinstance(value, dict):
        return {key: without_timing(item) for key, item in value.items() if key != 'elapsed_s'}
    if isinstance(value, list):
        return [without_timing(item) for item in value]
    return value


def window(case_inputs, week, training_weights=None, *, training=False):
    supply, docket, history, parameters, weights = case_inputs
    start = perf_counter()
    inputs = case.dated_inputs(history, docket, week)
    input_seconds = perf_counter() - start
    confirmed = inputs['confirmed_units']

    def run(totals, fixed=None):
        return case.solve(case.prepare(supply, docket, parameters, confirmed, totals), weights, fixed)

    planning = {name: run(totals) for name, totals in inputs['candidate_totals'].items()}
    # Common random numbers. Cache duplicate schedules/draws within this window only.
    settled = {}

    def dispatch(totals, schedule):
        key = (tuple(totals), fingerprint(schedule))
        if key not in settled:
            settled[key] = run(totals, schedule)
        return settled[key]

    scenarios = {name: [dispatch(draw, result['schedule']) for draw in inputs['draw_totals']]
                 if result['schedule'] is not None else []
                 for name, result in planning.items()}
    features = {}
    for name, outcomes in scenarios.items():
        if outcomes and all(outcome['cost_sar'] is not None for outcome in outcomes):
            costs = [outcome['cost_sar'] for outcome in outcomes]
            features[name] = [mean(costs), case.cvar(costs), planning[name]['cost_sar']]

    def pick(weights):
        return min(features, key=lambda name: np.dot(features[name], weights), default=None)

    choices = dict(zip(POLICIES, (*POLICIES[:3], pick(learning.INITIAL_WEIGHTS))))
    if training_weights is not None:
        choices[EXPERIMENT] = pick(training_weights)

    # Outcomes first enter the computation here. The oracle can also change fabrication.
    actual = [history[week, sku] for sku in case.PRODUCTS]
    if any(observed < booked for observed, booked in zip(actual, confirmed)):
        raise ValueError('Outcome totals cannot be below the frozen bookings in this fixture')
    oracle = run(actual)
    evaluated_names = planning if training else dict.fromkeys(name for name in choices.values() if name)
    realized = {name: dispatch(actual, planning[name]['schedule'])
                for name in evaluated_names if planning[name]['schedule'] is not None}

    # Budget the same full pool for every policy. Attribute latency to the
    # work it needs for selection, apart from common evaluation overhead.
    pool_seconds = sum(row['elapsed_s'] for row in planning.values())
    unique_draws = {(tuple(draw), fingerprint(planning[name]['schedule']))
                    for name, outcomes in scenarios.items() if outcomes for draw in inputs['draw_totals']}
    pool_seconds += sum(settled[key]['elapsed_s'] for key in sorted(unique_draws))
    results = {}
    for policy, name in choices.items():
        outcome = realized.get(name)
        incumbent = outcome['cost_sar'] if outcome else None
        oracle_cost, oracle_bound = oracle['cost_sar'], oracle['bound_sar']
        lower = max(0, outcome['bound_sar'] - oracle_cost) if incumbent is not None and oracle_cost is not None else None
        upper = max(0, incumbent - oracle_bound) if incumbent is not None and oracle_bound is not None else None
        exact = (incumbent is not None and outcome['status'] == oracle['status'] == 'OPTIMAL')
        results[policy] = {
            'candidate': name, 'realized': outcome,
            'elapsed_s': (input_seconds + pool_seconds if policy in ('scenario', EXPERIMENT)
                          else (0 if policy == 'current' else input_seconds) + planning[name]['elapsed_s']),
            'planning_gap_pct': planning[name]['gap_pct'] if name else None,
            'scenario_cvar90_sar': features[name][1] if name in features else None,
            'regret_sar': lower if exact else None, 'regret_bounds_sar': [lower, upper],
        }
    return {
        'inputs': inputs, 'actual_totals': actual,
        'oracle': oracle, 'planning': planning, 'draws': scenarios, 'feature_rows': features,
        'realized_candidates': realized, 'policies': results, 'unique_draw_solves': len(unique_draws),
    }


def training_example(row):
    names = list(row['feature_rows'])
    if len(names) < 2:
        return None
    if any(row['realized_candidates'][name]['status'] != 'OPTIMAL' for name in names):
        return None
    return (
        np.array([row['feature_rows'][name] for name in names]) / learning.COST_NORMALIZER,
        np.array([row['realized_candidates'][name]['cost_sar'] for name in names]),
    )


def summarize(rows):
    admissible = [row for row in rows if row['realized'] and row['realized']['cost_sar'] is not None]
    observed = [row['realized'] for row in admissible]
    costs = [row['cost_sar'] for row in observed]

    def average(values):
        present = [value for value in values if value is not None]
        return mean(present) if present else None

    return {
        'windows': len(rows), 'eligible_windows': len(observed), 'feasible_pct': 100 * len(observed) / len(rows),
        'cost_sar': average(costs), 'mean_regret_sar': average(row['regret_sar'] for row in rows),
        'mean_regret_bounds_sar': [average(row['regret_bounds_sar'][i] for row in rows) for i in (0, 1)],
        'exact_regret_windows': sum(row['regret_sar'] is not None for row in rows),
        'service_pct': average(row['service_pct'] for row in observed),
        'credit_breach_sar': max((row['credit_breach_sar'] for row in observed), default=None),
        'peak_overdraft_sar': max((row['peak_overdraft_sar'] for row in observed), default=None),
        'cvar90_realized_sar': case.cvar(costs) if costs else None,
        'mean_scenario_cvar90_sar': average(row['scenario_cvar90_sar'] for row in rows),
        'latency_p95_s': float(np.quantile([row['elapsed_s'] for row in rows], 0.95)),
        'mean_planning_gap_pct': average(row['planning_gap_pct'] for row in rows),
        'mean_dispatch_gap_pct': average(row['gap_pct'] for row in observed),
    }


def paired(rows, reference, challenger):
    differences = [None if (a := row['policies'][reference]['regret_sar']) is None
                   or (b := row['policies'][challenger]['regret_sar']) is None else a - b
                   for row in rows]
    valid = [value for value in differences if value is not None]
    interval = None
    # Keep adjacency intact. An incomplete series still reports its paired
    # differences and excludes itself from interval estimation and gating.
    if len(valid) == len(rows) and len(valid) >= 2 * BLOCK_WEEKS:
        rng = np.random.default_rng(case.SEED)
        n = len(valid)
        starts = rng.integers(n, size=(BOOTSTRAPS, (n + BLOCK_WEEKS - 1) // BLOCK_WEEKS))
        samples = (starts[:, :, None] + np.arange(BLOCK_WEEKS)) % n
        simulated = np.array(valid)[samples.reshape(BOOTSTRAPS, -1)[:, :n]].mean(axis=1)
        interval = np.quantile(simulated, [0.025, 0.975]).tolist()
    return {'reference': reference, 'challenger': challenger, 'paired_windows': len(valid),
            'mean_improvement_sar': mean(valid) if valid else None, 'ci95_sar': interval}


def run_benchmark():
    source = case.demo_case()
    supply, docket, history, parameters, weights = source
    manifest = {
        'source': 'app/data/demo/production.json', 'source_sha256': fingerprint([
            supply, docket, sorted((week, sku, qty) for (week, sku), qty in history.items()),
        ]),
        'supply': supply, 'static_docket': docket,
        'parameters': parameters, 'weights': weights, 'protocol': 'h3-production-v1',
        'engine': f'OR-Tools {ortools.__version__}', 'numpy': np.__version__,
        'seed': case.SEED, 'workers': case.WORKERS,
        'solver_wall_cap_s': case.WALL_SECONDS, 'solver_deterministic_cap': case.DETERMINISTIC_SECONDS,
        'solver_calls_cap_per_policy_window': len(case.CANDIDATES) * (1 + case.DRAW_COUNT),
        'draws_per_window': case.DRAW_COUNT, 'cvar_level': case.TAIL_LEVEL,
        'bootstrap_replicates': BOOTSTRAPS, 'bootstrap_block_weeks': BLOCK_WEEKS,
        'training_window': [case.date_for(case.FIRST_TRAIN), case.date_for(case.FIRST_TEST)],
        'test_window': [case.date_for(case.FIRST_TEST), case.date_for(case.LAST_TEST + 1)],
    }
    train_weeks = range(case.FIRST_TRAIN, case.FIRST_TEST)
    test_weeks = range(case.FIRST_TEST, case.LAST_TEST + 1)

    def fit():
        windows = [window(source, week, training=True) for week in train_weeks]
        examples = [example for row in windows if (example := training_example(row)) is not None]
        return learning.fit_spo_plus(examples), windows, len(examples)

    coefficients, training, example_count = fit()
    rows = [window(source, week, coefficients) for week in test_weeks]
    again_coefficients, again_training, _ = fit()
    again = [window(source, week, again_coefficients) for week in test_weeks]
    for primary, repeated in zip([*training, *rows], [*again_training, *again]):
        primary['input_sha256'] = fingerprint([manifest, primary['inputs']])
        repeated['input_sha256'] = fingerprint([manifest, repeated['inputs']])
        first_result = fingerprint(without_timing(primary))
        repeat_result = fingerprint(without_timing(repeated))
        primary['replay'] = {'matched': first_result == repeat_result,
                             'result_sha256': first_result, 'replay_sha256': repeat_result}
    names = (*POLICIES, EXPERIMENT)
    summary = {name: summarize([row['policies'][name] for row in rows]) for name in names}
    comparisons = [paired(rows, a, b) for a, b in combinations(names, 2)]
    baseline = min(POLICIES, key=lambda name: (
        -summary[name]['exact_regret_windows'],
        summary[name]['mean_regret_sar'] if summary[name]['mean_regret_sar'] is not None else float('inf'),
    ))
    replay = {
        'training_matched': sum(row['replay']['matched'] for row in training), 'training_windows': len(training),
        'evaluation_matched': sum(row['replay']['matched'] for row in rows), 'evaluation_windows': len(rows),
        'coefficients_matched': np.array_equal(coefficients, again_coefficients),
    }
    gate = learning.assess(summary[EXPERIMENT], summary[baseline], paired(rows, baseline, EXPERIMENT),
                           expected_windows=len(rows),
                           replay_ok=(replay['training_matched'] == len(training)
                                      and replay['evaluation_matched'] == len(rows)
                                      and replay['coefficients_matched']),
                           all_eligible=(example_count == len(training)
                                         and all(summary[name]['exact_regret_windows'] == len(rows)
                                                 for name in names)))
    return {
        'fixture': 'production_demo_l1_s1_s2', 'evidence': 'exploratory_demo',
        'manifest': manifest, 'summary': summary, 'comparisons': comparisons, 'replay': replay, 'windows': rows,
        'experiment': gate | {'method': EXPERIMENT, 'strongest_baseline': baseline,
                             'features': learning.FEATURES, 'coefficients': coefficients.tolist(),
                             'training_examples': example_count,
                             'training_replay': [{'week': row['inputs']['week'], 'input_sha256': row['input_sha256'],
                                                  **row['replay']} for row in training]},
    }
