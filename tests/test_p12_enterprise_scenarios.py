from base64 import b64decode
from io import BytesIO
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from conftest import sign_in
from openpyxl import load_workbook

from app.config import Settings
from app.db import get_conn
from app.enterprise.arms import Plan, production_ledger
from app.enterprise.finance import Ledger
from app.main import create_app
from test_p11_enterprise_impact import assume, edge, run, save_graph, scalar, solver_calls  # noqa: F401

BASE = '/api/enterprise'
SCENARIOS = BASE + '/scenarios'
COMPARE = SCENARIOS + '/compare'
TORNADO = SCENARIOS + '/tornado'


def save(client, name, *changes, **scope):
    response = client.post(SCENARIOS, json={'name': name, 'assumptions': changes, **scope})
    assert response.status_code == 201, response.text
    return response.json()


def analytics_model(client):
    return save_graph(client, [
        scalar('volume', 40), scalar('price', 20), scalar('zero', 0),
        scalar('pnl.result', 100), scalar('cash', 120, 'cash'),
        scalar('risk.credit', 25, 'risk_kpi'), scalar('risk.delay', 0, 'risk_kpi'),
        scalar('risk.fill', 96, 'risk_kpi'),
    ], [edge('volume', 'pnl.result', multiplier=2), edge('price', 'pnl.result', multiplier=-1),
        edge('volume', 'cash')])


@pytest.fixture
def model_id(client):
    return analytics_model(client)


