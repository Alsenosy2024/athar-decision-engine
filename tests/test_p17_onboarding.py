import sqlite3
from io import BytesIO

import pytest
from openpyxl import load_workbook

from app.config import Settings
from app.db import get_conn, initialize
from app.main import create_app, seed
from app.onboarding.presets import SECTIONS
from app.onboarding.storage import initialize as migrate
from fastapi.testclient import TestClient
from conftest import sign_in


def ok(response, status=200):
    assert response.status_code == status, response.text
    return response.json()


def create(client, sector='trading'):
    model = ok(client.post('/api/onboarding/companies', json={'name': 'شركة تجربة', 'sector': sector}), 201)
    return model, f"/api/companies/{model['company_id']}/onboarding"


def manual(client, path, section, records, version=1):
    return ok(client.post(f'{path}/sections/{section}/manual/preview',
                          json={'records': records, 'expected_version': version}))


def commit(client, path, review):
    assert review['validation']['ok'] and review['token'], review
    return ok(client.post(path + '/commit', json={'token': review['token']}))


def workbook(client, path, fill, version, section=None):
    suffix = '?section=' + section if section else ''
    response = client.get('/api/onboarding/templates/trading/workbook' + suffix)
    assert response.status_code == 200
    book = load_workbook(BytesIO(response.content))
    assert book.sheetnames == ([section] if section else list(SECTIONS))
    assert book.properties.title == 'أثر | Athar'
    assert all(sheet.sheet_view.rightToLeft for sheet in book)
    for sheet, records in fill.items():
        for record in records:
            book[sheet].append(record)
    out = BytesIO()
    book.save(out)
    book.close()
    form = {'expected_version': version}
    if section:
        form['section'] = section
    return ok(client.post(path + '/excel/preview', data=form, files={'file': ('upload.xlsx', out.getvalue())}))


def by_key(rows, key='id'):
    return {row[key]: row for row in rows}


@pytest.mark.parametrize(('sector', 'elasticity', 'capacity_type'), [
    ('trading', -.6, 'production_line'), ('retail', -1.1, 'production_line'),
    ('services', -.35, 'project'), ('custom', -.5, 'production_line'),
])
def test_presets_and_empty_model(client, sector, elasticity, capacity_type):
    template = ok(client.get(f'/api/onboarding/templates/{sector}'))
    assert [item['id'] for item in template['sections']] == list(SECTIONS)
    graph = template['graph']
    assert by_key(graph['nodes'])['capacity']['type'] == capacity_type
    assert next(edge for edge in graph['edges'] if edge['source'] == 'price'
                and edge['target'] == 'demand')['rule']['multiplier'] == elasticity
    model, path = create(client, sector)
    assert ok(client.get(path)) == model
    assert model['readiness']['score'] == 0
    assert all(node['baseline']['value'] is None for node in model['nodes'])
    assert by_key(ok(client.get('/api/companies')))[model['company_id']]['template'] == sector
    assert client.get(f"/api/companies/{model['company_id']}/model").status_code == 422
    result = ok(client.get('/api/enterprise/whatif/drivers', params={'model_id': model['model_id']}))
    assert result['drivers'] == []


def test_section_inputs_excel_and_partial_whatif(client):
    model, path = create(client)
    review = manual(client, path, 'structure', [{'key': 'department.sales', 'value': 'المبيعات'}])
    assert ok(client.get(path))['version'] == 1
    assert review['diff'][0]['before'] is None
    model = commit(client, path, review)
    assert by_key(model['readiness']['sections'])['structure']['score'] == 100
    review = workbook(client, path, {'sales': [('demand', 100, None, 'estimated')]}, 2, 'sales')
    model = commit(client, path, review)
    assert model['version'] == 3
    assert model['readiness']['sources']['demand'] == 'estimated'
    review = workbook(client, path, {'catalog': [('price', 20), ('offering.box', 'صندوق')]}, 3, 'catalog')
    model = commit(client, path, review)
    result = ok(client.post('/api/enterprise/whatif', json={
        'model_id': model['model_id'], 'weeks': 1, 'n': 15, 'seed': 42,
        'assumptions': [{'node_id': 'demand', 'operation': 'pct', 'value': 10}],
    }))
    rows = by_key(result['nodes'])
    assert rows['sales.volume']['after'] == pytest.approx(110)
    assert rows['pnl.revenue']['after'] == pytest.approx(2200)
    assert rows['pnl.revenue']['path']
    assert rows['cash']['after'] is None and rows['cash']['source'] == 'missing'
    assert result['charts']['cash'] is None
    finance = by_key(result['onboarding']['sections'])['finance']
    assert next(item for item in finance['blocks'] if item['feature'] == 'توقع الكاش')['fields'] == [
        'ap', 'ar', 'cash.opening', 'financing', 'pnl.expenses',
    ]
    historical = ok(client.get(f"/api/enterprise/models/{model['model_id']}/versions/2"))
    assert [item['key'] for item in historical['graph']['onboarding']['records']] == ['department.sales']


