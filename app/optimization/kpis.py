from decimal import Decimal
from itertools import accumulate


def cash_curve(activities, starts, receipts, lag, periods):
    flow = [0] * periods
    for activity in activities:
        start = starts[activity.id]
        finish = start + activity.duration
        for week in range(start, finish):
            flow[week] -= activity.cost
        flow[finish + lag] += receipts[activity.id]
    return list(accumulate(flow))


def resource_usage(activities, starts, capacity):
    usage = {resource: [0] * len(weeks) for resource, weeks in capacity.items()}
    for activity in activities:
        start = starts[activity.id]
        for week in range(start, start + activity.duration):
            usage[activity.resource][week] += activity.units
    return usage


def contracting_kpis(problem, starts, usage):
    finishes = {a.id: starts[a.id] + a.duration for a in problem.activities}
    late = {
        project: max(0, finishes[chain[-1].id] - problem.projects[project]['deadline_week'])
        if chain else 0
        for project, chain in problem.chains.items()
    }
    ld = sum(weeks * problem.projects[project]['ld_per_week'] for project, weeks in late.items())
    cash = cash_curve(problem.activities, starts, problem.receipts, problem.lag, problem.cash_periods)
    overdraft = [max(0, -balance) for balance in cash]
    peak = max(overdraft)
    breach = max(0, peak - problem.credit)
    interest = sum(overdraft) * problem.rate

    return {
        'total': float(round(ld + interest, 1)),
        'ld': ld,
        'interest': float(round(interest, 1)),
        'peak_od': float(peak),
        'credit_ok': breach == 0,
        'breach': float(breach),
        'late': late,
        'late_weeks_total': sum(late.values()),
        'makespan': max(finishes.values(), default=0),
        'starts': dict(starts),
        'finishes': finishes,
        'resource_util': {
            resource: round(100 * sum(usage[resource]) / sum(cap), 1) if any(cap) else 0.0
            for resource, cap in problem.capacity.items()
        },
        'binding_weeks': {
            resource: sum(used == available for used, available in zip(usage[resource], cap))
            for resource, cap in problem.capacity.items()
        },
        'cash_curve': [float(balance) for balance in cash],
    }


def production_kpis(problem, plan):
    schedule = sorted(plan['schedule'], key=lambda row: (row['day'], row['line']))
    slots = {}
    last_sku = {}
    changeover_days = []
    units = {sku: [0] * problem.horizon for sku in problem.skus}
    flow = [0] * problem.horizon
    feasible = True
    changeover_cost = 0
    for row in schedule:
        line, day, sku, qty = (row[key] for key in ('line', 'day', 'sku', 'qty'))
        if line not in problem.lines or sku not in problem.skus or day not in problem.days:
            raise ValueError('Invalid production row')
        change = qty > 0 and line in last_sku and last_sku[line] != sku
        specs = problem.lines[line]
        capacity = problem.rates.get((line, sku), 0)
        if change:
            capacity -= specs['changeover_loss_units']
            changeover_days.append({'line': line, 'day': day})
            changeover_cost += specs['changeover_cost']
        feasible &= (line, day) not in slots and day not in problem.down[line] and 0 < qty <= capacity
        slots[line, day] = row
        if qty > 0:
            last_sku[line] = sku
        units[sku][day] += qty
        flow[day] -= qty * problem.skus[sku]['unit_cost']

    shipments = []
    shipped = set()
    revenue = cost_of_sales = lateness_cost = 0
    for row in sorted(plan['shipments'], key=lambda row: (row['day'], row['order'])):
        order_id, day = row['order'], row['day']
        if order_id not in problem.orders or day not in problem.days:
            raise ValueError('Invalid shipment row')
        feasible &= order_id not in shipped
        shipped.add(order_id)
        order = problem.orders[order_id]
        sku = problem.skus[order['sku_id']]
        qty = order['qty']
        late = max(0, day - order['due_day'])
        shipments.append({'order': order_id, 'day': day, 'late_days': late})
        lateness_cost += late * order['penalty_per_day']
        units[order['sku_id']][day] -= qty
        receipt = qty * sku['unit_price']
        revenue += receipt
        cost_of_sales += qty * sku['unit_cost']
        if day + problem.lag < problem.horizon:
            flow[day + problem.lag] += receipt

    inventory = {sku: list(accumulate(
        (specs['initial_inventory'] + units[sku][0], *units[sku][1:])
    )) for sku, specs in problem.skus.items()}
    storage = [sum(specs['volume'] * inventory[sku][day] for sku, specs in problem.skus.items())
               for day in problem.days]
    storage_ok = max(storage) <= problem.storage
    feasible &= storage_ok and all(min(values) >= 0 for values in inventory.values())
    holding_cents = sum(specs['holding_cost_per_day'] * sum(inventory[sku])
                        for sku, specs in problem.skus.items())
    cash = list(accumulate((problem.opening_cash + flow[0], *flow[1:])))
    overdraft = [max(0, -balance) for balance in cash]
    peak_od = max(overdraft)
    breach = max(0, peak_od - problem.credit)
    interest = sum(overdraft) * problem.daily_interest
    unshipped = [order for order in problem.orders if order not in shipped]
    unshipped_cost = sum(problem.unshipped_penalty(problem.orders[order]) for order in unshipped)
    holding_cost = Decimal(holding_cents) / 100
    total = lateness_cost + changeover_cost + holding_cost + interest + unshipped_cost
    return {
        'total_cost': float(round(total, 2)),
        'lateness_cost': lateness_cost,
        'late_orders': sum(row['late_days'] > 0 for row in shipments),
        'late_days_total': sum(row['late_days'] for row in shipments),
        'unshipped': len(unshipped),
        'unshipped_orders': unshipped,
        'unshipped_cost': unshipped_cost,
        'changeovers': len(changeover_days),
        'changeover_days': changeover_days,
        'changeover_cost': changeover_cost,
        'holding_cost': float(holding_cost),
        'interest': float(round(interest, 2)),
        'peak_od': peak_od,
        'credit_ok': breach == 0,
        'breach': breach,
        'revenue': revenue,
        'margin': revenue - cost_of_sales,
        'line_util': {
            line: round(100 * sum((line, day) in slots for day in problem.days) / available, 1)
            if (available := problem.horizon - len(problem.down[line])) else 0.0
            for line in problem.lines
        },
        'peak_storage_pct': round(100 * max(storage) / problem.storage, 1) if problem.storage else 0.0,
        'storage_curve': storage,
        'storage_ok': storage_ok,
        'feasible': feasible,
        'schedule': schedule,
        'shipments': shipments,
        'inventory': inventory,
        'cash_curve': cash,
    }