def test_saved_scenarios_persist_and_pin_version(client, db_path, model_id):
    first = save(client, '  حملة موسمية  ', assume('volume', 10, 'add', 2),
                 assume('price', -10, 'pct'), model_id=model_id)
    second = save(client, 'سعر ثابت', assume('price', 20), model_id=model_id)
    model_path = f'{BASE}/models/{model_id}'
    model = client.get(model_path).json()
    graph = model['graph']
    profit = next(node for node in graph['nodes'] if node['id'] == 'pnl.result')
    profit['baseline']['value'] = 200
    assert client.put(model_path, json={'expected_version': 1, 'name': 'جديد', 'graph': graph}).json()['version'] == 2
    assert client.delete(model_path).status_code == 204
    with TestClient(create_app(Settings(db_path=db_path, forecast_engine='stat'))) as restarted:
        sign_in(restarted)
        assert restarted.get(SCENARIOS).json() == [second, first]
        assert first['name'] == 'حملة موسمية'
        assert len(first['assumptions']) == 2
        assert first['version'] == 1
        response = restarted.post(COMPARE, json={'scenario_ids': [first['id'], second['id']]})
        assert response.status_code == 200, response.text
        result = response.json()
        assert result['version'] == 1
        assert result['baseline'] == dict(profit=100, cash_low=120, credit_breach=25, delays=0, fill_rate=96)
        assert result['scenarios'][0]['kpis'] == result['baseline'] | {'profit': 122, 'cash_low': 130}
        assert result['scenarios'][1]['kpis'] == result['baseline']
        assert result['settings'] == {'weeks': 52, 'n': 200, 'seed': 42}
        drivers = restarted.get(BASE + '/whatif/drivers', params={'model_id': model_id, 'version': 1})
        assert drivers.status_code == 200
        assert {item['id'] for item in drivers.json()['drivers']} >= {'volume', 'price'}
    with get_conn(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM audit_log WHERE action='enterprise.scenario.create'").fetchone()[0] == 2


def test_schema_upgrade_keeps_enterprise_and_sector_models(client, db_path, model_id):
    models = client.get(BASE + '/models').json()
    companies = client.get('/api/companies').json()
    with get_conn(db_path) as conn:
        conn.execute('DROP TABLE enterprise_scenarios')
    with TestClient(create_app(Settings(db_path=db_path, forecast_engine='stat'))) as upgraded:
        sign_in(upgraded)
        assert upgraded.get(BASE + '/models').json() == models
        assert upgraded.get('/api/companies').json() == companies
        assert upgraded.get(SCENARIOS).json() == []
        assert save(upgraded, 'بعد الترقية', assume('price', 10), model_id=model_id)['version'] == 1


@pytest.mark.parametrize('change', [
    {'name': '   '}, {'assumptions': []}, {'assumptions': [assume('unknown', 2)]},
    {'version': 1}, {'assumptions': [assume('credit', 2), assume('credit', 3)]},
    {'assumptions': [assume('storage', 0)]}, {'name': 'س' * 121},
])
def test_invalid_save_does_not_write(client, change):
    body = {'name': 'تجربة', 'assumptions': [assume('credit', -10, 'pct')]} | change
    assert client.post(SCENARIOS, json=body).status_code == 422
    assert client.get(SCENARIOS).json() == []


@pytest.mark.parametrize('count', [2, 3, 4])
def test_comparison_preserves_requested_column_order(client, model_id, count):
    ids = [save(client, f'سيناريو {i}', assume('volume', i, 'add'), model_id=model_id)['id'] for i in range(count)]
    result = client.post(COMPARE, json={'scenario_ids': ids[::-1], 'n': 10, 'seed': 77}).json()
    assert [row['id'] for row in result['scenarios']] == ids[::-1]
    assert [row['kpis']['profit'] for row in result['scenarios']] == [100 + 2 * i for i in range(count)][::-1]
    assert result['settings']['seed'] == 77


@pytest.mark.parametrize('ids, status', [
    ([], 422), ([1], 422), ([1, 1], 422), ([1, 2, 3, 4, 5], 422), ([1, 99], 404),
])
def test_invalid_comparison_ids(client, ids, status):
    assert client.post(COMPARE, json={'scenario_ids': ids}).status_code == status


def test_comparison_rejects_mixed_models_and_versions(client, model_id):
    first = save(client, 'أ', assume('volume', 50), model_id=model_id)['id']
    model_path = f'{BASE}/models/{model_id}'
    graph = client.get(model_path).json()['graph']
    assert client.put(model_path, json={'expected_version': 1, 'name': 'معدل', 'graph': graph}).is_success
    second = save(client, 'ب', assume('volume', 40), model_id=model_id)['id']
    third = save(client, 'ج', assume('supplier', 5))['id']
    for ids in ([first, second], [second, third]):
        result = client.post(COMPARE, json={'scenario_ids': ids})
        assert result.status_code == 422
        assert result.json()['detail']['code'] == 'enterprise_scenario_scope'


def test_demo_comparison_reuses_baseline_and_matches_phase11_risk(client, solver_calls):
    changes = [assume('credit', 3_850_000, uncertainty=2_000_000), assume('supplier.payment_lag', 0)]
    before = save(client, 'مرجع', assume('credit', 0, 'pct'))['id']
    after = save(client, 'ضغط التوريد والتمويل', *changes)['id']
    assert not solver_calls
    body = {'scenario_ids': [before, after], 'n': 40, 'seed': 42}
    compared = client.post(COMPARE, json=body)
    assert compared.status_code == 200, compared.text
    assert solver_calls == {'production': 2, 'contracting': 2}
    data = compared.json()
    assert data['scenarios'][0]['kpis'] == data['baseline']
    assert data == client.post(COMPARE, json=body).json()
    result, nodes = run(client, *changes, n=40, seed=42)
    assert 0 < nodes['risk.credit']['after'] < 100
    assert data['scenarios'][1]['kpis'] == {
        'profit': nodes['pnl.result']['after'], 'cash_low': nodes['cash']['after'],
        'credit_breach': nodes['risk.credit']['after'], 'delays': nodes['risk.delay']['after'],
        'fill_rate': result['fill_rate'],
    }


def test_tornado_uses_two_independent_shocks_and_ranks_by_span(client, model_id):
    response = client.post(TORNADO, json={'model_id': model_id, 'kpi': 'profit', 'percent': 25,
                                          'driver_ids': ['price', 'zero', 'volume']})
    assert response.status_code == 200, response.text
    data = response.json()
    assert data['percent'] == 25 and data['baseline'] == 100
    assert [(row['driver']['id'], row['low']['delta'], row['high']['delta'], row['span']) for row in data['rows']] == [
        ('volume', -20, 20, 40), ('price', 5, -5, 10), ('zero', 0, 0, 0),
    ]
    assert data['rows'][-1]['zero_reference']


def test_tornado_keeps_valid_side_and_reports_other_side(client, solver_calls):
    response = client.post(TORNADO, json={'kpi': 'fill_rate', 'percent': 100,
                                          'driver_ids': ['storage', 'supplier.payment_lag']})
    assert response.status_code == 200, response.text
    row, failed = response.json()['rows']
    assert 0 <= response.json()['baseline'] <= 100
    assert row['span'] == 0
    assert failed['driver']['id'] == 'storage'
    assert failed['low']['delta'] is None and failed['low']['error']
    assert failed['high']['value'] is not None and failed['span'] is None
    assert solver_calls['contracting'] == 1


@pytest.mark.parametrize('extra', [
    {'kpi': 'unknown'}, {'percent': 0}, {'percent': 101}, {'driver_ids': []},
    {'driver_ids': ['volume', 'volume']}, {'driver_ids': ['fake']},
])
def test_invalid_tornado_inputs(client, model_id, extra):
    body = {'model_id': model_id, 'kpi': 'profit', 'driver_ids': ['volume']} | extra
    assert client.post(TORNADO, json=body).status_code == 422


def test_missing_kpi_is_blank_in_comparison_and_rejected_for_sweep(client):
    mid = save_graph(client, [scalar('price', 20), scalar('pnl.result', 100)], [])
    ids = [save(client, str(i), assume('price', i), model_id=mid)['id'] for i in range(2)]
    comparison = client.post(COMPARE, json={'scenario_ids': ids}).json()
    assert comparison['baseline']['fill_rate'] is None
    assert comparison['scenarios'][0]['kpis']['credit_breach'] is None
    assert client.post(TORNADO, json={'model_id': mid, 'kpi': 'cash_low'}).status_code == 422
    response = client.post(TORNADO, json={'model_id': mid, 'kpi': 'profit'})
    assert {row['driver']['id'] for row in response.json()['rows']} == {'price', 'pnl.result'}


def open_export(client, route, body):
    response = client.post(route + '.xlsx', json=body)
    assert response.status_code == 200, response.text
    assert response.headers['content-type'] == 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    assert response.headers['content-disposition'].endswith('.xlsx"')
    book = load_workbook(BytesIO(response.content))
    assert all(sheet.sheet_view.rightToLeft for sheet in book)
    return book


def test_comparison_excel_matches_visible_values_and_treats_names_as_text(client, model_id):
    first = save(client, '=1+1', assume('volume', 5, 'add'), model_id=model_id)
    second = save(client, 'سعر جديد', assume('price', 30), model_id=model_id)
    body = {'scenario_ids': [first['id'], second['id']], 'n': 10, 'weeks': 6, 'seed': 77}
    data = client.post(COMPARE, json=body).json()
    with_workbook = open_export(client, COMPARE, body)
    assert with_workbook.sheetnames == ['المقارنة', 'الفرضيات', 'الإعدادات']
    comparison = with_workbook['المقارنة']
    assert comparison['D1'].value == '=1+1' and comparison['D1'].data_type == 's'
    assert list(comparison.values)[1][2:] == (
        data['baseline']['profit'], *[row['kpis']['profit'] for row in data['scenarios']],
    )
    assert with_workbook['الفرضيات']['A2'].data_type == 's'
    assert list(with_workbook['الإعدادات'].values)[1][1:] == (1, 6, 10, 77)
    with_workbook.close()


def test_embedded_workbook_is_built_from_the_same_tornado_run(client, model_id, monkeypatch):
    from app.enterprise.whatif import ImpactModel

    baseline = ImpactModel.baseline
    calls = []

    def track(model):
        calls.append(model)
        return baseline(model)

    monkeypatch.setattr(ImpactModel, 'baseline', track)
    body = {'model_id': model_id, 'kpi': 'profit', 'percent': 25, 'driver_ids': ['volume']}
    response = client.post(TORNADO + '?include_excel=true', json=body)
    assert response.status_code == 200
    data = response.json()
    book = load_workbook(BytesIO(b64decode(data['xlsx_base64'])))
    assert book['الحساسية']['H2'].value == data['rows'][0]['span']
    assert len(calls) == 1
    book.close()


def test_tornado_excel_matches_ranked_deltas(client, model_id):
    body = {'model_id': model_id, 'kpi': 'profit', 'percent': 25, 'driver_ids': ['price', 'volume']}
    data = client.post(TORNADO, json=body).json()
    book = open_export(client, TORNADO, body)
    assert list(book['الحساسية'].values)[1][1:8] == ('volume', 40, 80, -20, 120, 20, 40)
    assert book['الحساسية']['H3'].value == data['rows'][1]['span']
    assert book['الإعدادات']['E2'].value == data['baseline']
    book.close()


def test_production_fill_rate_uses_unit_volume_and_handles_no_demand(monkeypatch):
    result = {'schedule': [], 'changeover_days': [], 'inventory': {}, 'storage_curve': [0],
              'shipments': [{'day': 0, 'order': 'one', 'late_days': 0}]}
    problem = SimpleNamespace(horizon=1, lines={}, skus={'S1': {'unit_price': 10, 'unit_cost': 5}}, lag=0, storage=1,
                              orders={'one': {'qty': 20, 'sku_id': 'S1', 'penalty_per_day': 0},
                                      'two': {'qty': 60, 'sku_id': 'S1', 'penalty_per_day': 0}})
    monkeypatch.setattr('app.enterprise.arms.production.prepare', lambda *_: problem)
    monkeypatch.setattr('app.enterprise.arms.production.evaluate', lambda *_: {'kpis': result})
    inputs = ({}, {})
    plan = Plan(*inputs, {}, 'FEASIBLE')
    assert production_ledger(inputs, plan, 0, 0, Ledger(1))['fill_rate'] == 25
    problem.orders.clear()
    result['shipments'].clear()
    assert production_ledger(inputs, plan, 0, 0, Ledger(1))['fill_rate'] is None