def test_full_workbook_upsert_finance_and_sensitivity(client):
    model, path = create(client)
    review = workbook(client, path, {
        'structure': [('department.sales', 'المبيعات')],
        'sales': [('demand', 80, '2026-10-01'), ('demand', 120, '2026-10-08')],
        'catalog': [('offering.box', 'صندوق'), ('price', 25), ('unit_cost', 10)],
        'operations': [('capacity', 110)],
        'inventory': [('inventory', 60)],
        'suppliers': [('supplier.delay', 5)],
        'workforce': [('workforce', 4)],
        'finance': [('cash.opening', 1000), ('ar', 0), ('ap', 800), ('credit', 500),
                    ('financing', 10), ('pnl.expenses', 100)],
        'risk': [('risk.exposure', 10, None, 'estimated')],
    }, 1)
    model = commit(client, path, review)
    assert model['readiness']['score'] == 100
    review2 = workbook(client, path, {
        'sales': [('demand', 90, '2026-10-01'), ('demand', 130, '2026-10-15')],
        'finance': [('ap', 850)],
    }, 2)
    assert len(review2['diff']) == 3
    assert review2['diff'][0]['before']['value'] == 80
    model = commit(client, path, review2)
    nodes = by_key(model['nodes'])
    assert [row['value'] for row in nodes['demand']['baseline']['points']] == [90, 120, 130]
    assert nodes['pnl.result']['baseline']['value'] == 1540
    assert nodes['cash']['baseline']['value'] == 1690
    assert model['readiness']['sources']['ar'] == 'actual'
    assert model['readiness']['sources']['cash'] == 'estimated'
    body = {'model_id': model['model_id'], 'weeks': 1, 'n': 20, 'seed': 42,
            'assumptions': [{'node_id': 'price', 'operation': 'add', 'value': 5, 'uncertainty': 1}]}
    result = ok(client.post('/api/enterprise/whatif', json=body))
    assert result == ok(client.post('/api/enterprise/whatif', json=body))
    rows = by_key(result['nodes'])
    assert rows['demand']['after'] == pytest.approx(114.4)
    assert rows['price']['source'] == 'actual' and rows['price']['scenario_source'] == 'estimated'
    assert rows['cash']['after'] == 2240
    assert rows['cash']['quantiles']['p10'] < rows['cash']['quantiles']['p90']
    assert result['charts']['cash']['after'] == [2240]
    sweep = ok(client.post('/api/enterprise/scenarios/tornado', json={
        'model_id': model['model_id'], 'weeks': 1, 'kpi': 'cash_low', 'driver_ids': ['price'],
    }))
    assert sweep['baseline'] == 1690
    assert sweep['rows'][0]['high']['value'] > sweep['rows'][0]['low']['value']


def test_version_binding_no_ops_and_replay(client, db_path):
    model, path = create(client)
    a = manual(client, path, 'sales', [{'key': 'demand', 'value': 12}])
    b = manual(client, path, 'sales', [{'key': 'demand', 'value': 20}])
    commit(client, path, a)
    assert client.post(path + '/commit', json={'token': a['token']}).status_code == 404
    assert client.post(path + '/commit', json={'token': b['token']}).status_code == 409
    assert manual(client, path, 'sales', [{'key': 'demand', 'value': 12}], 2)['token'] is None
    with get_conn(db_path) as connection:
        row = connection.execute("SELECT actor, user_id FROM audit_log WHERE action='onboarding.commit'").fetchone()
        assert row['actor'] == 'qa-admin' and row['user_id'] > 0
    model_path = f"/api/enterprise/models/{model['model_id']}"
    snapshot = ok(client.get(model_path))
    assert client.put(model_path, json={'name': 'test', 'expected_version': 2,
                                       'graph': snapshot['graph']}).status_code == 422
    assert client.delete(model_path).status_code == 422
    assert ok(client.get(path))['version'] == 2


@pytest.mark.parametrize(('section', 'records', 'column'), [
    ('sales', [{'key': 'price', 'value': 10}], 'key'),
    ('sales', [{'key': 'demand', 'value': -1}], 'value'),
    ('sales', [{'key': 'demand', 'value': 1}, {'key': 'demand', 'value': 2}], 'key'),
    ('finance', [{'key': 'ar', 'value': 25, 'date': 'yesterday'}], 'date'),
    ('risk', [{'key': 'risk.exposure', 'value': 101}], 'value'),
    ('catalog', [{'key': 'price', 'value': 2, 'source': 'unknown'}], 'source'),
])
def test_invalid_rows_are_located_and_cannot_commit(client, section, records, column):
    _, path = create(client)
    result = manual(client, path, section, records)
    assert not result['validation']['ok'] and not result['token']
    assert result['validation']['errors'][0]['column'] == column
    assert ok(client.get(path))['records'] == []


