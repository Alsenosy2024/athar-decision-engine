"""Re-solve each week on the original timeline while locking executed rows."""

from app.decision_loop.candidates import SOLVE_SECONDS, prefix, solve_candidate
from app.decision_loop.evaluation import dispatch, measure
from app.optimization.kpis import production_kpis


def ledger(problem, plan, cutoff):
    stock = {key: row['initial_inventory'] for key, row in problem.skus.items()}
    setup = {}
    receivables = {}
    for row in plan['schedule']:
        if row['day'] < cutoff:
            stock[row['sku']] += row['qty']
    spend = sum(row['qty'] * problem.skus[row['sku']]['unit_cost']
                for row in plan['schedule'] if row['day'] < cutoff)
    for row in sorted(plan['schedule'], key=lambda item: item['day']):
        if row['day'] < cutoff:
            setup[row['line']] = row['sku']
    for row in plan['shipments']:
        if row['day'] >= cutoff:
            continue
        order = problem.orders[row['order']]
        sku, qty = order['sku_id'], order['qty']
        stock[sku] -= qty
        day = row['day'] + problem.lag
        receivables[day] = receivables.get(day, 0) + qty * problem.skus[sku]['unit_price']
    return {'inventory': stock, 'cash_sar': problem.opening_cash - spend + sum(
        receipt for day, receipt in receivables.items() if day < cutoff
    ), 'receivables_sar': {day: receipt for day, receipt in sorted(receivables.items()) if day >= cutoff},
        'last_setup': setup}


def rolling(base, demand, selected, realized, weights, *, hard_credit, budget, actual=False,
            seconds=SOLVE_SECONDS):
    """Commit day zero, then look only at each weekly information set.

    Open inventory, cash, pending receipts and last setup enter the new solve via
    equations tied to the frozen past. Keep the original dates for auditability.
    """
    plan = selected
    committed = prefix(plan, 1)
    stages = []
    observed_release = {demand.release[key] for key in realized}
    starts = [day for day in sorted({1, *range(7, base.horizon, 7)}) if day < base.horizon]
    ends = [*starts[1:], base.horizon]
    problem = demand.problem(base, {})
    reason = None
    for day, end in zip(starts, ends):
        if actual and any(demand.release[key] <= day and demand.release[key] not in observed_release
                          for key in demand.forecast):
            reason = 'awaiting_observations'
            break
        if budget.remaining() < seconds:
            reason = 'budget_exhausted'
            break
        problem = demand.observe(base, realized, day)
        opening = ledger(problem, committed, day)
        fallback = dispatch(problem, plan)
        floor = None
        if prefix(fallback, day) == committed:
            fixed_metrics = measure(problem, fallback)
            if fixed_metrics['physical_ok'] and (not hard_credit or not fixed_metrics['credit_breach_sar']):
                floor = sum(problem.orders[row['order']]['qty'] for row in fallback['shipments']
                            if row['day'] <= problem.orders[row['order']]['due_day'])
        solver, alternative = solve_candidate(
            problem, demand.release, weights, 'cost', seconds,
            committed=committed, day=day, hint=fallback, hard_credit=hard_credit, min_on_time=floor,
        )
        options = []
        for option in (alternative, fallback):
            if option is None or prefix(option, day) != committed:
                continue
            metrics = measure(problem, option)
            if metrics['physical_ok'] and (not hard_credit or not metrics['credit_breach_sar']):
                options.append((metrics['cost_sar'], option))
        if not options:
            reason = 'no_verified_recourse'
            stages.append({'day': day, 'solver': solver, 'opening': opening, 'closing': None})
            break
        _, plan = min(options, key=lambda pair: pair[0])
        committed = prefix(plan, end)
        closing = ledger(problem, committed, end)
        kpis = production_kpis(problem, plan)
        transition_ok = all(
            state['cash_sar'] == kpis['cash_curve'][cut - 1]
            and all(quantity == kpis['inventory'][sku][cut - 1]
                    for sku, quantity in state['inventory'].items())
            for state, cut in ((opening, day), (closing, end))
        )
        if not transition_ok:
            raise RuntimeError('Replan ledger diverged from production balances')
        stages.append({
            'day': day, 'through_day': end - 1, 'solver': solver, 'min_on_time_units': floor,
            'action_source': 'cp_sat' if plan is alternative else 'verified_previous_schedule',
            'revealed_quantities': {key: value for key, value in realized.items() if demand.release[key] == day},
            'opening': opening, 'closing': closing, 'ledger_verified': transition_ok,
        })
    finished = reason is None
    return {'status': 'complete' if finished else reason, 'first_shift': prefix(selected, 1),
            'stages': stages, 'committed': committed, 'metrics': measure(problem, committed) if finished else None}
