"""Use the production CP-SAT constraints for bounded candidate generation and replanning."""
from ortools.sat.python import cp_model

from app.optimization.common import OptimizationInputError
from app.optimization.kpis import production_kpis
from app.optimization.production import build_model

SEED = 7
WORKERS = 1
SOLVE_SECONDS = 0.5


def prefix(plan, day):
    return {
        'schedule': sorted((row for row in plan['schedule'] if row['day'] < day),
                           key=lambda row: (row['day'], row['line'])),
        'shipments': sorted((row for row in plan['shipments'] if row['day'] < day),
                            key=lambda row: (row['day'], row['order'])),
    }


def solve_candidate(problem, release, weights, objective, seconds, *, committed=None, day=0,
                    hint=None, hard_credit=False, different_from=None, min_on_time=None):
    if seconds <= 0:
        return {'status': 'UNKNOWN', 'wall_s': 0.0, 'objective': None, 'bound': None}, None
    try:
        complete_hint = None
        if hint is not None:
            kpis = production_kpis(problem, hint)
            if (kpis['feasible'] and (not hard_credit or kpis['credit_ok'])
                    and prefix(hint, day) == (committed or prefix(hint, 0))
                    and all(row['day'] >= release[row['order']] and problem.orders[row['order']]['qty'] > 0
                            for row in hint['shipments'])):
                complete_hint = hint
        model, quantities, shipments, _ = build_model(problem, weights, complete_hint,
                                                        preserve_hint_lateness=False)
        run_rows = {(row['line'], row['day'], row['sku']): row['qty']
                    for row in (committed or {}).get('schedule', [])}
        ship_rows = {(row['order'], row['day']) for row in (committed or {}).get('shipments', [])}
        if (run_rows.keys() - quantities.keys() or ship_rows - shipments.keys()
                or any(slot < release[key] for key, slot in ship_rows)):
            raise OptimizationInputError('الالتزام المثبّت غير متاح في نموذج التشغيل.')
        for key, variable in quantities.items():
            if key[1] < day:
                model.add(variable == run_rows.get(key, 0))
        for (key, slot), variable in shipments.items():
            if slot < day or slot < release[key] or problem.orders[key]['qty'] == 0:
                model.add(variable == int((key, slot) in ship_rows))

        if different_from is not None:
            known = {(row['order'], row['day']) for row in different_from['shipments']}
            model.add_bool_or(~variable if key in known else variable
                              for key, variable in shipments.items())
        on_time = sum(problem.orders[key]['qty'] * variable
                      for (key, slot), variable in shipments.items() if slot <= problem.orders[key]['due_day'])
        if min_on_time is not None:
            model.add(on_time >= min_on_time)
        if objective == 'service':
            model.maximize(on_time)
        overdrafts = [model.get_int_var_from_proto_index(index)
                      for index, var in enumerate(model.proto.variables) if var.name.startswith('overdraft[')]
        if hard_credit:
            for overdraft in overdrafts:
                model.add(overdraft <= problem.credit)
        if objective == 'cash':
            # Keep the cost plan's service where feasible, then minimize peak financing.
            peak = model.new_int_var(0, max(list(var.proto.domain)[-1] for var in overdrafts), 'peak')
            model.add_max_equality(peak, overdrafts)
            if complete_hint is not None:
                model.add_hint(peak, kpis['peak_od'])
            if hint:
                target = sum(problem.orders[row['order']]['qty'] for row in hint['shipments']
                             if row['day'] <= problem.orders[row['order']]['due_day'])
                model.add(on_time >= target * 9 // 10)
            model.minimize(peak)

        if hint is not None and complete_hint is None:
            runs = {(row['line'], row['day'], row['sku']): row['qty'] for row in hint['schedule']}
            ships = {(row['order'], row['day']) for row in hint['shipments']}
            for key, variable in quantities.items():
                model.add_hint(variable, run_rows.get(key, 0) if key[1] < day else runs.get(key, 0))
            for key, variable in shipments.items():
                model.add_hint(variable, int(key in (ship_rows if key[1] < day else ships)))
        if model.validate():
            raise ValueError('invalid CP-SAT model')
    except (OverflowError, TypeError, ValueError) as error:
        if isinstance(error, OptimizationInputError):
            raise
        raise OptimizationInputError('القيم الرقمية أكبر من نطاق نموذج التجربة.') from None

    solver = cp_model.CpSolver()
    solver.parameters.random_seed = SEED
    solver.parameters.num_workers = WORKERS
    solver.parameters.max_time_in_seconds = seconds
    # The deterministic quota stops normal runs before the safety wall cap.
    solver.parameters.max_deterministic_time = seconds * 0.16
    solver.parameters.repair_hint = complete_hint is None
    status = solver.solve(model)
    solved = status in (cp_model.OPTIMAL, cp_model.FEASIBLE)
    meta = {'status': solver.status_name(status), 'wall_s': solver.wall_time,
            'objective': solver.objective_value if solved else None,
            'bound': solver.best_objective_bound if solved else None}
    if not solved:
        return meta, None
    return meta, {
        'schedule': [{'line': line, 'day': slot, 'sku': sku, 'qty': qty}
                     for (line, slot, sku), var in quantities.items() if (qty := solver.value(var))],
        'shipments': [{'order': key, 'day': slot} for (key, slot), var in shipments.items()
                      if solver.value(var)],
    }
