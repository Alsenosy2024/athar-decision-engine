from dataclasses import dataclass
from decimal import Decimal
from math import isfinite

from ortools.sat.python import cp_model

from app.config import Settings
from app.ingestion.loader import load_demo
from app.optimization.common import OptimizationInputError, configured_solver
from app.optimization.kpis import production_kpis
from app.templates_registry import load_templates


@dataclass(frozen=True)
class ProductionProblem:
    lines: dict
    skus: dict
    orders: dict
    rates: dict
    down: dict
    horizon: int
    storage: int
    credit: int
    opening_cash: int
    lag: int
    daily_interest: Decimal

    @property
    def days(self):
        return range(self.horizon)

    def unshipped_penalty(self, order):
        sku = self.skus[order['sku_id']]
        return order['penalty_per_day'] * self.horizon + order['qty'] * (
            sku['unit_price'] - sku['unit_cost']
        )


def prepare(dataset, parameters):
    lines = {row['id']: row for row in dataset['lines']}
    skus = {row['id']: row for row in dataset['skus']}
    orders = {row['id']: row for row in dataset['orders']}
    horizon = parameters['horizon_days']
    if any(not 0 <= order['due_day'] < horizon for order in orders.values()):
        raise OptimizationInputError('موعد تسليم أحد الطلبات خارج أفق التخطيط.')

    down = {line: set() for line in lines}
    for line, days in parameters['line_down'].items():
        if (line not in down or not isinstance(days, list)
                or any(type(day) is not int or not 0 <= day < horizon for day in days)):
            raise OptimizationInputError(f'أيام التوقف للخط «{line}» غير صالحة.')
        down[line].update(days)

    for rows, fields in (
        (lines, ('changeover_cost', 'changeover_loss_units')),
        (skus, ('unit_cost', 'unit_price', 'initial_inventory', 'holding_cost_per_day', 'volume')),
        (orders, ('penalty_per_day',)),
    ):
        if any(row[field] < 0 for row in rows.values() for field in fields):
            raise OptimizationInputError('تكاليف الإنتاج والكميات والسعات لازم تكون غير سالبة.')

    rates = {(row['line_id'], row['sku_id']): row['units_per_day']
             for row in dataset['rates'] if row['units_per_day'] > 0}
    return ProductionProblem(
        lines, skus, orders, rates, down, horizon, parameters['cold_capacity'],
        parameters['credit_limit'], parameters['opening_cash'], parameters['payment_lag_days'],
        Decimal(str(parameters['weekly_interest'])) / 7,
    )


def evaluate(problem, plan):
    """Recount inventory, cash and switch costs from production and shipment rows."""
    kpis = production_kpis(problem, plan)
    return {'kpis': kpis, 'plan': {
        key: kpis[key] for key in ('schedule', 'shipments', 'inventory', 'cash_curve')
    }}


def baseline(problem):
    """Place whole orders in EDD order, retrying later production starts."""
    schedule = {}
    shipments = []
    orders = sorted(problem.orders.values(), key=lambda order: (
        order['due_day'], not order['is_export'],
    ))
    for order in orders:
        sku = order['sku_id']
        line = max(problem.lines, key=lambda line: problem.rates.get((line, sku), 0), default=None)
        rate = problem.rates.get((line, sku), 0)
        stock = production_kpis(problem, {'schedule': list(schedule.values()), 'shipments': shipments})[
            'inventory'
        ][sku]
        for start in problem.days:
            trial = schedule.copy()
            produced = 0
            for day in problem.days:
                slot = trial.get((line, day))
                if day >= start and line is not None and day not in problem.down[line] and (
                    slot is None or slot['sku'] == sku
                ):
                    previous = next(
                        (trial[line, prior_day] for prior_day in range(day - 1, -1, -1)
                         if (line, prior_day) in trial),
                        None,
                    )
                    switch_loss = problem.lines[line]['changeover_loss_units'] if (
                        previous and previous['sku'] != sku
                    ) else 0
                    existing = slot['qty'] if slot else 0
                    extra = min(rate - switch_loss - existing, order['qty'] - stock[day] - produced)
                    if extra > 0:
                        trial[line, day] = {'line': line, 'day': day, 'sku': sku, 'qty': existing + extra}
                        produced += extra
                if stock[day] + produced < order['qty']:
                    continue
                shipment = {'order': order['id'], 'day': day}
                candidate = {'schedule': list(trial.values()), 'shipments': [*shipments, shipment]}
                kpis = production_kpis(problem, candidate)
                if kpis['feasible'] and kpis['credit_ok']:
                    schedule = trial
                    shipments.append(shipment)
                    break
            else:
                continue
            break

    plan = {'schedule': list(schedule.values()), 'shipments': shipments}
    return plan if production_kpis(problem, plan)['feasible'] else None


