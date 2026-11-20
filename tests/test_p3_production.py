import pytest
from ortools.sat.python import cp_model

from app.ingestion.loader import load_demo
from app.optimization import production
from app.templates_registry import load_templates


@pytest.fixture(scope='module')
def demo():
    return load_demo('production')


@pytest.fixture(scope='module')
def problem(demo):
    return production.prepare(demo, load_templates()['production'].model_defaults()['parameters'])


@pytest.fixture(scope='module')
def result(demo):
    return production.solve(demo)


@pytest.mark.parametrize('variant', ['baseline', 'optimized'])
def test_demo_capacity_and_stock(problem, result, variant):
    plan = result[variant]['plan']
    slots = {(row['line'], row['day']): row for row in plan['schedule']}
    assert len(slots) == len(plan['schedule'])
    last_sku = {}
    for (line, day), row in slots.items():
        prior = last_sku.get(line)
        loss = problem.lines[line]['changeover_loss_units'] if prior is not None and prior != row['sku'] else 0
        assert 0 < row['qty'] <= problem.rates[line, row['sku']] - loss
        last_sku[line] = row['sku']
    stock = {sku: specs['initial_inventory'] for sku, specs in problem.skus.items()}
    assert len({row['order'] for row in plan['shipments']}) == len(plan['shipments'])
    for day in problem.days:
        for row in plan['schedule']:
            if row['day'] == day:
                stock[row['sku']] += row['qty']
        for row in plan['shipments']:
            if row['day'] == day:
                order = problem.orders[row['order']]
                stock[order['sku_id']] -= order['qty']
                assert row['late_days'] == max(0, day - order['due_day'])
        assert all(plan['inventory'][sku][day] == qty >= 0 for sku, qty in stock.items())
        assert sum(problem.skus[sku]['volume'] * qty for sku, qty in stock.items()) <= problem.storage
    assert result[variant]['kpis']['feasible']


@pytest.mark.parametrize('variant', ['baseline', 'optimized'])
def test_demo_credit_and_payment_lag(problem, result, variant):
    kpis = result[variant]['kpis']
    assert kpis['credit_ok'] and kpis['breach'] == 0
    assert kpis['peak_od'] <= problem.credit
    balance = problem.opening_cash
    for day in problem.days:
        balance -= sum(problem.skus[row['sku']]['unit_cost'] * row['qty']
                       for row in kpis['schedule'] if row['day'] == day)
        for shipment in kpis['shipments']:
            if shipment['day'] + problem.lag == day:
                order = problem.orders[shipment['order']]
                balance += order['qty'] * problem.skus[order['sku_id']]['unit_price']
        assert balance == kpis['cash_curve'][day]


def test_demo_cost_improves(result):
    assert result['optimized']['kpis']['total_cost'] <= result['baseline']['kpis']['total_cost']


def test_demo_late_orders_do_not_increase(result):
    assert result['optimized']['kpis']['late_orders'] <= result['baseline']['kpis']['late_orders']


@pytest.mark.parametrize('variant', ['baseline', 'optimized'])
def test_changeovers_match_recount(problem, result, variant):
    kpis = result[variant]['kpis']
    changes = []
    for line in problem.lines:
        runs = sorted((row for row in kpis['schedule'] if row['line'] == line), key=lambda row: row['day'])
        changes.extend((line, run['day']) for previous, run in zip(runs, runs[1:])
                       if previous['sku'] != run['sku'])
    assert kpis['changeovers'] == len(changes)
    assert kpis['changeover_cost'] == sum(problem.lines[line]['changeover_cost'] for line, _ in changes)


def test_daily_runtime(result):
    assert result['solver']['status'] in ('OPTIMAL', 'FEASIBLE')
    assert result['solver']['wall_s'] < 20


def test_demo_is_reproducible():
    first, second = production.solve_demo(), production.solve_demo()
    assert first['optimized']['kpis']['total_cost'] == second['optimized']['kpis']['total_cost']
    assert first['optimized']['plan'] == second['optimized']['plan']


def test_line_down(demo):
    result = production.solve(demo, parameters={'line_down': {'L1': [3, 4]}}, time_limit=2)
    for variant in ('baseline', 'optimized'):
        plan = result[variant]['plan']
        assert plan['schedule']
        assert all(row['line'] != 'L1' or row['day'] not in (3, 4) for row in plan['schedule'])


def test_demand_increase(demo, result):
    raised = {**demo, 'orders': [{**row, 'qty': round(row['qty'] * 1.2)} for row in demo['orders']]}
    increased = production.solve(raised)
    assert increased['optimized']['kpis']['total_cost'] >= result['optimized']['kpis']['total_cost']


def test_complete_hint_and_time_limit(problem, demo):
    hint = production.baseline(problem)
    model, _, _, score = production.build_model(
        problem, load_templates()['production'].model_defaults()['weights'], hint,
    )
    assert len(model.proto.solution_hint.vars) == len(model.proto.variables)
    assert score > 0
    result = production.solve(demo, time_limit=0.001)
    assert result['solver']['status'] in ('OPTIMAL', 'FEASIBLE')
    assert result['optimized']['kpis']['total_cost'] <= result['baseline']['kpis']['total_cost']


