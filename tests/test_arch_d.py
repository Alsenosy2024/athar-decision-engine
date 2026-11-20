"""Acceptance of template-free company onboarding, hierarchy, and connector ownership."""
from pathlib import Path
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from app.db import get_conn, initialize
from conftest import sign_in

FIXTURES = Path(__file__).with_name('fixtures')
CSV = FIXTURES / 'arch_d_retail_sales.csv'
XLSX = FIXTURES / 'arch_d_retail_ledger.xlsx'


def ok(response, status=200):
    assert response.status_code == status, response.text[:1800]
    return response.json()


def company(client, name):
    return ok(client.post('/api/onboarding/companies', json={'name': name, 'sector': 'enterprise_company'}), 201)


def ai(model):
    return f"/api/companies/{model['company_id']}/ai"


def preview(client, model, *files, **form):
    uploads = [('files', (path.name, path.read_bytes())) if isinstance(path, Path) else ('files', path)
               for path in files]
    return ok(client.post(ai(model) + '/entry', data={'model_id': model['model_id'], **form}, files=uploads), 201)


def resolve(client, model, review, status='accepted'):
    return client.post(ai(model) + f"/reviews/{review['id']}/decide", json={'status': status})


def model_path(model):
    return f"/api/enterprise/models/{model['model_id']}"


def graph(client, model, suffix=''):
    return ok(client.get(model_path(model) + suffix))['graph']


def test_raw_retail_two_inputs_one_company_one_review_and_real_rollups(client):
    retail = company(client, 'تجزئة المدن')
    empty = graph(client, retail)
    assert 'onboarding' not in empty and not empty['arms'] and not empty['edges']
    draft = preview(client, retail, CSV, XLSX)
    assert (draft['company_id'], draft['model_id'], draft['model_version'], draft['status']) == (
        retail['company_id'], retail['model_id'], 1, 'pending')
    data = draft['data']
    assert data['provider'] == 'rules' and data['validation']['ok']
    assert [source['sheet'] for source in data['sources']] == ['CSV', 'حركات دفتر الأستاذ', 'دليل الحسابات']
    assert data['validation']['summary']['observations'] == 16
    assert data['sources'][0]['mapping']['dimensions']['branch'] == 'store_id'
    assert graph(client, retail) == empty
    assert ok(resolve(client, retail, draft))['outcome'] == {'model_id': retail['model_id'], 'version': 2}
    assert resolve(client, retail, draft).status_code == 409
    assert graph(client, retail, '/versions/1') == empty

    saved = graph(client, retail)
    company_data = saved['company_data']
    entities = {(entity['kind'], entity['code']): entity for entity in company_data['entities'].values()}
    for kind, child, parent in [('branch', 'RYD', 'SA'), ('branch', 'JED', 'SA'),
                                 ('department', 'الأغذية', 'التجزئة'), ('account', '4101', '4100'),
                                 ('cost_center', 'CC-R', 'CC-SA')]:
        assert entities[kind, child]['parent_id'] == entities[kind, parent]['id']
    assert entities['account', '4100']['name'] == 'إيرادات التجزئة'
    assert entities['product', 'A01']['id'] != entities['product', 'B02']['id']
    assert all(len(item['source']['sha256']) == 64 and item['source']['rows'] for item in company_data['observations'])
    assert all(edge['kind'] == 'aggregation' and edge['source'] == 'approved_dimension_rollup'
               for edge in data['relations'])
    nodes = {node['id']: node for node in saved['nodes']}

    def series(key):
        return [point['value'] for point in nodes[key]['baseline']['points']]

    assert company_data['node_measures']['pnl.revenue'] == 'sales'
    assert series('pnl.revenue') == [320, 500]
    assert series('inventory.company') == [35, 30]
    assert series('company.amount') == [320, 500]
    assert series('amount.' + entities['account', '4101']['id']) == [120, 200]
    assert series('amount.' + entities['account', '4102']['id']) == [200, 300]
    assert series('amount.' + entities['cost_center', 'CC-SA']['id']) == [320, 500]
    assert series('sales.' + entities['product', 'A01']['id']) == [120, 200]

    driver = 'sales.' + entities['branch', 'RYD']['id']
    response = ok(client.post('/api/enterprise/whatif', json={
        'model_id': retail['model_id'], 'version': 2, 'weeks': 1, 'n': 1,
        'assumptions': [{'node_id': driver, 'operation': 'add', 'value': 10}],
    }))
    revenue = next(row for row in response['nodes'] if row['id'] == 'pnl.revenue')
    assert (revenue['before'], revenue['after']) == (500, 510)
    assert ok(client.get('/api/enterprise/map', params={'company_id': retail['company_id']}))['model']['version'] == 2
    map_data = ok(client.get('/api/enterprise/map', params={'company_id': retail['company_id']}))
    assert {'الرياض', 'جدة', 'الأغذية', 'الأجهزة'} <= {item['name_ar'] for item in map_data['departments']}
    assert map_data['context']['data_from'] == '2026-11-02'
    no_change = preview(client, retail, CSV, XLSX)
    assert no_change['model_version'] == 2 and not no_change['data']['model']
    assert all(item['reused'] for item in no_change['data']['sources'])
    assert resolve(client, retail, no_change).status_code == 422
    assert client.post(model_path(retail) + '/imports/apply', json={
        'expected_version': 2, 'updates': [{'node_id': 'pnl.revenue', 'baseline': nodes['pnl.revenue']['baseline']
                                             | {'points': [{'date': '2026-11-09', 'value': 4}]}}]
    }).status_code == 422


