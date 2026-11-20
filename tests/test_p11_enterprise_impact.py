from collections import Counter

import numpy as np
import pytest

from app.db import encode, get_conn
from app.enterprise.demo import demo_template
from app.enterprise.finance import Ledger, settle
from app.enterprise.impact_graph import CausalGraph
from app.enterprise.schema import Graph
from app.optimization import contracting, production

BASE = '/api/enterprise'
PATH = BASE + '/whatif'


def scalar(key, value, kind='profit_loss'):
    return dict(id=key, type=kind, name_ar=key, unit='SAR', baseline=dict(kind='scalar', value=value),
                definition=dict(kind='formula', expression='قيمة مرجعية'))


def edge(source, target, **rule):
    return dict(source=source, target=target, name_ar=f'{source} to {target}',
                **({'rule': rule} if rule else {}))


def save_graph(client, nodes, edges):
    response = client.post(BASE + '/models', json={'name': 'نموذج اختبار', 'graph': {
        'nodes': nodes, 'edges': edges,
    }})
    assert response.status_code == 201, response.text
    return response.json()['id']


def assume(node_id, value, operation='set', uncertainty=0):
    return dict(node_id=node_id, value=value, operation=operation, uncertainty=uncertainty)


def run(client, *assumptions, **options):
    response = client.post(PATH, json=dict(assumptions=assumptions, **options))
    assert response.status_code == 200, response.text
    result = response.json()
    return result, {row['id']: row for row in result['nodes']}


@pytest.fixture
def solver_calls(monkeypatch):
    calls = Counter()

    def patch(sector, module, pick):
        def solve(dataset, parameters, **kwargs):
            calls[sector] += 1
            problem = module.prepare(dataset, parameters)
            plan = pick(problem)
            if plan is None:
                return {'optimized': {'plan': {}}, 'solver': {'status': 'INFEASIBLE'}}
            return {'optimized': module.evaluate(problem, plan), 'solver': {'status': 'FEASIBLE'}}
        monkeypatch.setattr(module, 'solve', solve)

    patch('production', production, production.baseline)
    patch('contracting', contracting, lambda problem: contracting.baseline(problem, respect_credit=False))
    return calls


def test_topological_paths_lag_elasticity_capacity_and_unknown_baseline(client):
    nodes = [scalar('result', 100), scalar('volume', 50), scalar('price', 10), scalar('unknown', None)]
    edges = [edge('volume', 'result', multiplier=3, maximum=130),
             edge('price', 'volume', mode='elasticity', multiplier=2, lag_weeks=2)]
    model_id = save_graph(client, nodes, edges)
    short, _ = run(client, assume('price', 50, 'pct'), model_id=model_id, weeks=2)
    assert not short['paths']
    result, rows = run(client, assume('price', 50, 'pct'), model_id=model_id, weeks=3)
    assert rows['price']['after'] == 15
    assert rows['volume']['after'] == 100
    assert rows['result']['after'] == 130
    assert rows['result']['delta_pct'] == 30
    assert rows['unknown']['after'] is None
    assert rows['unknown']['quantiles'] is None
    assert {(row['source'], row['target']) for row in result['paths']} == {
        ('price', 'volume'), ('volume', 'result'),
    }
    graph = CausalGraph(Graph.model_validate(dict(nodes=nodes, edges=edges)))
    values, _ = graph.propagate(4, {'price': 15})
    assert values['result'].tolist() == [100, 100, 130, 130]


def test_reference_pinned_and_neutral_scenario(client):
    model_id = save_graph(client, [scalar('input', 10)], [])
    snapshot = client.get(f'{BASE}/models/{model_id}').json()
    graph = snapshot['graph']
    graph['nodes'][0]['baseline']['value'] = 12
    assert client.put(f'{BASE}/models/{model_id}', json={
        'expected_version': 1, 'name': 'الإصدار التاني', 'graph': graph,
    }).status_code == 200
    result, rows = run(client, assume('input', 10), model_id=model_id, version=1)
    assert result['version'] == 1 and rows['input']['affected'] is False
    result, rows = run(client, assume('input', 10), model_id=model_id)
    assert result['version'] == 2 and rows['input']['delta'] == -2