def test_recount_switch_vs_idle_and_breach(demo):
    params = load_templates()['production'].model_defaults()['parameters']
    problem = production.prepare(demo, {
        **params, 'credit_limit': 0, 'opening_cash': 0, 'cold_capacity': 30_000,
    })
    kpis = production.evaluate(problem, {'schedule': [
        {'line': 'L1', 'day': 0, 'sku': 'S1', 'qty': 10},
        {'line': 'L1', 'day': 1, 'sku': 'S2', 'qty': 3900},
        {'line': 'L1', 'day': 3, 'sku': 'S1', 'qty': 5000},
    ], 'shipments': []})['kpis']
    assert not kpis['feasible']  # Day 3 exceeds capacity after the changeover loss.
    assert kpis['changeover_days'] == [{'line': 'L1', 'day': 1}, {'line': 'L1', 'day': 3}]
    assert not kpis['credit_ok']
    assert kpis['breach'] == kpis['peak_od'] == 48_450
    assert kpis['unshipped'] == len(problem.orders)


@pytest.mark.parametrize('down_days', [[], [1]])
def test_changeover_after_idle_day(demo, down_days):
    defaults = load_templates()['production'].model_defaults()
    problem = production.prepare(demo, {**defaults['parameters'], 'line_down': {'L1': down_days}})
    line = problem.lines['L1']
    capacity = problem.rates['L1', 'S2'] - line['changeover_loss_units']

    for extra, expected in [(0, cp_model.OPTIMAL), (1, cp_model.INFEASIBLE)]:
        runs = {('L1', 0, 'S1'): 10, ('L1', 2, 'S2'): capacity + extra}
        schedule = [{'line': line_id, 'day': day, 'sku': sku, 'qty': qty}
                    for (line_id, day, sku), qty in runs.items()]
        kpis = production.evaluate(problem, {'schedule': schedule, 'shipments': []})['kpis']
        assert kpis['changeover_days'] == [{'line': 'L1', 'day': 2}]
        assert kpis['changeover_cost'] == line['changeover_cost']
        assert kpis['feasible'] == (extra == 0)

        weights = dict.fromkeys(defaults['weights'], 0) | {'changeover': 1}
        model, quantities, ship, _ = production.build_model(problem, weights, None)
        for key, variable in quantities.items():
            model.add(variable == runs.get(key, 0))
        for variable in ship.values():
            model.add(variable == 0)
        solver = cp_model.CpSolver()
        solver.parameters.num_workers = 1
        assert solver.solve(model) == expected
        if extra == 0:
            assert solver.objective_value == line['changeover_cost'] * 70_000


def test_impossible_storage_returns_infeasible(demo):
    result = production.solve(demo, parameters={'cold_capacity': 0}, time_limit=2)
    assert result['solver']['status'] == 'INFEASIBLE'
    assert result['optimized'] == {'kpis': {}, 'plan': {}}


def test_edd_export_wins_due_date_tie():
    dataset = {
        'lines': [{'id': 'L', 'changeover_cost': 0, 'changeover_loss_units': 0}],
        'skus': [{'id': 'S', 'unit_cost': 0, 'unit_price': 1, 'volume': 1,
                  'initial_inventory': 0, 'holding_cost_per_day': 0}],
        'rates': [{'line_id': 'L', 'sku_id': 'S', 'units_per_day': 1}],
        'orders': [{'id': order, 'sku_id': 'S', 'qty': 1, 'due_day': 0,
                    'penalty_per_day': 1, 'is_export': export}
                   for order, export in [('local', False), ('export', True)]],
    }
    params = load_templates()['production'].model_defaults()['parameters']
    problem = production.prepare(dataset, {**params, 'horizon_days': 1, 'cold_capacity': 0})
    baseline = production.evaluate(problem, production.baseline(problem))
    assert baseline['plan']['shipments'] == [{'order': 'export', 'day': 0, 'late_days': 0}]
    assert baseline['kpis']['unshipped_orders'] == ['local']


def test_production_api_persists_result(client):
    path = '/api/companies/2'
    dataset_id = client.get(path + '/datasets').json()[0]['id']
    edit = client.put(path + '/model', json={'parameters': {'line_down': {'L1': [3, 4]}}})
    assert edit.status_code == 200
    response = client.post(path + '/optimize', json={
        'dataset_id': dataset_id, 'time_limit': 0.2, 'weights': {'holding': 0.5},
    })
    assert response.status_code == 200
    result = response.json()
    assert result['solver']['status'] in ('OPTIMAL', 'FEASIBLE')
    assert result['explanation_ar'].strip()
    plan = result['optimized']['plan']
    assert plan['schedule'] and plan['shipments']
    assert all(row['line'] != 'L1' or row['day'] not in (3, 4) for row in plan['schedule'])
    run = client.get(f"/api/runs/{result.pop('run_id')}").json()
    assert run['result'] == result
    assert run['model_id'] == edit.json()['id']
    assert run['dataset_id'] == dataset_id
    assert client.get(path + '/runs/latest?kind=optimize').json() == run


def test_production_api_rejects_invalid_line_down(client):
    path = '/api/companies/2'
    assert client.put(path + '/model', json={
        'parameters': {'line_down': {'L9': [3, 4]}},
    }).status_code == 200
    response = client.post(path + '/optimize')
    assert response.status_code == 422
    assert response.json()['detail']['code'] == 'invalid_optimization_input'
    assert 'L9' in response.json()['detail']['msg_ar']