def test_mapping_review_stale_rejection_and_automatic_reuse(client):
    retail = company(client, 'شركة بربط مخصص')
    headers = 'who,when,how_much\n'
    data = ('custom.csv', (headers + 'R,2026-11-02,4\n').encode())
    assert not preview(client, retail, data)['data']['validation']['ok']
    mapping = {'day': 'when', 'dimensions': {'branch': 'who'}, 'measures': [
        {'column': 'how_much', 'key': 'quantity', 'name': 'الكمية', 'unit': 'units', 'aggregation': 'sum'}]}
    draft = preview(client, retail, data, company_mapping=json.dumps({'0:CSV': mapping}))
    obsolete = preview(client, retail, data, company_mapping=json.dumps({'0:CSV': mapping}))
    ok(resolve(client, retail, draft))
    assert resolve(client, retail, obsolete).status_code == 409
    correction = preview(client, retail, ('custom.csv', (headers + 'R,2026-11-02,9\n').encode()))
    assert correction['data']['sources'][0]['reused']
    ok(resolve(client, retail, correction, 'rejected'))
    assert graph(client, retail)['company_data']['observations'][0]['value'] == 4
    amended = preview(client, retail, ('custom.csv', (headers + 'R,2026-11-02,9\n').encode()))
    assert amended['data']['diff'][0]['before'] == 4
    assert ok(resolve(client, retail, amended))['outcome']['version'] == 3
    readings = graph(client, retail)['company_data']['observations']
    assert len(readings) == 1 and readings[0]['value'] == 9
    assert client.post(ai(retail) + '/entry', files={'files': data},
                       data={'expected_version': 1}).status_code == 409


def test_hierarchy_rejects_cycles_and_does_not_sum_snapshot_duplicates(client):
    retail = company(client, 'اختبار الهيكل')
    draft = preview(client, retail, ('cycles.csv', b'branch,branch_parent\nA,B\nB,A\n'))
    assert not draft['data']['validation']['ok']
    assert resolve(client, retail, draft).status_code == 422
    duplicate = preview(client, retail, ('closing.csv', b'date,branch,stock_units\n2026-01-01,A,4\n2026-01-01,A,5\n'))
    assert not duplicate['data']['validation']['ok']
    assert graph(client, retail)['company_data']['observations'] == []


