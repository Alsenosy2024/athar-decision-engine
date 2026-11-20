from dataclasses import replace
from math import log

import numpy as np

from app.forecasting.methods import quantiles
from app.optimization import contracting, production
from app.optimization.common import OptimizationInputError
from app.optimization.kpis import contracting_kpis, production_kpis, resource_usage
from app.templates_registry import load_templates


def histogram(values):
    bins, counts = np.unique(values, return_counts=True)
    return [{'bin': value, 'count': count} for value, count in zip(bins.tolist(), counts.tolist())]


def contracting_trial_schedule(problem, resources, order, durations):
    sampled, starts = contracting.schedule_with_priority(problem, order, durations)
    if starts is None:
        # Keep known outages inside the planning window. Beyond it, resume the
        # normal crew capacity to capture overruns without discarding trials.
        extra = sum(durations.values()) + problem.lag * len(durations)
        if any(a.units > resources[a.resource] for a in problem.activities):
            raise OptimizationInputError('سعة الفريق لا تسمح بمحاكاة مدة النشاط.')
        extended = replace(
            problem, horizon=problem.horizon + extra,
            capacity={key: weeks + [resources[key]] * extra for key, weeks in problem.capacity.items()},
        )
        sampled, starts = contracting.schedule_with_priority(extended, order, durations)
        if starts is None:
            # The serial schedule can require extra credit even after allowing
            # more time. Measure the resulting breach on a crew-feasible plan.
            sampled, starts = contracting.schedule_with_priority(
                extended, order, durations, respect_credit=False,
            )
    return sampled, starts


def contracting_draw(problem, resources, order, durations):
    sampled, starts = contracting_trial_schedule(problem, resources, order, durations)
    usage = resource_usage(sampled.activities, starts, sampled.capacity)
    return contracting_kpis(sampled, starts, usage)


def contracting_mc(dataset, parameters, plan, forecast, n, rng):
    problem = contracting.prepare(dataset, parameters)
    phases = forecast['phases']
    priority = [row['activity_id'] for row in sorted(plan['activities'], key=lambda row: row['start_week'])]
    resources = {row['id']: row['capacity'] for row in dataset['resources']}
    means = [phases[a.phase]['mu'] for a in problem.activities]
    deviations = [phases[a.phase]['sigma'] for a in problem.activities]
    draws = rng.lognormal(means, deviations, size=(n, len(problem.activities)))

    weeks, costs, overdrafts, breaches = [], [], [], []
    projects = {project: [] for project in problem.projects}
    for multipliers in draws:
        durations = {a.id: max(1, round(a.duration * m)) for a, m in zip(problem.activities, multipliers)}
        kpis = contracting_draw(problem, resources, priority, durations)
        weeks.append(kpis['late_weeks_total'])
        costs.append(kpis['total'])
        overdrafts.append(kpis['peak_od'])
        breaches.append(not kpis['credit_ok'])
        for project, late in kpis['late'].items():
            projects[project].append(late)

    return {
        'n': n,
        'p_any_late': sum(value > 0 for value in weeks) / n,
        'p_credit_breach': sum(breaches) / n,
        'late_weeks': {**quantiles(weeks), 'mean': sum(weeks) / n, 'hist': histogram(weeks)},
        'total_cost': quantiles(costs),
        'peak_od': quantiles(overdrafts),
        'per_project': {
            project: {'p_late': sum(value > 0 for value in values) / n, 'late_p50': float(np.median(values))}
            for project, values in projects.items()
        },
    }


def production_trial_plan(problem, schedule, departures, daily_supply, quantities):
    orders = {key: {**row, 'qty': quantities[key]} for key, row in problem.orders.items()}
    inventory = {key: row['initial_inventory'] for key, row in problem.skus.items()}
    pending, shipments = [], []
    for day in problem.days:
        for sku, qty in daily_supply[day].items():
            inventory[sku] += qty
        pending.extend(departures[day])
        waiting = []
        for key in pending:
            sku = orders[key]['sku_id']
            qty = quantities[key]
            if inventory[sku] < qty:
                waiting.append(key)
                continue
            inventory[sku] -= qty
            shipments.append({'order': key, 'day': day})
        pending = waiting

    return replace(problem, orders=orders), {'schedule': schedule, 'shipments': shipments}