def test_cycles_reported_as_arabic_422(client, db_path):
    nodes = [scalar('left', 10), scalar('right', 10)]
    model_id = save_graph(client, nodes, [edge('left', 'right')])
    # A pre-contract snapshot may already contain the cycle. It remains readable
    # for correction, while both calculation endpoints must fail closed.
    with get_conn(db_path) as connection:
        connection.execute('UPDATE enterprise_model_versions SET graph_json = ? WHERE model_id = ?',
                           (encode({'nodes': nodes, 'edges': [
                               edge('left', 'right'), edge('right', 'left', lag_weeks=1),
                           ]}), model_id))
    response = client.post(PATH, json={'model_id': model_id, 'assumptions': [assume('left', 1)]})
    assert response.status_code == 422
    assert 'دورة' in response.json()['detail']['msg_ar']
    assert client.get(PATH + '/drivers', params={'model_id': model_id}).status_code == 422


def test_seeded_monte_carlo_and_quantiles(client):
    model_id = save_graph(client, [scalar('driver', 10), scalar('pnl.result', 0)],
                          [edge('driver', 'pnl.result', multiplier=2)])
    options = {'model_id': model_id}
    scenario = assume('driver', 10, uncertainty=4)
    first, rows = run(client, scenario, **options)
    assert first['simulation'] == {'n': 200, 'seed': 42, 'max_financing_iterations': 0}
    assert first == run(client, scenario, **options)[0]
    assert rows['pnl.result']['delta_pct'] is None
    assert rows['pnl.result']['after'] == 0
    p10, p50, p90 = rows['pnl.result']['quantiles'].values()
    assert -8 < p10 < p50 < p90 < 8
    assert rows['pnl.result']['quantiles'] != run(client, scenario, seed=43, **options)[1]['pnl.result']['quantiles']


@pytest.mark.parametrize(('options', 'assumptions'), [
    ({'version': 1}, [assume('credit', 10)]),
    ({'n': 0}, [assume('credit', 10)]),
    ({'n': 1001}, [assume('credit', 10)]),
    ({'seed': -1}, [assume('credit', 10)]),
    ({'weeks': 105}, [assume('credit', 10)]),
    ({}, [assume('credit', 10), assume('credit', 11)]),
    ({}, [assume('credit', 10, uncertainty=-1)]),
    ({}, [assume('missing', 10)]),
    ({}, [assume('pnl.result', 10)]),
    ({}, [assume('credit', 10, uncertainty=11)]),
    ({}, [assume('storage', 0)]),
    ({}, [assume('workforce.R2', 0)]),
])
def test_rejected_scenarios_launch_no_solver(client, solver_calls, options, assumptions):
    response = client.post(PATH, json=dict(assumptions=assumptions, **options))
    assert response.status_code == 422
    assert response.json()['detail']['msg_ar']
    assert solver_calls == {}


def test_demo_driver_options_and_page(client, solver_calls):
    response = client.get(PATH + '/drivers')
    assert response.status_code == 200
    drivers = {item['id'] for item in response.json()['drivers']}
    assert {'price.S1', 'demand.S1', 'storage', 'credit', 'cash', 'supplier',
            'workforce.R1', 'downtime.L1', 'supplier.payment_lag'} <= drivers
    assert 'pnl.revenue' not in drivers
    assert solver_calls == {}
    assert 'data-testid="page-impact"' in client.get('/').text
    assert client.get('/static/js/pages/impact.js').status_code == 200


def test_supply_shock_replans_only_production_and_reconciles_pnl(client, solver_calls):
    result, rows = run(client, assume('supplier', 4, 'add'), n=4)
    assert solver_calls == {'contracting': 1, 'production': 2}
    assert result['optimizers_run']['scenario'] == ['production']
    assert rows['supplier']['delta'] == 4
    assert rows['line.L1']['affected']
    assert any(path['source'] == 'supplier' for path in rows['line.L1']['path'])
    assert rows['risk.delay']['delta'] == 0
    for side in ('before', 'after'):
        assert rows['pnl.result'][side] == pytest.approx(
            rows['pnl.revenue'][side] - rows['pnl.direct_cost'][side]
            - rows['pnl.expenses'][side] - rows['financing'][side],
        )
        assert min(result['charts']['cash'][side]) == rows['cash'][side]
    assert result['charts']['cash']['dates'][1] == '2026-01-05'
    assert all(row['quantiles']['p50'] == row['after'] for row in rows.values())


def test_credit_runs_both_once_and_trials_reuse_plans(client, solver_calls):
    result, rows = run(client, assume('credit', 3_850_000, uncertainty=2_000_000), n=24)
    assert solver_calls == {'contracting': 2, 'production': 2}
    assert result['optimizers_run']['scenario'] == ['production', 'contracting']
    assert 0 < rows['risk.credit']['after'] < 100
    assert rows['financing.draw']['after'] <= rows['credit']['after']
    assert rows['financing.draw']['quantiles']['p10'] < rows['financing.draw']['quantiles']['p90']
    assert result['simulation']['max_financing_iterations'] <= 40


