from collections import Counter
from itertools import product

import numpy as np
import pytest

from app.decision_loop.api import PlanMetrics, ProductionExperiment
from app.decision_loop.candidates import SOLVE_SECONDS, prefix, solve_candidate
from app.decision_loop.evaluation import assess, cvar, dispatch, measure
from app.decision_loop.frontier import shared_frontier
from app.decision_loop.rolling import ledger, rolling
from app.decision_loop.scenarios import Demand, draw_paths, reduced_sample
from app.decision_loop.service import experiment, select
from app.ingestion.loader import load_demo
from app.optimization.budget import SolveBudget
from app.optimization.joint_allocation import Allocation
from app.optimization.kpis import production_kpis
from app.optimization.production import prepare
from app.templates_registry import load_templates


@pytest.fixture(scope='module')
def demo():
    dataset = load_demo('production')
    config = load_templates()['production'].model_defaults()
    base = prepare(dataset, config['parameters'])
    return dataset, config, base


@pytest.fixture(scope='module')
def demo_run(demo):
    dataset, config, _ = demo
    return experiment(dataset, config['parameters'], config['weights'],
                      ProductionExperiment(rolling=True), hard_credit=True)


def test_p1a_correlated_scenario_reduction_and_confirmed_orders(demo):
    _, _, base = demo
    demand = Demand.from_problem(base, 0.10)
    paths = draw_paths(demand, 512, 7)
    assert paths == draw_paths(demand, 512, 7)
    a, b = demand.forecast[0], demand.forecast[-1]
    assert demand.release[a] < demand.release[b]
    assert np.corrcoef([row[a] for row in paths], [row[b] for row in paths])[0, 1] > 0.25
    selected = reduced_sample(paths, 16)
    assert len({idx for idx, _ in selected}) == 16
    assert sum(mass for _, mass in selected) == pytest.approx(1)
    assert all(demand.problem(base, row).orders[key] == order
               for row in paths for key, order in base.orders.items())


def test_p1a_fractional_cvar_changes_choice_and_excludes_ineligible_tradeoff():
    assert cvar([0, 10, 40], [0.5, 0.4, 0.1], 0.75) == pytest.approx(22)
    scales = {'cost_sar': 100, 'service_loss_pct': 100, 'peak_credit_sar': 100}

    def grade(tail_weight, costs, *, breach=0):
        rows = [{'cost_sar': cost, 'service_loss_pct': 10, 'peak_credit_sar': 5,
                 'credit_breach_sar': breach, 'physical_ok': True,
                 'holding_and_setup_sar': 2, 'financing_sar': 1} for cost in costs]
        return assess(rows, [0.9, 0.1], ProductionExperiment(tail_weight=tail_weight), scales, True)

    for weight, winner in ((0, 'cheap_in_mean'), (1, 'stable')):
        candidates = [
            {'id': 'cheap_in_mean', 'assessment': grade(weight, [10, 100])},
            {'id': 'stable', 'assessment': grade(weight, [24, 24])},
            {'id': 'breaks_credit', 'assessment': grade(weight, [0, 0], breach=1)},
        ]
        assert select(candidates, 'assessment')['id'] == winner


def test_p1a_demonstration_keeps_solver_evidence_and_full_sample_guard(demo, demo_run):
    assert demo_run['settings']['seed'] == 7
    assert demo_run['settings']['workers'] == 1
    assert len(demo_run['candidates']) >= 3
    assert len({row['scenario_fingerprint'] for row in demo_run['candidates']}) > 1
    assert len({row['version'] for row in demo_run['candidates']}) == len(demo_run['candidates'])
    assert demo_run['selected_id'] == select(demo_run['candidates'], 'assessment')['id']
    assert len(demo_run['frontier']) >= 2
    demand = Demand.from_problem(demo[2], demo_run['policy']['forecast_uplift'])
    paths = draw_paths(demand, demo_run['scenario_set']['full_count'], demo_run['settings']['seed'])
    for row in demo_run['candidates']:
        cases = [demand.problem(demo[2], path) for path in paths]
        costs = [measure(case, dispatch(case, row['plan']))['cost_sar'] for case in cases]
        assert row['p50_cost_sar'] == pytest.approx(np.percentile(costs, 50))
    for point in demo_run['frontier']:
        row = next(row for row in demo_run['candidates'] if row['id'] == point['candidate_id'])
        assert row['assessment']['breaches']['physical']['max_excess'] == 0
        assert row['assessment']['breaches']['credit']['max_excess'] == 0
        assert point['solver']['status'] in {'OPTIMAL', 'FEASIBLE'}
        assert point['version'] == row['version']


