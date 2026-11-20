"""Production experiment orchestration. The existing optimize endpoint stays independent."""
from time import perf_counter

from app.decision_loop.candidates import SEED, SOLVE_SECONDS, WORKERS, prefix, solve_candidate
from app.decision_loop.evaluation import assess, dispatch, measure
from app.decision_loop.frontier import TOLERANCE, production_frontier
from app.decision_loop.rolling import rolling
from app.decision_loop.scenarios import Demand, check_observations, draw_paths, fingerprint, reduced_sample
from app.optimization.budget import SolveBudget
from app.optimization.common import OptimizationInputError
from app.optimization.production import baseline, prepare

PATHS = 64
SHORT_PATHS = 16
TIME_LIMIT = 5.0
MAX_HORIZON = 35
MAX_ORDERS = 100


def select(candidates, key):
    return min((row for row in candidates if row[key]['eligible']),
               key=lambda row: (row[key]['score'], row['id']), default=None)


def experiment(dataset, parameters, weights, policy, *, hard_credit=False):
    started = perf_counter()
    if parameters['horizon_days'] > MAX_HORIZON or len(dataset['orders']) > MAX_ORDERS:
        raise OptimizationInputError('التجربة متاحة لأفق ٣٥ يوم و١٠٠ طلب مؤكد بحد أقصى.')
    base = prepare(dataset, parameters)
    demand = Demand.from_problem(base, policy.forecast_uplift)
    realized = check_observations(demand, policy.observations or [])
    paths = draw_paths(demand, PATHS, SEED)
    reduced = reduced_sample(paths, policy.short_paths)
    typical = reduced_sample(paths, 3)
    full_probs = [1 / len(paths)] * len(paths)
    scales = {
        'cost_sar': max(1, sum(base.unshipped_penalty(order) for order in demand.orders.values())),
        'service_loss_pct': 100,
        'peak_credit_sar': max(base.credit, 1),
    }
    budget = SolveBudget(TIME_LIMIT)
    candidates, attempts = [], []
    hint = baseline(base)
    strategies = [('central', 'cost', {}), ('low_demand', 'cost', paths[typical[0][0]]),
                  ('high_demand', 'cost', paths[typical[-1][0]]),
                  ('service', 'service', {}), ('liquidity', 'cash', {})]
    by_version = {}
    for name, objective, quantities in strategies:
        problem = demand.problem(base, quantities)
        meta, plan = solve_candidate(problem, demand.release, weights, objective,
                                     SOLVE_SECONDS if budget.remaining() >= SOLVE_SECONDS else 0,
                                     hint=hint, hard_credit=hard_credit,
                                     different_from=hint if name in {'low_demand', 'high_demand'} else None)
        attempt = {'id': name, 'objective_kind': objective, 'solver': meta,
                   'scenario_fingerprint': fingerprint(quantities)}
        attempts.append(attempt)
        if plan is None:
            continue
        version = fingerprint(plan)
        if version in by_version:
            attempt['duplicates'] = by_version[version]
            continue
        if name == 'central':
            hint = plan
        by_version[version] = name
        # Same candidate pool and draws for the reduced/full comparison.
        rows = []
        for path in paths:
            case = demand.problem(base, path)
            rows.append(measure(case, dispatch(case, plan)))
        full = assess(rows, full_probs, policy, scales, hard_credit)
        short = assess([rows[index] for index, _ in reduced], [weight for _, weight in reduced],
                       policy, scales, hard_credit)
        candidates.append(attempt | {'version': version, 'plan': plan, 'assessment': full,
                                     'reduced_assessment': short})
    chosen, short_chosen = (select(candidates, key) for key in ('assessment', 'reduced_assessment'))
    reference = next((row for row in candidates if row['id'] == 'central'), None)
    points = production_frontier(candidates, reference or chosen)
    for point in points:
        point['version'] = next(row['version'] for row in candidates if row['id'] == point['candidate_id'])
    replan = None
    if policy.rolling and chosen:
        actual = policy.observations is not None
        if not actual:
            realized = paths[typical[1][0]]
        replan = rolling(base, demand, chosen['plan'], realized, weights,
                         hard_credit=hard_credit, budget=budget, actual=actual, seconds=SOLVE_SECONDS)
        replan['input'] = 'observations' if actual else 'synthetic_median'
        replan['input_fingerprint'] = fingerprint(realized)
        through_day = max((stage['through_day'] for stage in replan['stages']
                           if 'through_day' in stage), default=0)
        context = fingerprint({'dataset': dataset, 'parameters': parameters, 'weights': weights,
                               'hard_credit': hard_credit, 'settings': policy.model_dump(exclude={
                                   'observations', 'expected_commitment', 'model_id', 'dataset_id',
                               })})

        def commitment_at(day):
            return fingerprint({
                'context': context, 'through_day': day, 'plan': prefix(replan['committed'], day + 1),
                'observed': {key: value for key, value in realized.items() if demand.release[key] <= day},
            })

        replan['commitment'] = {'through_day': through_day, 'fingerprint': commitment_at(through_day)}
        if policy.expected_commitment:
            expected = policy.expected_commitment
            if expected.through_day > through_day or commitment_at(expected.through_day) != expected.fingerprint:
                raise OptimizationInputError('إعادة التشغيل ما طابقتش الالتزام السابق. راجع نسخة القرار.')
        if replan['metrics'] is not None:
            case = demand.problem(base, realized)
            replan['fixed_plan_metrics'] = measure(case, dispatch(case, chosen['plan']))

    if policy.expected_commitment and replan is None:
        raise OptimizationInputError('الالتزام السابق محتاج إعادة تشغيل على نفس نسخة القرار.')

    return {
        'selected_id': chosen['id'] if chosen else None, 'reference_id': (reference or chosen or {}).get('id'),
        'reason': 'lowest_eligible_normalized_tail_score' if chosen else 'no_eligible_candidate',
        'candidates': candidates, 'attempts': attempts, 'frontier': points,
        'frontier_tolerance': TOLERANCE, 'frontier_scope': 'verified_candidate_pool',
        'comparison': {
            'reduced_selected_id': short_chosen['id'] if short_chosen else None,
            'same_selection': (chosen or {}).get('id') == (short_chosen or {}).get('id'),
            'full_score_difference': short_chosen['assessment']['score'] - chosen['assessment']['score']
            if chosen and short_chosen else None,
        },
        'scenario_set': {
            'fingerprint': fingerprint(paths), 'full_count': len(paths), 'reduced_count': len(reduced),
            'representatives': [{'index': index, 'probability': mass} for index, mass in reduced],
            'method': 'persistent_shared_demand_shock', 'source': 'synthetic_incremental_demand',
            'forecast_orders': [{**demand.orders[key], 'release_day': demand.release[key]} for key in demand.forecast],
        },
        'policy': policy.model_dump(exclude={'observations', 'expected_commitment'}),
        'credit_policy': 'hard' if hard_credit else 'diagnostic',
        'scales': scales, 'rolling': replan,
        'settings': {'seed': SEED, 'workers': WORKERS, 'solve_time_limit_s': SOLVE_SECONDS,
                     'search_budget_s': TIME_LIMIT, 'horizon_days': base.horizon,
                     'plan_start_date': parameters['plan_start_date']},
        'wall_s': perf_counter() - started,
    }