def test_payables_timing_and_neutral_rounding_skip_replanning(client, solver_calls):
    result, rows = run(client, assume('supplier.payment_lag', 30), assume('price.S1', 0.01, 'add'), n=2)
    assert result['optimizers_run']['scenario'] == []
    assert solver_calls == {'contracting': 1, 'production': 1}
    assert rows['ap']['before'] == 0 < rows['ap']['after']
    assert rows['pnl.revenue']['delta'] == 0
    assert rows['pnl.direct_cost']['delta'] == 0
    assert rows['procurement']['delta'] == 0
    assert result['charts']['cash']['before'] != result['charts']['cash']['after']


def test_one_line_downtime_does_not_dirty_contracting(client, solver_calls):
    result, rows = run(client, assume('downtime.L1', 14), n=3)
    assert result['optimizers_run']['scenario'] == ['production']
    assert rows['line.L1']['before'] > rows['line.L1']['after'] == 0
    assert rows['risk.delay']['delta'] == 0
    assert rows['line.L1']['path'][0]['source'] == 'downtime.L1'


def test_workforce_shock_dirties_contracting_only(client, solver_calls):
    result, rows = run(client, assume('workforce.R2', 4), n=3)
    assert result['optimizers_run']['scenario'] == ['contracting']
    assert rows['line.L1']['delta'] == rows['line.L2']['delta'] == 0
    assert rows['sales.production']['delta'] == 0


def test_demand_replay_changes_distributed_results_without_extra_solves(client, solver_calls):
    result, rows = run(client, assume('demand.S1', 10, 'pct', uncertainty=120), n=25)
    assert result['optimizers_run']['scenario'] == ['production']
    assert solver_calls == {'production': 2, 'contracting': 1}
    assert rows['sales.production']['quantiles']['p10'] < rows['sales.production']['quantiles']['p90']
    assert any(path['source'] == 'demand.S1' for path in rows['sales.production']['path'])


def test_invalid_capacity_rule_rejected_at_model_write(client):
    response = client.post(BASE + '/models', json={'name': 'نموذج اختبار', 'graph': {
        'nodes': [scalar('price', 10), scalar('value', 100)],
        'edges': [edge('price', 'value', minimum=20, maximum=19)],
    }})
    assert response.status_code == 422


def test_legacy_snapshot_uses_same_ledger_and_paths(client, solver_calls):
    demo = demo_template().model_dump(mode='json')
    graph = demo['graph']
    extra = {'pnl.expenses', 'supplier.payment_lag', 'downtime.L1', 'downtime.L2'}
    graph['nodes'] = [node for node in graph['nodes'] if node['id'] not in extra]
    graph['edges'] = [row for row in graph['edges'] if not {row['source'], row['target']} & extra]
    for row in graph['edges']:
        row.pop('rule')
    response = client.post(BASE + '/models', json={'name': demo['name'], 'graph': graph})
    assert response.status_code == 201
    result, rows = run(client, assume('supplier', 3, 'add'), model_id=response.json()['id'], n=2)
    assert rows['pnl.result']['before'] != rows['pnl.result']['after']
    assert 'pnl.expenses' not in rows
    assert result['version'] == 1


@pytest.mark.parametrize(('position', 'limit', 'expected_draw'), [(-80, 100, 80 / .99), (-120, 100, 100), (120, 100, 0)])
def test_financing_fixed_point(position, limit, expected_draw):
    cash, draw, interest, iterations = settle(position, limit, .01, previous_draw=25)
    assert draw == pytest.approx(expected_draw, abs=.00001)
    assert interest == pytest.approx(draw * .01)
    assert cash == pytest.approx(position + draw - interest, abs=.00001)
    assert 1 < iterations <= 40


def test_shared_facility_and_accounting_ledger():
    ledger = Ledger(days=3)
    ledger.post('payments', 0, 60)
    ledger.post('payments', 0, 60)
    ledger.post('revenue.contracting', 0, 140)
    ledger.post('receipts.contracting', 1, 140)
    ledger.post('contract_cost', 0, 60)
    ledger.post('purchases', 0, 60)
    ledger.post('direct_cost', 0, 120)
    values, _ = ledger.close(0, 100, 0)
    assert np.array_equal(values['cash'], [0, -20, 20, 20])
    assert np.array_equal(values['ap'], [0, 0, 0])
    assert np.array_equal(values['ar.contracting'], [140, 0, 0])
    assert np.array_equal(values['financing.draw'], [100, 0, 0])
    assert values['pnl.result'][-1] == 20 and values['risk.credit'][-1] == 100
