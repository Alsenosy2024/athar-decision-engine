"""Rebind previous plans to current constraints before setting solver hints."""
from dataclasses import dataclass
from datetime import date
from time import monotonic

from ortools.sat.python import cp_model

from app.optimization.kpis import cash_curve, production_kpis


@dataclass(frozen=True)
class PreviousPlan:
    plan: dict
    previous_dataset: dict
    previous_parameters: dict


def period_shift(previous, parameters, start_key, days_per_period):
    days = (date.fromisoformat(previous.previous_parameters[start_key])
            - date.fromisoformat(parameters[start_key])).days
    return days // days_per_period if days % days_per_period == 0 else None


def production_hint(problem, previous, parameters, late_cap):
    shift = period_shift(previous, parameters, 'plan_start_date', 1)
    if shift is None:
        return None
    schedule = []
    last_sku = {}
    used_slots = set()
    for row in sorted(previous.plan.get('schedule', []), key=lambda row: (row['day'], row['line'])):
        line, sku, day = row['line'], row['sku'], row['day'] + shift
        rate = problem.rates.get((line, sku), 0)
        if not rate or day not in problem.days or day in problem.down[line] or (line, day) in used_slots:
            continue
        if line in last_sku and last_sku[line] != sku:
            rate -= problem.lines[line]['changeover_loss_units']
        if row['qty'] > rate or row['qty'] <= 0:
            continue
        used_slots.add((line, day))
        last_sku[line] = sku
        schedule.append({**row, 'day': day})

    old_orders = {row['id']: row for row in previous.previous_dataset['orders']}
    shipments = []
    shipped = set()
    late = 0
    for row in sorted(previous.plan.get('shipments', []), key=lambda row: (row['day'], row['order'])):
        order_id, day = row['order'], row['day'] + shift
        old = old_orders.get(order_id)
        current = problem.orders.get(order_id)
        if (old is None or current is None or order_id in shipped or day not in problem.days
                or any(old[key] != current[key] for key in ('sku_id', 'qty', 'customer_ar', 'is_export'))):
            continue
        is_late = day > current['due_day']
        if late_cap is not None and late + is_late > late_cap:
            continue
        shipments.append({'order': order_id, 'day': day})
        shipped.add(order_id)
        late += is_late

    hint = {'schedule': schedule, 'shipments': shipments}
    if not schedule and not shipments:
        return None
    kpis = production_kpis(problem, hint)
    return hint if kpis['feasible'] and kpis['credit_ok'] else None


def contracting_hint(problem, previous, parameters):
    shift = period_shift(previous, parameters, 'project_start_date', 7)
    if shift is None:
        return None
    prior = {row['activity_id']: row for row in previous.plan.get('activities', [])}
    remaining = {resource: list(weeks) for resource, weeks in problem.capacity.items()}
    starts = {}
    placed = []
    for project, chain in problem.chains.items():
        earliest = problem.projects[project]['nta_week']
        latest = problem.horizon - sum(activity.duration for activity in chain)
        for activity in chain:
            old = prior.get(activity.id)
            if old is None or any(old[key] != getattr(activity, key) for key in ('project', 'phase', 'resource')):
                break
            start = old['start_week'] + shift
            finish = start + activity.duration
            load = remaining[activity.resource]
            if not earliest <= start <= latest or any(
                load[week] < activity.units for week in range(start, finish)
            ):
                break
            tentative = starts | {activity.id: start}
            if min(cash_curve([*placed, activity], tentative, problem.receipts,
                              problem.lag, problem.cash_periods)) < -problem.credit:
                break
            starts = tentative
            placed.append(activity)
            for week in range(start, finish):
                load[week] -= activity.units
            earliest = finish
            latest += activity.duration
    return starts or None


class FirstQualified(cp_model.CpSolverSolutionCallback):
    """Time to the first independently checked solver incumbent, including setup."""

    def __init__(self, started, qualified):
        super().__init__()
        self.started = started
        self.qualified = qualified
        self.elapsed = None

    def on_solution_callback(self):
        if self.elapsed is None and self.qualified(self):
            self.elapsed = monotonic() - self.started


def hint_metrics(previous, hint, measure_first, first):
    if previous is None and not measure_first:
        return {}
    return {
        'hint_source': 'previous_plan' if hint is not None else 'heuristic',
        'first_qualified_s': first.elapsed if first is not None else None,
    }
