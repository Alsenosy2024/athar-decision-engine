"""Fixed production policies are scored without access to solver variables or bounds."""
from collections import defaultdict

from app.optimization.kpis import production_kpis

OBJECTIVES = ('cost_sar', 'service_loss_pct', 'peak_credit_sar')


def dispatch(problem, plan):
    """Keep booked shipment dates. Skip unavailable forecast lots and retry later.

    Confirmed orders get priority over forecasts with the same booking date.
    Past decisions never depend on future inventory or orders.
    """
    production = defaultdict(list)
    bookings = defaultdict(list)
    for row in plan['schedule']:
        production[row['day']].append(row)
    for row in plan['shipments']:
        bookings[row['day']].append(row['order'])
    stock = {sku: specs['initial_inventory'] for sku, specs in problem.skus.items()}
    shipments, pending = [], []
    for day in problem.days:
        for row in production[day]:
            stock[row['sku']] += row['qty']
        pending.extend(sorted(bookings[day], key=lambda key: (problem.orders[key].get('is_forecast', False), key)))
        waiting = []
        for key in pending:
            order = problem.orders[key]
            sku, qty = order['sku_id'], order['qty']
            if not qty:
                continue
            if qty <= stock[sku]:
                stock[sku] -= qty
                shipments.append({'order': key, 'day': day})
            else:
                waiting.append(key)
        pending = waiting
    return plan | {'shipments': shipments}


def measure(problem, plan):
    kpis = production_kpis(problem, plan)
    demand = sum(row['qty'] for row in problem.orders.values())
    on_time = sum(problem.orders[row['order']]['qty'] for row in kpis['shipments']
                  if not row['late_days'])
    return {
        'cost_sar': kpis['total_cost'],
        'service_loss_pct': 100 * (demand - on_time) / demand if demand else 0.0,
        'peak_credit_sar': kpis['peak_od'],
        'credit_breach_sar': kpis['breach'],
        'physical_ok': kpis['feasible'],
        'holding_and_setup_sar': kpis['holding_cost'] + kpis['changeover_cost'],
        'financing_sar': kpis['interest'],
        'storage_by_day': kpis['storage_curve'],
        'credit_by_day': [max(0, -balance) for balance in kpis['cash_curve']],
    }


def cvar(values, probabilities, alpha):
    remaining = 1 - alpha
    total = 0.0
    for value, mass in sorted(zip(values, probabilities), reverse=True):
        taken = min(mass, remaining)
        total += taken * value
        remaining -= taken
        if remaining <= 1e-14:
            break
    return total / (1 - alpha)


def assess(rows, probabilities, policy, scales, hard_credit):
    def expected(values):
        return sum(value * weight for value, weight in zip(values, probabilities))

    keys = (*OBJECTIVES, 'credit_breach_sar', 'holding_and_setup_sar', 'financing_sar')
    mean = {key: expected([row[key] for row in rows]) for key in keys}
    tail = {key: cvar([row[key] for row in rows], probabilities, policy.alpha)
            for key in (*OBJECTIVES, 'credit_breach_sar')}

    def risk(key, violates, severity):
        mass = expected([violates(row) for row in rows])
        return key, {'probability': mass, 'mean_excess': expected([severity(row) for row in rows]),
                     'max_excess': max(severity(row) for row in rows)}

    breaches = dict([
        risk('physical', lambda row: not row['physical_ok'], lambda row: int(not row['physical_ok'])),
        risk('credit', lambda row: row['credit_breach_sar'] > 0, lambda row: row['credit_breach_sar']),
        risk('service', lambda row: row['service_loss_pct'] > 100 - policy.min_service_pct + 1e-9,
             lambda row: max(0.0, row['service_loss_pct'] - (100 - policy.min_service_pct))),
    ])
    score = mean['cost_sar'] / scales['cost_sar'] + policy.tail_weight * sum(
        tail[key] / scales[key] for key in OBJECTIVES
    )
    failed = []
    if breaches['physical']['max_excess']:
        failed.append('physical')
    if (hard_credit and breaches['credit']['max_excess']
            or breaches['credit']['probability'] > policy.max_credit_risk + 1e-9):
        failed.append('credit')
    if breaches['service']['probability'] > policy.max_service_risk + 1e-9:
        failed.append('service')
    return {'mean': mean, 'cvar': tail, 'breaches': breaches, 'score': score,
            'eligible': not failed, 'failed_rules': failed}
