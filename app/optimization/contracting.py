from dataclasses import dataclass, replace
from decimal import Decimal
from math import isfinite

from ortools.sat.python import cp_model

from app.config import Settings
from app.ingestion.loader import load_demo
from app.optimization.common import OptimizationInputError, Weights, configured_solver
from app.optimization.kpis import cash_curve, contracting_kpis, resource_usage
from app.templates_registry import load_templates


@dataclass(frozen=True)
class Activity:
    id: str
    project: str
    phase: str
    duration: int
    resource: str
    units: int
    cost: int


@dataclass
class ContractingProblem:
    projects: dict
    chains: dict[str, list[Activity]]
    activities: list[Activity]
    capacity: dict[str, list[int]]
    horizon: int
    lag: int
    credit: int
    rate: Decimal
    receipts: dict[str, Decimal]

    @property
    def cash_periods(self):
        # The last activity can finish at H and get paid at H + lag.
        return self.horizon + self.lag + 1


def prepare(dataset, parameters):
    horizon = parameters['horizon_weeks']
    projects = {row['id']: row for row in dataset['projects']}
    chains = {project: [] for project in projects}
    sequences = set()
    activities = []
    for row in sorted(dataset['activities'], key=lambda row: row['seq']):
        key = (row['project_id'], row['seq'])
        if key in sequences:
            raise OptimizationInputError(f'ترتيب الأنشطة مكرر في المشروع «{key[0]}».')
        sequences.add(key)
        activity = Activity(row['id'], row['project_id'], row['phase'], row['duration_weeks'],
                            row['resource'], row['units'], row['cost_per_week'])
        chains[activity.project].append(activity)
        activities.append(activity)

    capacity = {row['id']: [row['capacity']] * horizon for row in dataset['resources']}
    for resource, overrides in parameters['capacity_override'].items():
        if resource not in capacity or not isinstance(overrides, dict):
            raise OptimizationInputError(f'تعديل سعة الفريق «{resource}» غير صالح.')
        for week, available in overrides.items():
            try:
                index = int(week)
            except (ValueError, TypeError):
                raise OptimizationInputError(f'أسبوع تعديل سعة الفريق «{resource}» غير صالح.') from None
            if (str(index) != str(week) or not 0 <= index < horizon
                    or type(available) is not int or available < 0):
                raise OptimizationInputError(f'تعديل سعة الفريق «{resource}» في الأسبوع «{week}» غير صالح.')
            capacity[resource][index] = available

    margin = Decimal(str(parameters['margin']))
    return ContractingProblem(
        projects, chains, activities, capacity, horizon,
        parameters['payment_lag_weeks'], parameters['credit_limit'],
        Decimal(str(parameters['weekly_interest'])),
        {a.id: a.duration * a.cost * margin for a in activities},
    )


def baseline(problem, *, respect_credit=True):
    """Place each project's activities ASAP in dataset order using serial SGS."""
    order = [activity for chain in problem.chains.values() for activity in chain]
    return _serial_sgs(problem, order, respect_credit)


def schedule_with_priority(problem, order, durations, *, respect_credit=True):
    """Rebuild the sampled problem and schedule the first eligible activity in priority order."""
    priority = {activity_id: rank for rank, activity_id in enumerate(order)}
    activities = [replace(a, duration=durations[a.id]) for a in problem.activities]
    by_id = {a.id: a for a in activities}
    chains = {p: [by_id[a.id] for a in chain] for p, chain in problem.chains.items()}
    receipts = {
        a.id: problem.receipts[a.id] / a.duration * by_id[a.id].duration
        for a in problem.activities
    }
    sampled = replace(problem, activities=activities, chains=chains, receipts=receipts)
    ready = {p: iter(chain) for p, chain in chains.items() if chain}
    heads = {p: next(chain) for p, chain in ready.items()}
    eligible_order = []
    while heads:
        project = min(heads, key=lambda p: priority[heads[p].id])
        eligible_order.append(heads.pop(project))
        successor = next(ready[project], None)
        if successor is not None:
            heads[project] = successor
    return sampled, _serial_sgs(sampled, eligible_order, respect_credit)