def test_p2a_first_shift_stays_frozen_and_future_quantities_do_not_leak(demo, demo_run):
    _, config, base = demo
    demand = Demand.from_problem(base, 0.10)
    first = select(demo_run['candidates'], 'assessment')['plan']
    one, two = draw_paths(demand, 2, 31)
    two.update({key: qty for key, qty in one.items() if demand.release[key] < 7})
    left, right = [
        rolling(base, demand, first, path, config['weights'], hard_credit=True, budget=SolveBudget(5))
        for path in (one, two)
    ]
    assert left['status'] == right['status'] == 'complete'
    assert left['first_shift'] == right['first_shift'] == prefix(first, 1)
    assert prefix(left['committed'], 7) == prefix(right['committed'], 7)
    assert left['stages'][0]['revealed_quantities'] == right['stages'][0]['revealed_quantities']
    for path, outcome in ((one, left), (two, right)):
        case = demand.problem(base, path)
        actual = production_kpis(case, outcome['committed'])
        for stage in outcome['stages']:
            day = stage['through_day']
            assert stage['ledger_verified']
            assert stage['closing']['cash_sar'] == actual['cash_curve'][day]
            assert stage['closing']['inventory'] == {key: values[day] for key, values in actual['inventory'].items()}
        assert outcome['metrics']['physical_ok'] and outcome['metrics']['credit_breach_sar'] == 0


def test_p2a_carries_uncollected_receipts_and_a_zero_forecast_has_no_penalty(demo):
    _, config, base = demo
    first_sku = base.orders['O1']['sku_id']
    unit_price = base.skus[first_sku]['unit_price']
    qty = base.orders['O1']['qty']
    day = 3
    plan = {'schedule': [], 'shipments': [{'order': 'O1', 'day': day}]}
    before = ledger(base, plan, day + base.lag)
    after = ledger(base, plan, day + base.lag + 1)
    assert before['receivables_sar'] == {day + base.lag: qty * unit_price}
    assert before['cash_sar'] + before['receivables_sar'][day + base.lag] == after['cash_sar']
    assert after['receivables_sar'] == {}
    demand = Demand.from_problem(base, 0.10)
    key = demand.forecast[0]
    case = demand.problem(base, {key: 0, 'O1': 0})
    assert case.orders['O1']['qty'] == qty
    assert case.unshipped_penalty(case.orders[key]) == 0
    assert solve_candidate(case, demand.release, config['weights'], 'cost', 0)[0]['status'] == 'UNKNOWN'
    assert SOLVE_SECONDS == 0.5


def test_p1l3_matches_independent_exhaustive_frontier():
    specs = {
        'ops': {'a': (7, 5, 4, 2), 'b': (4, 9, 2, 6), 'c': (5, 12, 4, 6)},
        'sales': {'a': (8, 3, 4, 2), 'b': (5, 7, 3, 4), 'c': (20, 2, 5, 6)},
    }
    problem = Allocation(limits={'credit.1': 8, 'storage.1': 10}, units=[
        {'id': unit, 'current': 'a', 'options': [
            {'id': key, 'resources': {'credit.1': vals[2], 'storage.1': vals[3]}, 'value': -vals[0]}
            for key, vals in options.items()]} for unit, options in specs.items()
    ])
    metrics = {unit: {key: PlanMetrics(version=f'{unit}-{key}', cost_sar=x[0], service_loss_pct=x[1])
                      for key, x in options.items()} for unit, options in specs.items()}
    eligible = set()
    for a, b in product(*[specs[unit].values() for unit in ('ops', 'sales')]):
        cost, loss, draw, storage = (sum(values) for values in zip(a, b))
        if draw <= 8 and storage <= 10:
            eligible.add((cost, loss / 2, draw))
    reference = {point for point in eligible if not any(
        other != point and all(a <= b for a, b in zip(other, point)) for other in eligible
    )}
    result = shared_frontier(problem, metrics)
    assert result['status'] == 'OPTIMAL' and result['checked_combinations'] == 9
    assert len(reference) == 3
    assert {tuple(item['objectives'].values()) for item in result['frontier']} == reference
    for item in result['frontier']:
        usage = Counter()
        for unit in problem.units:
            option = next(opt for opt in unit.options if opt.id == item['selection'][unit.id])
            usage.update(option.resources)
        assert usage == item['resource_usage']
        assert all(usage[key] <= maximum for key, maximum in problem.limits.items())
        assert set(item['department_deltas']) == {'ops', 'sales'}
        assert item['solver']['status'] == 'OPTIMAL'