def production_draw(problem, schedule, departures, daily_supply, quantities):
    sampled, plan = production_trial_plan(problem, schedule, departures, daily_supply, quantities)
    kpis = production_kpis(sampled, plan)
    shipped = {row['order'] for row in plan['shipments']}
    late = {row['order'] for row in kpis['shipments'] if row['late_days'] and quantities[row['order']]}
    late.update(key for key in kpis['unshipped_orders'] if quantities[key])
    total = sum(quantities.values())
    fill_rate = sum(quantities[key] for key in shipped) / total if total else 1.0
    return fill_rate, late, not kpis['storage_ok'], not kpis['credit_ok']


def production_mc(dataset, parameters, plan, forecast, n, rng):
    problem = production.prepare(dataset, parameters)
    forecast_by_sku = {series['key']: series for series in forecast['series']}
    orders = list(problem.orders)
    departures = [[] for _ in problem.days]
    for shipment in sorted(plan['shipments'], key=lambda row: (row['day'], row['order'])):
        departures[shipment['day']].append(shipment['order'])
    supply = [{} for _ in problem.days]
    for row in plan['schedule']:
        day, sku = row['day'], row['sku']
        supply[day][sku] = supply[day].get(sku, 0) + row['qty']

    deviations = []
    for order in problem.orders.values():
        series = forecast_by_sku[order['sku_id']]
        week = min(order['due_day'] // 7, len(series['p50']) - 1)
        low, median, high = (series[key][week] for key in ('p10', 'p50', 'p90'))
        # Order quantity is the median of its multiplier distribution. The
        # forecast supplies relative dispersion. With a zero lower band, use
        # the upper half alone, reflected around the median on the log scale.
        if low > 0:
            sigma = (log(high) - log(low)) / 2.5631
        elif median > 0:
            sigma = 2 * (log(high) - log(median)) / 2.5631
        else:
            sigma = 0.0
        deviations.append(sigma)
    draws = rng.lognormal(0, deviations, size=(n, len(orders)))
    filled, late_orders = [], []
    storage_count = credit_count = 0
    late_counts = dict.fromkeys(orders, 0)
    for multipliers in draws:
        quantities = {key: round(problem.orders[key]['qty'] * m) for key, m in zip(orders, multipliers)}
        fill_rate, late, overflow, breach = production_draw(
            problem, plan['schedule'], departures, supply, quantities,
        )
        filled.append(fill_rate)
        late_orders.append(len(late))
        storage_count += overflow
        credit_count += breach
        for key in late:
            late_counts[key] += 1
    return {
        'n': n,
        'p_any_late': sum(value > 0 for value in late_orders) / n,
        'p_storage_overflow': storage_count / n,
        'p_credit_breach': credit_count / n,
        'fill_rate': quantiles(filled),
        'late_orders': {**quantiles(late_orders), 'hist': histogram(late_orders)},
        'per_order': {key: {'p_late': count / n} for key, count in late_counts.items()},
    }


def run_mc(template, dataset, plan, forecast, n=200, seed=42, *, parameters=None):
    defaults = load_templates()[template].model_defaults()['parameters']
    simulate = {'contracting': contracting_mc, 'production': production_mc}[template]
    return simulate(dataset, defaults | (parameters or {}), plan, forecast, n, np.random.default_rng(seed))


def driver_trials(centers, spreads, n, seed, correlations=()):
    """Preserve the original random stream for independent assumptions."""
    if correlations:
        from app.simulation.dependence import correlated_triangular

        keys = sorted(key for key in centers if spreads[key])
        for shock in correlated_triangular(keys, correlations, n, seed):
            yield centers | {key: float(centers[key] + spreads[key] * value)
                             for key, value in zip(keys, shock)}
        return
    rng = np.random.default_rng(seed)
    columns = {
        key: rng.triangular(value - spreads[key], value, value + spreads[key], size=n)
        for key, value in sorted(centers.items()) if spreads[key]
    }
    for i in range(n):
        yield centers | {key: float(values[i]) for key, values in columns.items()}