def build_model(problem, weights, hint):
    model = cp_model.CpModel()
    quantities, used, setups, switches = {}, {}, {}, {}
    inventory, ship, skipped, overdraft, breach = {}, {}, {}, {}, {}
    sku_codes = {sku: code for code, sku in enumerate(problem.skus, start=1)}
    initial_setup = model.new_constant(0)  # No last-run SKU at the start of the horizon.

    for line, specs in problem.lines.items():
        previous_setup = initial_setup
        for day in problem.days:
            today = {}
            for (rated_line, sku), rate in problem.rates.items():
                if rated_line != line or day in problem.down[line]:
                    continue
                key = (line, day, sku)
                used[key] = today[sku] = model.new_bool_var(f'run[{line},{day},{sku}]')
                quantities[key] = model.new_int_var(0, rate, f'qty[{line},{day},{sku}]')
                model.add(quantities[key] <= rate * used[key])
                model.add(quantities[key] >= used[key])
            if not today:
                continue
            model.add_at_most_one(today.values())
            setup = setups[line, day] = model.new_int_var(0, len(sku_codes), f'setup[{line},{day}]')
            change = switches[line, day] = model.new_bool_var(f'change[{line},{day}]')
            model.add(setup == previous_setup).only_enforce_if([~active for active in today.values()])
            model.add(change <= sum(today.values()))
            for sku, active in today.items():
                code = sku_codes[sku]
                model.add(setup == code).only_enforce_if(active)
                model.add_linear_expression_in_domain(
                    previous_setup, cp_model.Domain.from_values([0, code]),
                ).only_enforce_if(active, ~change)
                model.add(previous_setup != 0).only_enforce_if(active, change)
                model.add(previous_setup != code).only_enforce_if(active, change)
                model.add(
                    quantities[line, day, sku]
                    <= problem.rates[line, sku] - specs['changeover_loss_units'] * change
                ).only_enforce_if(active)
            previous_setup = setup

    for order in problem.orders:
        skipped[order] = model.new_bool_var(f'unshipped[{order}]')
        for day in problem.days:
            ship[order, day] = model.new_bool_var(f'ship[{order},{day}]')
        model.add_exactly_one([skipped[order], *(ship[order, day] for day in problem.days)])

    produced = {(sku, day): sum(quantities[line, day, sku] for line in problem.lines
                                if (line, day, sku) in quantities)
                for sku in problem.skus for day in problem.days}

    cash = problem.opening_cash
    spend_bound = 0
    for day in problem.days:
        for sku, specs in problem.skus.items():
            storage_bound = problem.storage // specs['volume'] if specs['volume'] else (
                specs['initial_inventory'] + (day + 1) * sum(
                    problem.rates.get((line, sku), 0) for line in problem.lines
                )
            )
            inventory[sku, day] = model.new_int_var(0, storage_bound, f'stock[{sku},{day}]')
            prior = inventory[sku, day - 1] if day else specs['initial_inventory']
            model.add(inventory[sku, day] == prior + produced[sku, day] - sum(
                order['qty'] * ship[order_id, day] for order_id, order in problem.orders.items()
                if order['sku_id'] == sku
            ))
        model.add(sum(specs['volume'] * inventory[sku, day]
                      for sku, specs in problem.skus.items()) <= problem.storage)

        cash -= sum(specs['unit_cost'] * produced[sku, day] for sku, specs in problem.skus.items())
        if day >= problem.lag:
            cash += sum(order['qty'] * problem.skus[order['sku_id']]['unit_price']
                        * ship[order_id, day - problem.lag] for order_id, order in problem.orders.items())
        spend_bound += sum(max(
            (problem.rates[line, sku] * problem.skus[sku]['unit_cost'] for sku in problem.skus
             if (line, day, sku) in quantities), default=0,
        ) for line in problem.lines)
        od_limit = max(0, spend_bound - problem.opening_cash)
        overdraft[day] = model.new_int_var(0, od_limit, f'overdraft[{day}]')
        breach[day] = model.new_int_var(0, max(0, od_limit - problem.credit), f'breach[{day}]')
        model.add_max_equality(overdraft[day], [0, -cash])
        model.add_max_equality(breach[day], [0, overdraft[day] - problem.credit])

    # Scale SAR by 70,000, so one halala is integral and the demo's
    # weekly rate of 0.004 / 7 is exact. Round weighted coefficients once.
    scale = 70_000
    objective_terms = []

    def charge(variable, amount, weight):
        objective_terms.append((variable, round(amount * scale * Decimal(str(weights[weight])))))

    for order_id, order in problem.orders.items():
        charge(skipped[order_id], problem.unshipped_penalty(order), 'unshipped')
        for day in range(order['due_day'] + 1, problem.horizon):
            charge(ship[order_id, day], order['penalty_per_day'] * (day - order['due_day']), 'lateness')
    for (line, _day), variable in switches.items():
        charge(variable, problem.lines[line]['changeover_cost'], 'changeover')
    for (sku, _day), variable in inventory.items():
        charge(variable, Decimal(problem.skus[sku]['holding_cost_per_day']) / 100, 'holding')
    for day in problem.days:
        charge(overdraft[day], problem.daily_interest, 'financing')
        objective_terms.append((breach[day], 1_000_000 * scale))
    objective = sum(variable * cost for variable, cost in objective_terms)
    model.minimize(objective)

    hint_score = None
    if hint is not None:
        rows = {(row['line'], row['day']): row for row in hint['schedule']}
        shipped = {row['order']: row['day'] for row in hint['shipments']}
        kpis = production_kpis(problem, hint)
        # Use the same definition as the KPI: a late order shipped after its due date.
        model.add(sum(variable for (order, day), variable in ship.items()
                      if day > problem.orders[order]['due_day']) <= kpis['late_orders'])
        values = {}

        def add_hint(variable, value):
            model.add_hint(variable, value)
            values[variable.index] = value

        add_hint(initial_setup, 0)
        for (line, day, sku), variable in quantities.items():
            row = rows.get((line, day))
            qty = row['qty'] if row and row['sku'] == sku else 0
            add_hint(variable, qty)
            add_hint(used[line, day, sku], int(qty > 0))
        last_setup = dict.fromkeys(problem.lines, 0)
        for (line, day), variable in setups.items():
            if row := rows.get((line, day)):
                last_setup[line] = sku_codes[row['sku']]
            add_hint(variable, last_setup[line])
        changed = {(row['line'], row['day']) for row in kpis['changeover_days']}
        for key, variable in switches.items():
            add_hint(variable, int(key in changed))
        for (sku, day), variable in inventory.items():
            add_hint(variable, kpis['inventory'][sku][day])
        for (order, day), variable in ship.items():
            add_hint(variable, int(shipped.get(order) == day))
        for order, variable in skipped.items():
            add_hint(variable, int(order not in shipped))
        for day, balance in enumerate(kpis['cash_curve']):
            add_hint(overdraft[day], max(0, -balance))
            add_hint(breach[day], max(0, -balance - problem.credit))
        hint_score = sum(values[var.index] * cost for var, cost in objective_terms)

    return model, quantities, ship, hint_score