def test_configurable_custom_rules(client):
    _, path = create(client, 'custom')
    _, trading = create(client)
    changes = {'expected_version': 1, 'multipliers': {'price:demand': -2}}
    review = ok(client.post(path + '/rules/preview', json=changes))
    assert review['rule_diff'] == [{'key': 'price:demand', 'before': -.5, 'after': -2}]
    assert client.post(trading + '/rules/preview', json=changes).status_code == 422
    model = commit(client, path, review)
    assert next(edge for edge in model['edges'] if edge['source'] == 'price')['rule']['multiplier'] == -2
    version = lambda section, rows, current: commit(client, path, manual(client, path, section, rows, current))
    version('sales', [{'key': 'demand', 'value': 100}], 2)
    version('catalog', [{'key': 'price', 'value': 10}], 3)
    result = ok(client.post('/api/enterprise/whatif', json={
        'model_id': model['model_id'], 'assumptions': [{'node_id': 'price', 'operation': 'pct', 'value': 10}],
    }))
    assert by_key(result['nodes'])['demand']['after'] == pytest.approx(80)


def test_saved_erp_connector_respects_section_and_preview(client, tmp_path, monkeypatch):
    model, path = create(client)
    source = tmp_path / 'p17-erp.sqlite'
    with sqlite3.connect(source) as db:
        db.execute('CREATE TABLE metrics (field TEXT, value TEXT, date TEXT)')
        db.executemany('INSERT INTO metrics VALUES (?, ?, ?)', [
            ('demand', '100', '2026-11-01'), ('department.finance', 'المالية', None), ('ar', '20', None),
        ])
    monkeypatch.setenv('ATHAR_ERP_P17_SQL', f'sqlite:///{source}')
    connector = ok(client.post(f"/api/companies/{model['company_id']}/connectors/erp", json={'kind': 'sql', 'name': 'ERP',
                   'url_env': 'ATHAR_ERP_P17_SQL', 'table': 'metrics',
                   'mapping': {'node': 'field', 'value': 'value', 'date': 'date'}}), 201)
    def pull(section, version):
        return ok(client.post(f"{path}/sections/{section}/erp/{connector['id']}/preview",
                              json={'expected_version': version}))
    review = pull('sales', 1)
    assert [row['key'] for row in review['diff']] == ['demand']
    with sqlite3.connect(source) as db:
        db.execute("UPDATE metrics SET value='7' WHERE field='demand'")
    model = commit(client, path, review)
    assert model['records'][0]['value'] == 100
    model = commit(client, path, pull('structure', 2))
    assert by_key(model['records'], 'key')['department.finance']['value'] == 'المالية'


def test_company_and_token_permissions(client):
    model, path = create(client)
    _, other = create(client)
    token = manual(client, path, 'sales', [{'key': 'demand', 'value': 100}])['token']
    for role in ['planner', 'viewer']:
        ok(client.post('/api/users', json={'username': role, 'password': 'phase-17-password',
                 'role': role, 'company_ids': [model['company_id']]}), 201)
    sign_in(client, 'planner', 'phase-17-password')
    assert client.get(other).status_code == 404
    assert client.get(path).status_code == 200
    assert client.post(path + '/commit', json={'token': token}).status_code == 404
    assert client.post('/api/onboarding/companies', json={'name': 'شركة', 'sector': 'retail'}).status_code == 403
    assert manual(client, path, 'finance', [{'key': 'ar', 'value': 2}])['token']
    sign_in(client, 'viewer', 'phase-17-password')
    assert client.get(path).status_code == 200
    assert client.post(path + '/commit', json={'token': token}).status_code == 403


def test_migration_preserves_legacy_data_and_reopens(db_path):
    initialize(db_path)
    seed(db_path)
    migrate(db_path)
    migrate(db_path)
    with get_conn(db_path) as db:
        assert db.execute('PRAGMA foreign_key_check').fetchall() == []
        row = db.execute('SELECT name, template FROM companies WHERE id=2').fetchone()
        assert row['template'] == 'production'
        assert db.execute('SELECT company_id FROM datasets WHERE company_id=2').fetchone()[0] == 2
        assert db.execute('SELECT company_id FROM company_models WHERE company_id=1').fetchone()[0] == 1
    with TestClient(create_app(Settings(db_path=db_path, forecast_engine='stat', llm_provider='rules'))) as client:
        sign_in(client)
        model, path = create(client, 'retail')
        assert model['company_id'] == 3
    with TestClient(create_app(Settings(db_path=db_path, forecast_engine='stat', llm_provider='rules'))) as client:
        sign_in(client)
        assert ok(client.get(path)) == model
