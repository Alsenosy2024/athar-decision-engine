"""Dated weekly fixture and a common production/dispatch evaluator for H3."""
from datetime import date, timedelta
from time import perf_counter

import numpy as np
from ortools.sat.python import cp_model

from app.ingestion.loader import load_demo
from app.intelligence.forecasting import stat_prediction
from app.optimization import production
from app.templates_registry import load_templates

SEED = 7
WORKERS = 1
WALL_SECONDS = 1.0
DETERMINISTIC_SECONDS = 0.10
DEMO_START = date(2025, 1, 6)
PRODUCTS = ('S1', 'S2')
FIRST_TRAIN = 17
FIRST_TEST = 37
LAST_TEST = 52
DRAW_COUNT = 8
TAIL_LEVEL = 0.90
CANDIDATES = ('current', 'stat', 'winner', 'lower', 'upper')
COST_SCALE = 70_000  # Matches production.build_model with unit weights.


def demo_case():
    demo = load_demo('production')
    defaults = load_templates()['production'].model_defaults()
    supply = {
        'lines': [row for row in demo['lines'] if row['id'] == 'L1'],
        'rates': [row for row in demo['rates'] if row['line_id'] == 'L1'
                  and row['sku_id'] in PRODUCTS],
        'skus': [row for row in demo['skus'] if row['id'] in PRODUCTS],
    }
    docket = {sku: next(row for row in demo['orders'] if row['sku_id'] == sku)
              for sku in PRODUCTS}
    history = {(row['week'], row['sku_id']): row['qty'] for row in demo['demand_history']
               if row['sku_id'] in PRODUCTS}
    parameters = defaults['parameters'] | {'horizon_days': 7}
    return supply, docket, history, parameters, defaults['weights']


def date_for(week):
    return (DEMO_START + timedelta(weeks=week - 1)).isoformat()


def point_models(values):
    return {
        'stat': float(stat_prediction(values, 1, True)[1, 0]),
        'last_week': float(values[-1]),
        'trailing_median_4': float(np.median(values[-4:])),
    }