def test_experiment_api_pins_source_and_refuses_mismatched_commitments(client):
    path = '/api/companies/2/decision-loop/production'
    assert client.post('/api/companies/1/decision-loop/production', json={}).status_code == 422
    assert client.post(path, json={'dataset_id': 1}).status_code == 404
    assert client.post(path, json={'model_id': 1}).status_code == 404
    waiting = client.post(path, json={'rolling': True, 'observations': []})
    assert waiting.status_code == 200, waiting.text
    first = waiting.json()
    assert first['rolling']['status'] == 'awaiting_observations'
    assert first['rolling']['commitment']['through_day'] == 0
    assert client.get('/api/companies/2/runs/latest?allow_missing=true').json() is None
    entries = first['scenario_set']['forecast_orders']
    next_day = min(order['release_day'] for order in entries)
    policy = {
        'rolling': True, 'dataset_id': first['dataset_id'], 'model_id': first['model_id'],
        'observations': [{'day': next_day, 'quantities': {order['id']: order['qty'] for order in entries
                                                         if order['release_day'] == next_day}}],
        'expected_commitment': first['rolling']['commitment'],
    }
    continued = client.post(path, json=policy)
    assert continued.status_code == 200, continued.text
    assert continued.json()['rolling']['first_shift'] == first['rolling']['first_shift']
    assert continued.json()['rolling']['commitment']['through_day'] > 0
    policy['expected_commitment']['fingerprint'] = 'f' * 64
    assert client.post(path, json=policy).status_code == 422
    policy['observations'][0]['day'] += 1
    assert client.post(path, json=policy).status_code == 422


def test_allocation_api_rebuilds_approved_operations_and_rejects_overlap(client, db_path):
    from app.db import encode, get_conn, utc_now

    def save_plan(label, order_ids, events):
        sku = f'{label}-S'
        line = f'{label}-L'
        data = {
            'lines': [{'id': line, 'changeover_cost': 0, 'changeover_loss_units': 0}],
            'skus': [{'id': sku, 'unit_cost': 4, 'unit_price': 9, 'initial_inventory': 0,
                      'holding_cost_per_day': 1, 'volume': 1}],
            'orders': [{'id': key, 'sku_id': sku, 'qty': 10, 'due_day': 1, 'penalty_per_day': 1}
                       for key in order_ids],
            'rates': [{'line_id': line, 'sku_id': sku, 'units_per_day': 100}],
        }
        plan = {
            'schedule': [{'line': line, 'sku': sku, 'day': day, 'qty': qty} for day, qty in events],
            'shipments': [{'order': order_ids[0], 'day': events[-1][0]}] if events else [],
        }
        with get_conn(db_path) as conn:
            dataset_id = conn.execute('INSERT INTO datasets (company_id, name, source, payload_json, '
                                      "validation_json, created_at, is_active) VALUES (2, ?, 'upload', ?, ?, ?, 0)",
                                      (label, encode(data), encode({'ok': True}), utc_now())).lastrowid
            mid = conn.execute('SELECT id FROM company_models WHERE company_id=2').fetchone()['id']
            run = conn.execute('INSERT INTO runs (company_id, dataset_id, model_id, kind, result_json, status, '
                               "wall_s, created_at) VALUES (2, ?, ?, 'optimize', ?, 'FEASIBLE', 0, ?)", (
                                   dataset_id, mid, encode({'optimized': {'plan': plan, 'kpis': {'feasible': True}}}),
                                   utc_now(),
                               )).lastrowid
        draft = client.post('/api/companies/2/plans', json={'run_id': run, 'variant': 'optimized'})
        assert draft.status_code == 200, draft.text
        return draft.json()['id']

    a0 = save_plan('A', ['A-order'], [(0, 10)])
    a1 = save_plan('A', ['A-order'], [(0, 14)])
    b0 = save_plan('B', ['B-order'], [(0, 10)])
    overlap = save_plan('A', ['B-order'], [(0, 10)])
    path = '/api/companies/2/decision-loop/allocation'
    body = {'units': [{'id': 'lineA', 'current': a0, 'plans': [a0, a1]},
                      {'id': 'lineB', 'current': b0, 'plans': [b0]}],
            'limits': {'credit.0': 0, 'storage.0': 2}}
    assert client.post(path, json=body).status_code == 409
    for pid in (a0, a1, b0, overlap):
        decision = client.post(f'/api/plans/{pid}/decide', json={'decision': 'approved', 'comment': 'QA'})
        assert decision.status_code == 200, decision.text
    result = client.post(path, json=body)
    assert result.status_code == 200, result.text
    assert result.json()['checked_combinations'] == 2
    [point] = result.json()['frontier']
    assert point['selection'] == {'lineA': f'plan.{a0}', 'lineB': f'plan.{b0}'}
    assert point['resource_usage'] == {'credit.0': 0, 'storage.0': 0}
    assert len(set(point['versions'].values())) == 2
    body['units'][1].update(current=overlap, plans=[overlap])
    assert client.post(path, json=body).json()['detail']['code'] == 'overlapping_operations'