def _serial_sgs(problem, order, respect_credit):
    starts = {}
    usage = {resource: [0] * problem.horizon for resource in problem.capacity}
    placed = []
    earliest = {p: row['nta_week'] for p, row in problem.projects.items()}
    for activity in order:
        capacity = problem.capacity[activity.resource]
        load = usage[activity.resource]
        for start in range(earliest[activity.project], problem.horizon - activity.duration + 1):
            finish = start + activity.duration
            if any(load[t] + activity.units > capacity[t] for t in range(start, finish)):
                continue
            starts[activity.id] = start
            if respect_credit:
                cash = cash_curve([*placed, activity], starts, problem.receipts,
                                  problem.lag, problem.cash_periods)
                if min(cash) < -problem.credit:
                    continue
            placed.append(activity)
            for week in range(start, finish):
                load[week] += activity.units
            earliest[activity.project] = finish
            break
        else:
            return None
    return starts


def evaluate(problem, starts):
    usage = resource_usage(problem.activities, starts, problem.capacity)
    kpis = contracting_kpis(problem, starts, usage)
    activities = [
        {
            'activity_id': a.id, 'project': a.project, 'phase': a.phase,
            'start_week': starts[a.id], 'finish_week': kpis['finishes'][a.id],
            'resource': a.resource, 'units': a.units,
        }
        for a in problem.activities
    ]
    return {'kpis': kpis, 'plan': {
        'activities': activities, 'resource_usage': usage, 'capacity': problem.capacity,
    }}


def build_model(problem, weights, hint):
    model = cp_model.CpModel()
    choices = {}
    starts = {}
    for project, chain in problem.chains.items():
        earliest = problem.projects[project]['nta_week']
        latest = problem.horizon - sum(a.duration for a in chain)
        for activity in chain:
            options = {
                week: model.new_bool_var(f'{activity.id}@{week}')
                for week in range(earliest, latest + 1)
                if all(cap >= activity.units for cap in problem.capacity[activity.resource][week:week + activity.duration])
            }
            model.add_exactly_one(options.values())
            choices[activity.id] = options
            starts[activity.id] = sum(week * var for week, var in options.items())
            earliest += activity.duration
            latest += activity.duration

    late = {}
    makespan = model.new_int_var(0, problem.horizon, 'makespan')
    for project, chain in problem.chains.items():
        if not chain:
            continue
        for previous, activity in zip(chain, chain[1:]):
            model.add(starts[activity.id] >= starts[previous.id] + previous.duration)
        last = chain[-1]
        finish = starts[last.id] + last.duration
        late[project] = model.new_int_var(0, problem.horizon, f'late_{project}')
        model.add(late[project] >= finish - problem.projects[project]['deadline_week'])
        model.add(makespan >= finish)

    resource_terms = {r: [[] for _ in capacity] for r, capacity in problem.capacity.items()}
    cash_terms = [[] for _ in range(problem.cash_periods)]
    receipts = {activity: int(receipt) for activity, receipt in problem.receipts.items()}
    for a in problem.activities:
        for start, choice in choices[a.id].items():
            finish = start + a.duration
            for week in range(start, finish):
                resource_terms[a.resource][week].append(a.units * choice)
            for week in range(start, problem.cash_periods):
                paid = receipts[a.id] if week >= finish + problem.lag else 0
                balance = paid - a.cost * min(week - start + 1, a.duration)
                if balance:
                    cash_terms[week].append(balance * choice)

    for resource, terms in resource_terms.items():
        for week, load in enumerate(terms):
            model.add(sum(load) <= problem.capacity[resource][week])

    max_od = sum(a.duration * a.cost for a in problem.activities)
    hint_cash = None if hint is None else cash_curve(
        problem.activities, hint, receipts, problem.lag, problem.cash_periods)
    # A known credit-feasible plan lets us enforce the ceiling during the solve.
    breach_limit = (0 if hint_cash is not None and min(hint_cash) >= -problem.credit
                    else max(0, max_od - problem.credit))
    overdrafts = []
    breaches = []
    for week, terms in enumerate(cash_terms):
        balance = sum(terms)
        od = model.new_int_var(0, min(max_od, problem.credit + breach_limit), f'od_{week}')
        breach = model.new_int_var(0, breach_limit, f'breach_{week}')
        model.add(od >= -balance)
        model.add(balance + breach >= -problem.credit)
        overdrafts.append(od)
        breaches.append(breach)
        if hint_cash is not None:
            model.add_hint(od, max(0, -hint_cash[week]))
            model.add_hint(breach, max(0, -hint_cash[week] - problem.credit))

    ld_weight, fin_weight, span_weight = weights.scaled()
    ld_coefficients = {p: 1000 * ld_weight * problem.projects[p]['ld_per_week'] for p in late}
    fin_coefficient = int(1000 * problem.rate) * fin_weight
    span_coefficient = 1000 * span_weight
    # The objective uses tenths for weight sliders. In the slack model, keep
    # any credit breach more expensive than the full range of the other terms.
    max_base_score = (problem.horizon * (sum(ld_coefficients.values()) + span_coefficient)
                      + fin_coefficient * max_od * problem.cash_periods)
    breach_coefficient = max(10_000_000, max_base_score + 1) if breach_limit else 0
    model.minimize(
        sum(ld_coefficients[p] * var for p, var in late.items())
        + fin_coefficient * sum(overdrafts) + span_coefficient * makespan
        + breach_coefficient * sum(breaches)
    )

    hint_score = None
    if hint_cash is not None:
        span = 0
        ld_score = 0
        for activity, options in choices.items():
            for week, choice in options.items():
                model.add_hint(choice, int(hint[activity] == week))
        for project, var in late.items():
            last = problem.chains[project][-1]
            finish = hint[last.id] + last.duration
            span = max(span, finish)
            weeks = max(0, finish - problem.projects[project]['deadline_week'])
            model.add_hint(var, weeks)
            ld_score += ld_coefficients[project] * weeks
        model.add_hint(makespan, span)
        hint_score = (
            ld_score + span_coefficient * span
            + fin_coefficient * sum(max(0, -cash) for cash in hint_cash)
            + breach_coefficient * sum(max(0, -cash - problem.credit) for cash in hint_cash)
        )

    return model, starts, hint_score


