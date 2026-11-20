"""Pareto comparison over verified operating alternatives and shared allocations."""
from collections import Counter
from itertools import product
from math import prod
from time import perf_counter

from app.decision_loop.evaluation import OBJECTIVES

TOLERANCE = 1e-8
MAX_COMBINATIONS = 50_000


def nondominated(points):
    """Return one stable representative per equivalent point."""
    frontier = []
    for point in points:
        values = point['objectives']
        if any(all(abs(point2['objectives'][key] - values[key]) <= TOLERANCE
                   for key in OBJECTIVES) for point2 in frontier):
            continue
        if any(
            all(other['objectives'][key] <= values[key] + TOLERANCE for key in OBJECTIVES)
            and any(other['objectives'][key] < values[key] - TOLERANCE for key in OBJECTIVES)
            for other in points
        ):
            continue
        frontier.append(point)
    return frontier


def production_frontier(candidates, reference):
    if reference is None:
        return []
    ref = reference['assessment']['mean']
    points = []
    for candidate in candidates:
        result = candidate['assessment']
        if not result['eligible']:
            continue
        mean = result['mean']
        points.append({
            'candidate_id': candidate['id'],
            'objectives': {key: mean[key] for key in OBJECTIVES},
            'department_deltas': {
                'operations': {'holding_and_setup_sar': mean['holding_and_setup_sar'] - ref['holding_and_setup_sar']},
                'sales': {'on_time_service_pp': ref['service_loss_pct'] - mean['service_loss_pct']},
                'finance': {'peak_credit_sar': mean['peak_credit_sar'] - ref['peak_credit_sar'],
                            'financing_sar': mean['financing_sar'] - ref['financing_sar']},
                'leadership': {'cost_sar': mean['cost_sar'] - ref['cost_sar']},
            },
            'solver': candidate['solver'],
        })
    return nondominated(points)


def shared_frontier(problem, metrics):
    """Enumerate the small allocation instance using its existing units and limits.

    Every selected bundle is rechecked in the original resource units. Keep equal
    weights for unit service percentages explicit in the shared metric contract.
    """
    started = perf_counter()
    groups = [unit.options for unit in problem.units]
    count = prod(map(len, groups))
    if count > MAX_COMBINATIONS:
        raise ValueError('عدد حزم التخصيص أكبر من الحد المسموح للتجربة.')
    reference = [metrics[unit.id][unit.current] for unit in problem.units]
    # Incremental skyline keeps the work bounded in addition to the bundle count.
    points = []
    checked = 0
    comparisons = 0
    status = 'OPTIMAL'
    for options in product(*groups):
        if comparisons >= 1_000_000:
            status = 'PARTIAL'
            break
        checked += 1
        usage = Counter(dict.fromkeys(problem.limits, 0.0))
        for option in options:
            usage.update(option.resources)
        if any(usage[key] > limit + TOLERANCE for key, limit in problem.limits.items()):
            continue
        rows = [metrics[unit.id][opt.id] for unit, opt in zip(problem.units, options)]
        values = {
            'cost_sar': sum(row.cost_sar for row in rows),
            'service_loss_pct': sum(row.service_loss_pct for row in rows) / len(rows),
            'peak_credit_sar': max(usage[key] for key in problem.limits if key.startswith('credit.')),
        }
        point = {'selection': {unit.id: opt.id for unit, opt in zip(problem.units, options)},
                 'objectives': values, 'resource_usage': dict(usage),
                 'versions': {unit.id: row.version for unit, row in zip(problem.units, rows)},
                 'department_deltas': {
                     unit.id: {'cost_sar': row.cost_sar - old.cost_sar,
                               'on_time_service_pp': old.service_loss_pct - row.service_loss_pct,
                               'credit_by_period_sar': {key: opt.resources.get(key, 0) - next(
                                   item.resources.get(key, 0) for item in unit.options if item.id == unit.current
                               ) for key in problem.limits if key.startswith('credit.')}}
                     for unit, row, old, opt in zip(problem.units, rows, reference, options)
                 }}
        comparisons += (len(points) + 1)**2
        points = nondominated([*points, point])
    elapsed = perf_counter() - started
    for point in points:
        point['solver'] = {'status': status, 'wall_s': elapsed}
    return {'frontier': points, 'status': status, 'wall_s': elapsed,
            'checked_combinations': checked, 'total_combinations': count, 'tolerance': TOLERANCE}