def test_company_and_connector_isolation_with_legacy_assignment(client, db_path, monkeypatch):
    first, second = [company(client, name) for name in ('شركة أ', 'شركة ب')]
    config = {'kind': 'rest', 'name': 'تهيئة سابقة', 'url_env': 'ATHAR_ERP_QA',
              'mapping': {'node': 'node', 'value': 'value'}}
    with get_conn(db_path) as conn:
        conn.execute('DROP TABLE erp_connectors')
        conn.execute('CREATE TABLE erp_connectors (id INTEGER PRIMARY KEY, config_json TEXT, created_at TEXT)')
        conn.execute('INSERT INTO erp_connectors VALUES (?, ?, ?)', (88, json.dumps(config), '2026-11-20'))
    initialize(db_path)
    legacy = '/api/erp/legacy-connectors'
    assert ok(client.get(legacy))[0]['id'] == 88

    def erp_url(model):
        return f"/api/companies/{model['company_id']}/connectors/erp"

    def claim(model):
        return erp_url(model) + '/88/claim'

    assert ok(client.get(erp_url(first))) == ok(client.get(erp_url(second))) == []
    assert ok(client.post(claim(first)))['id'] == 88
    assert client.post(claim(second)).status_code == 404
    connector_b = ok(client.post(erp_url(second), json=config | {'name': 'الموصل ب'}), 201)
    assert [row['id'] for row in ok(client.get(erp_url(first)))] == [88]
    assert [row['id'] for row in ok(client.get(erp_url(second)))] == [connector_b['id']]
    assert ok(client.get(legacy)) == []
    with get_conn(db_path) as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute('UPDATE erp_connectors SET company_id=? WHERE id=88', (second['company_id'],))
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO erp_connectors (config_json) VALUES ('{}')")

    draft_a = preview(client, first, CSV, XLSX)
    draft_b = preview(client, second, ('retail_b.csv', b'date,store_id,net_sales_sar\n2026-11-02,RYD,777\n'))
    ok(resolve(client, first, draft_a))
    ok(resolve(client, second, draft_b))
    assert client.get(ai(second) + f"/reviews/{draft_a['id']}").status_code == 404
    assert resolve(client, second, draft_a).status_code == 404
    assert client.post(ai(second) + '/entry', files={'files': (CSV.name, CSV.read_bytes())},
                       data={'model_id': first['model_id']}).status_code == 404
    assert graph(client, first)['company_data']['observations'] != graph(client, second)['company_data']['observations']

    uid = 'arch-d-planner'
    ok(client.post('/api/users', json={'username': uid, 'password': 'fixture-test-password', 'role': 'planner',
                                       'company_ids': [second['company_id']]}), 201)

    def denied_pull(config):
        pytest.fail('Cross-company preview must never reach the upstream connector')

    monkeypatch.setattr('app.connectors.erp.pull', denied_pull)
    with TestClient(client.app) as planner:
        sign_in(planner, uid, 'fixture-test-password')
        assert planner.get(legacy).status_code == 403
        assert planner.get(erp_url(first)).status_code == 404
        assert planner.post(claim(second)).status_code == 403
        assert planner.get(model_path(first)).status_code == 404
        assert planner.post(model_path(second) + '/imports/erp/88/preview').status_code == 404
        assert planner.post(model_path(first) + f"/imports/erp/{connector_b['id']}/preview").status_code == 404


def test_raw_forecast_resolves_the_company_model_without_sector_template(client):
    retail = company(client, 'تجزئة بتاريخ أسبوعي')
    contents = b'date,store_id,net_sales_sar\n' + b'\n'.join(
        f'2026-11-{day:02},A,{value}'.encode()
        for day, value in [(2, 100), (9, 110), (16, 120), (23, 130), (30, 140)])
    ok(resolve(client, retail, preview(client, retail, ('sales.csv', contents))))
    forecast = ok(client.post(ai(retail) + '/forecast', json={'node_id': 'pnl.revenue', 'horizon': 2}), 201)
    assert forecast['model_id'] == retail['model_id'] and forecast['model_version'] == 2
    assert len(forecast['data']['points']) == 2
    assert all('date' in point for point in forecast['data']['points'])


def test_raw_erp_pull_uses_the_same_pinned_review_with_company_scope(client, tmp_path, monkeypatch):
    models = [company(client, name) for name in ('تجزئة الرياض', 'تجزئة جدة')]
    connectors = []
    for index, model in enumerate(models):
        path = tmp_path / f'erp-{index}.sqlite'
        with sqlite3.connect(path) as db:
            db.execute('CREATE TABLE sales (store_id TEXT, net_sales_sar INTEGER, txn_day TEXT, department TEXT)')
            db.execute('INSERT INTO sales VALUES (?, ?, ?, ?)', ('B', 100 + index, '2026-11-02', 'التجزئة'))
        environment = f'ATHAR_ERP_D_QA_{index}'
        monkeypatch.setenv(environment, f'sqlite:///{path}')
        config = {'kind': 'sql', 'name': f'ERP {index}', 'url_env': environment, 'table': 'sales',
                  'mapping': {'node': 'store_id', 'value': 'net_sales_sar', 'date': 'txn_day'},
                  'source_columns': ['department']}
        connectors.append(ok(client.post(f"/api/companies/{model['company_id']}/connectors/erp",
                                         json=config), 201)['id'])
    for index, model in enumerate(models):
        base = f"/api/companies/{model['company_id']}/connectors/erp"
        request = {'model_id': model['model_id'], 'expected_version': 1}
        assert client.post(f'{base}/{connectors[1 - index]}/entry', json=request).status_code == 404
        draft = ok(client.post(f'{base}/{connectors[index]}/entry', json=request), 201)
        assert draft['model_version'] == 1 and draft['status'] == 'pending'
        assert draft['data']['sources'][0]['mapping']['dimensions']['department'] == 'department'
        assert draft['data']['diff'][0]['value'] == 100 + index
        assert not graph(client, model)['company_data']['observations']
        ok(resolve(client, model, draft))