def dated_inputs(history, docket, week):
    """Generate policy inputs with a strict as-of cutoff. Read no outcome rows."""
    past = np.array([[history[w, sku] for sku in PRODUCTS] for w in range(1, week)])
    predictions = [point_models(past[:, col]) for col in range(len(PRODUCTS))]
    # Adjacent, one-step selection forecasts all precede the decision origin.
    indices = range(len(past) - 8, len(past))
    historical = [[point_models(past[:index, col]) for col in range(len(PRODUCTS))]
                  for index in indices]
    scores = [{name: float(np.mean([abs(row[col][name] - past[i, col])
                                  for i, row in zip(indices, historical)]))
               for name in predictions[col]} for col in range(len(PRODUCTS))]
    winners = [min(score, key=score.get) for score in scores]
    central = np.array([row[name] for row, name in zip(predictions, winners)])
    residuals = np.array([past[i] - [row[col][name] for col, name in enumerate(winners)]
                          for i, row in zip(indices, historical)])
    # Joint weekly residual vectors retain the dependence between products.
    rng = np.random.default_rng(np.random.SeedSequence([SEED, week]))
    draw_indices = rng.choice(len(residuals), DRAW_COUNT, replace=True)
    draws = central + residuals[draw_indices]
    lower, upper = np.quantile(residuals, [0.25, 0.75], axis=0) + central
    confirmed = [docket[sku]['qty'] // 4 for sku in PRODUCTS]

    def totals(raw):
        return [max(qty, round(value)) for qty, value in zip(confirmed, raw)]

    return {
        'week': week, 'cutoff_exclusive': date_for(week), 'last_observed_week': date_for(week - 1),
        'outcome_window': [date_for(week), date_for(week + 1)],
        'history_totals': past.tolist(), 'confirmed_units': confirmed,
        'model_mae_units': scores, 'winners': winners, 'draw_indices': draw_indices.tolist(),
        'draw_totals': [totals(draw) for draw in draws],
        'candidate_totals': dict(zip(CANDIDATES, (
            totals([docket[sku]['qty'] for sku in PRODUCTS]),
            totals([row['stat'] for row in predictions]), totals(central), totals(lower), totals(upper),
        ))),
    }


def prepare(supply, docket, parameters, confirmed, totals):
    orders = []
    for sku, booked, total in zip(PRODUCTS, confirmed, totals):
        row = docket[sku]
        orders.append(row | {'id': f'{sku}-confirmed', 'qty': booked})
        # Confirmed bookings stay fixed. The residual lot is known only in recourse.
        if total > booked:
            orders.append(row | {'id': f'{sku}-open', 'qty': total - booked})
    return production.prepare(supply | {'orders': orders}, parameters)


def solve(problem, weights, fixed_schedule=None):
    """Lock fabrication in recourse. Dispatch can adapt with perfect weekly information."""
    start = perf_counter()
    model, quantities, ship, _ = production.build_model(problem, weights, hint=None)
    if fixed_schedule is not None:
        slots = {(row['line'], row['day'], row['sku']): row['qty'] for row in fixed_schedule}
        for key, variable in quantities.items():
            model.add(variable == slots.get(key, 0))

    # Match production's daily cash ledger and enforce hard eligibility in this benchmark.
    cash = problem.opening_cash
    for day in problem.days:
        cash -= sum(problem.skus[sku]['unit_cost'] * qty
                    for (_, run_day, sku), qty in quantities.items() if run_day == day)
        if day >= problem.lag:
            cash += sum(order['qty'] * problem.skus[order['sku_id']]['unit_price']
                        * ship[oid, day - problem.lag] for oid, order in problem.orders.items())
        model.add(cash >= -problem.credit)
    for oid, order in problem.orders.items():
        if oid.endswith('-confirmed'):
            model.add(sum(ship[oid, day] for day in range(order['due_day'] + 1)) == 1)

    solver = cp_model.CpSolver()
    solver.parameters.random_seed = SEED
    solver.parameters.num_workers = WORKERS
    solver.parameters.max_time_in_seconds = WALL_SECONDS
    solver.parameters.max_deterministic_time = DETERMINISTIC_SECONDS
    status = solver.solve(model)
    elapsed = perf_counter() - start
    record = {'status': solver.status_name(status), 'elapsed_s': elapsed}
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return record | dict.fromkeys(('schedule', 'shipments', 'cost_sar', 'bound_sar',
                                       'gap_pct', 'service_pct', 'credit_breach_sar',
                                       'peak_overdraft_sar', 'hard_checks'))
    schedule = [{'line': line, 'day': day, 'sku': sku, 'qty': qty}
                for (line, day, sku), var in quantities.items() if (qty := solver.value(var))]
    shipments = [{'order': oid, 'day': day}
                 for (oid, day), var in ship.items() if solver.value(var)]
    kpis = production.evaluate(problem, {'schedule': schedule, 'shipments': shipments})['kpis']
    on_time = {row['order'] for row in kpis['shipments'] if row['late_days'] == 0}
    mandatory = {oid for oid in problem.orders if oid.endswith('-confirmed')}
    hard_checks = {'physical_feasible': kpis['feasible'], 'credit_ok': kpis['credit_ok'],
                   'confirmed_on_time': mandatory <= on_time}
    if not all(hard_checks.values()):
        raise RuntimeError('Benchmark solution failed independent eligibility checks')
    cost = solver.objective_value / COST_SCALE
    bound = solver.best_objective_bound / COST_SCALE
    if abs(cost - kpis['total_cost']) > 0.011:
        raise RuntimeError('Benchmark solver objective diverged from production cash/cost ledger')
    return record | {
        'schedule': schedule, 'shipments': shipments,
        'cost_sar': cost, 'bound_sar': bound,
        'gap_pct': 100 * max(0, cost - bound) / max(abs(cost), 1),
        'service_pct': 100 * sum(problem.orders[oid]['qty'] for oid in on_time)
        / sum(row['qty'] for row in problem.orders.values()),
        'credit_breach_sar': kpis['breach'], 'peak_overdraft_sar': kpis['peak_od'],
        'hard_checks': hard_checks,
    }


def cvar(costs, level=TAIL_LEVEL):
    """Mean of the worst (1-level) fraction, including fractional boundary mass."""
    ordered = sorted(costs, reverse=True)
    mass = len(ordered) * (1 - level)
    return sum(cost * min(1, max(0, mass - index))
               for index, cost in enumerate(ordered)) / mass