def solve(dataset, parameters=None, weights=None, time_limit=None, *, deterministic=False):
    defaults = load_templates()['contracting'].model_defaults()
    problem = prepare(dataset, defaults['parameters'] | (parameters or {}))
    weights = Weights(**(defaults['weights'] | (weights or {})))
    time_limit = Settings().solver_time_limit if time_limit is None else time_limit
    if not isfinite(time_limit) or time_limit <= 0:
        raise OptimizationInputError('وقت التحسين لازم يكون أكبر من صفر.')
    initial = baseline(problem)
    if initial is None:
        initial = baseline(problem, respect_credit=False)
    try:
        model, start_vars, hint_score = build_model(problem, weights, initial)
    except (OverflowError, ValueError, TypeError):
        raise OptimizationInputError('القيم الرقمية أكبر من النطاق المدعوم للتحسين.') from None
    if model.validate():
        raise OptimizationInputError('القيم الرقمية أكبر من النطاق المدعوم للتحسين.')

    solver = configured_solver(time_limit)
    if deterministic:
        solver.parameters.num_workers = 1
        solver.parameters.max_deterministic_time = 0.8 * time_limit
        solver.parameters.max_time_in_seconds = 3 * time_limit
    status = solver.solve(model)
    objective = None
    bound = None
    chosen = None
    if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        objective = solver.objective_value
        bound = solver.best_objective_bound
        chosen = {activity: solver.value(var) for activity, var in start_vars.items()}
    if initial is not None and (status == cp_model.UNKNOWN or objective is not None and hint_score < objective):
        chosen = initial
        objective = hint_score
        status = cp_model.FEASIBLE

    empty = {'kpis': {}, 'plan': {}}
    return {
        'baseline': empty if initial is None else evaluate(problem, initial),
        'optimized': empty if chosen is None else evaluate(problem, chosen),
        'solver': {'status': solver.status_name(status), 'objective': objective,
                   'bound': bound, 'wall_s': solver.wall_time},
        'explanation_ar': '',
    }


def solve_demo(**kwargs):
    return solve(load_demo('contracting'), **kwargs)
