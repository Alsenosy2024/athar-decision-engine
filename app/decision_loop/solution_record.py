"""Record the solver response separately from the candidate and its approval verdict."""
import json

from app.decision_loop.eligibility import approved_inputs, assess, constraint_policy
from app.optimization.common import CONTRACTING_POLICY_TIME_RATIO, DETERMINISTIC_TIME_RATIO, SOLVER_SEED


def solve_options(template, policy):
    options = {'deterministic': True}
    if template == 'contracting':
        options['credit_hard'] = policy['credit_limit']['kind'] == 'hard'
    return options


def solution_record(template, dataset, model, policy, result, *, dataset_id, inputs_approved, time_limit,
                    legacy=False, contract=None):
    solver = result['solver']
    objective, bound = solver.get('search_objective', solver.get('objective')), solver.get('bound')
    gap = None if objective is None or bound is None else abs(objective - bound) / max(1, abs(objective))
    parameters = model['parameters']
    cost_key = 'total_cost' if template == 'production' else 'total'
    optimized = result['optimized'].get('kpis', {})
    before = result['baseline'].get('kpis', {}).get(cost_key)
    after = optimized.get(cost_key)
    ratio = CONTRACTING_POLICY_TIME_RATIO if template == 'contracting' and contract is None else DETERMINISTIC_TIME_RATIO
    approval_policy = contract['approval_policy'] if contract else None
    return {
        'snapshot': {
            'dataset_id': dataset_id, 'model_id': model['id'], 'model_version': model['version'],
            'effective_weights': model['weights'], 'inputs_approved': inputs_approved,
            'seed': None if legacy else SOLVER_SEED, 'workers': None if legacy else 1, 'time_limit_s': time_limit,
            'deterministic_time_s': None if legacy else time_limit * ratio,
        },
        'solver': solver | {'relative_gap': gap},
        'policy': policy,
        'cost_delta': None if before is None or after is None else round(after - before, 6),
        'delayed_projects': sum(weeks > 0 for weeks in optimized.get('late', {}).values()),
        'limits': {'credit_limit': parameters['credit_limit']},
        'optimized': assess(template, dataset, parameters, policy, result['optimized']['plan'], solver,
                            inputs_approved=inputs_approved, approval_policy=approval_policy),
        'baseline': assess(template, dataset, parameters, policy, result['baseline']['plan'], solver,
                           inputs_approved=inputs_approved, heuristic=True, approval_policy=approval_policy),
    }


def stored_record(row, dataset, model, result):
    """Reconstruct eligibility for historical runs without fabricating solver evidence."""
    if result.get('solution_record'):
        return result['solution_record']
    variant = 'scenario' if row['kind'] == 'whatif' else 'optimized'
    solver = (result[variant].get('solver') if variant == 'scenario' else result.get('solver')) or {}
    contract = result.get('decision_contract')
    if row['kind'] == 'whatif':
        contract = json.loads(row['base_result_json']).get('decision_contract')
    return solution_record(
        row['template'], dataset, model, constraint_policy(row['template'], model),
        {'solver': solver | {'status': row['status']}, 'optimized': result[variant],
         'baseline': result.get('baseline', {'plan': {}})},
        dataset_id=row['dataset_id'], inputs_approved=approved_inputs(json.loads(row['validation_json'])),
        time_limit=solver.get('time_limit_s'), legacy=True, contract=contract,
    )


def changed_input(template, original_data, original_model, dataset, model, changes):
    """Summarize one chat intervention in the source units used by its model."""
    if len(changes) != 1:
        return None
    kind = changes[0].type
    old, new = original_model['parameters'], model['parameters']
    match kind:
        case 'credit_limit_pct' | 'credit_limit_abs':
            key, label = 'credit_limit', 'الحد الائتماني'
            unit = 'ألف ريال' if template == 'contracting' else 'ريال'
            before, after = old[key], new[key]
            constraint = key
        case 'storage_pct':
            key, label, unit = 'cold_capacity', 'حد التخزين المبرد', 'وحدة تخزين'
            before, after = old[key], new[key]
            constraint = key
        case 'demand_pct':
            key, label, unit = 'demand', 'إجمالي الكمية المطلوبة', 'وحدة'
            sku = changes[0].sku
            before, after = (sum(row['qty'] for row in source['orders'] if sku in (None, row['sku_id']))
                             for source in (original_data, dataset))
            constraint = None
        case _:
            return None
    return {'metric': key, 'before': before, 'after': after, 'unit': unit, 'label_ar': label,
            'constraint_id': constraint}