def solve(dataset, parameters=None, weights=None, time_limit=None):
    defaults = load_templates()['production'].model_defaults()
    problem = prepare(dataset, defaults['parameters'] | (parameters or {}))
    weights = defaults['weights'] | (weights or {})
    time_limit = Settings().solver_time_limit if time_limit is None else time_limit
    if not isfinite(time_limit) or time_limit <= 0:
        raise OptimizationInputError('وقت التحسين لازم يكون أكبر من صفر.')

    initial = baseline(problem)
    try:
        model, quantities, ship, hint_score = build_model(problem, weights, initial)
    except (OverflowError, ValueError, TypeError):
        raise OptimizationInputError('القيم الرقمية أكبر من النطاق المدعوم للتحسين.') from None
    if model.validate():
        raise OptimizationInputError('القيم الرقمية أكبر من النطاق المدعوم للتحسين.')

    solver = configured_solver(time_limit)
    solver.parameters.random_seed = 7
    solver.parameters.num_workers = 1
    # End the usual search on repeatable work units, with the wall limit as a backstop.
    solver.parameters.max_deterministic_time = 0.8 * time_limit
    # Generous wall backstop so the deterministic limit binds even on a busy machine.
    solver.parameters.max_time_in_seconds = 3 * time_limit
    status = solver.solve(model)
    chosen = objective = bound = None
    if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        chosen = {
            'schedule': [{'line': line, 'day': day, 'sku': sku, 'qty': qty}
                         for (line, day, sku), variable in quantities.items()
                         if (qty := solver.value(variable)) > 0],
            'shipments': [{'order': order, 'day': day} for (order, day), variable in ship.items()
                          if solver.value(variable)],
        }
        objective = solver.objective_value
        bound = solver.best_objective_bound
    if initial is not None and (
        status == cp_model.UNKNOWN or (objective is not None and hint_score < objective)
    ):
        chosen, objective, status = initial, hint_score, cp_model.FEASIBLE

    empty = {'kpis': {}, 'plan': {}}
    return {
        'baseline': empty if initial is None else evaluate(problem, initial),
        'optimized': empty if chosen is None else evaluate(problem, chosen),
        'solver': {'status': solver.status_name(status), 'objective': objective,
                   'bound': bound, 'wall_s': solver.wall_time},
        'explanation_ar': '',
    }


def solve_demo(**kwargs):
    return solve(load_demo('production'), **kwargs)
