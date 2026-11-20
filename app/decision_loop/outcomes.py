"""Score a stored shadow policy on one fully observed demand window."""
from app.decision_loop.candidates import SOLVE_SECONDS, solve_candidate
from app.decision_loop.evaluation import dispatch, measure
from app.decision_loop.scenarios import Demand
from app.optimization.common import OptimizationInputError
from app.optimization.production import prepare


def evaluate_window(dataset, model, contract, risk, quantities):
    base = prepare(dataset, model['parameters'])
    demand = Demand.from_contract(dataset, base, contract)
    if quantities.keys() != demand.bands.keys():
        raise OptimizationInputError('سجّل الكمية الفعلية لكل بند طلب إضافي، بما فيها الصفر.')
    selected = risk['selected_id']
    if selected is None:
        raise OptimizationInputError('مفيش بديل مختار في سجل التشغيل لقياس نتيجته.')
    problem = demand.problem(base, quantities)
    hard_credit = risk['credit_policy'] == 'hard'

    def eligible(metrics):
        return metrics['physical_ok'] and (not hard_credit or not metrics['credit_breach_sar'])

    evaluated = {}
    hint = None
    for row in risk['candidates']:
        if row['id'] in {selected, risk['reference_id']}:
            plan = dispatch(problem, row['plan'])
            metrics = measure(problem, plan)
            evaluated[row['id']] = {'candidate_version': row['version'], 'metrics': metrics,
                                    'realized_feasible': eligible(metrics)}
            if row['id'] == selected:
                hint = plan

    # With hindsight, quantities are known before production starts. Retain the
    # same shipment release dates and operating constraints for comparison.
    solver, plan = solve_candidate(problem, demand.release, model['weights'], 'cost', SOLVE_SECONDS,
                                   hint=hint, hard_credit=hard_credit)
    hindsight = measure(problem, plan) if plan is not None else None
    verified_costs = [row['metrics']['cost_sar'] for row in evaluated.values() if row['realized_feasible']]
    if hindsight is not None and eligible(hindsight):
        verified_costs.append(hindsight['cost_sar'])
    reference = min(verified_costs, default=None)
    for row in evaluated.values():
        row['cost_gap_sar'] = row['metrics']['cost_sar'] - reference if (
            reference is not None and row['realized_feasible']
        ) else None
    return {'selected_id': selected, 'evaluated': evaluated,
            'reference': {'kind': 'best_known_feasible', 'cost_sar': reference, 'solver': solver},
            'unit': 'SAR'}
