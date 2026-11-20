"""Finding 04, 06, 10 and 12 data contracts."""
from fastapi.testclient import TestClient

from app.db import get_conn, initialize
from conftest import sign_in
from test_p15_access import USER_PASSWORD, new_account
from test_p17_onboarding import by_key, commit, create, manual, ok
from test_p21_living_map import ASSUMPTIONS, graph

MAP = '/api/enterprise/map'
SCENARIOS = '/api/enterprise/scenarios'
WHATIF = '/api/enterprise/whatif'


def test_demo_scope_and_result_provenance(client, db_path):
    company = ok(client.get('/api/companies'))[0]
    selected = ok(client.get(MAP, params={'company_id': company['id']}))
    demo = ok(client.get(MAP))
    assert selected['model'] is None and not selected['is_demo']
    assert demo['company'] is None and demo['context']['company_id'] is None
    assert demo['context']['model_version'] == 1
    assert 'تجريبية' in demo['context']['source']

    synthetic = {'name': 'فرضية روافد', 'assumptions': [
        {'node_id': 'credit', 'operation': 'pct', 'value': -10}]}
    assert client.post(SCENARIOS, params={'company_id': company['id']}, json=synthetic).status_code == 422
    row = ok(client.post(SCENARIOS, json=synthetic), 201)
    assert row['company_id'] is None

    # An old mislabeled demo row stays in storage without appearing in workspace comparisons.
    with get_conn(db_path) as conn:
        conn.execute("UPDATE enterprise_scenarios SET company_id=? WHERE id=?", (company['id'], row['id']))
    assert ok(client.get(SCENARIOS)) == []

    model = ok(client.post('/api/enterprise/models', params={'company_id': company['id']}, json=graph()), 201)
    body = {'model_id': model['id'], 'version': 1, 'assumptions': ASSUMPTIONS}
    result = ok(client.post(WHATIF, json={**body, 'weeks': 13}))
    scope = result['context']
    assert scope['company_name'] == company['name']
    assert scope['company_id'] == company['id'] and scope['model_version'] == 1
    assert scope['source'] == 'بيانات النموذج المؤسسي المحفوظ'
    assert scope['calculation_weeks'] == 13
    answer = ok(client.post('/api/enterprise/chat', json={
        'model_id': model['id'], 'version': 1, 'message': 'خفض الطلب 20%', 'parse_only': True,
    }))
    assert answer['context']['company_name'] == scope['company_name']
    assert answer['context']['source'] == scope['source']


def weekly_company(client):
    model, path = create(client)
    for section, records in {
        'sales': [{'key': 'demand', 'value': 100, 'date': '2026-11-09'}],
        'catalog': [{'key': 'price', 'value': 25}, {'key': 'unit_cost', 'value': 10}],
        'finance': [{'key': key, 'value': value} for key, value in {
            'cash.opening': 1000, 'ar': 0, 'ap': 800, 'pnl.expenses': 100, 'financing': 10,
        }.items()],
    }.items():
        model = commit(client, path, manual(client, path, section, records, model['version']))
    return model


def test_weekly_cash_period_matches_calculation_and_comparison(client):
    model = weekly_company(client)
    selection = {'model_id': model['model_id'], 'version': model['version']}
    results = [ok(client.post(WHATIF, json={
        **selection, 'weeks': weeks, 'assumptions': ASSUMPTIONS,
    })) for weeks in (1, 52, 104)]
    assert results[0] == results[1] == results[2]
    result = results[0]
    assert result['context']['weekly_snapshot'] and result['context']['calculation_weeks'] == 1
    assert result['context']['data_from'] == result['context']['data_to'] == '2026-11-09'
    rows = by_key(result['nodes'])
    assert (rows['cash']['before'], rows['cash']['after']) == (1590, 1290)
    assert (rows['pnl.result']['before'], rows['pnl.result']['after']) == (1390, 1090)
    assert result['charts']['cash'] == {'period_ar': 'نهاية الأسبوع', 'before': [1590], 'after': [1290]}

    scenarios = [ok(client.post(SCENARIOS, json={
        **selection, 'name': name, 'assumptions': ASSUMPTIONS,
    }), 201)['id'] for name in ('الخطة الأولى', 'الخطة الثانية')]
    compared = ok(client.post(SCENARIOS + '/compare', json={'scenario_ids': scenarios, 'weeks': 104, 'n': 3}))
    assert compared['settings']['weeks'] == compared['context']['calculation_weeks'] == 1
    assert compared['scenarios'][0]['kpis']['cash_low'] == 1290


def test_units_and_period_labels_match_map_and_result(client):
    model = weekly_company(client)
    payload = ok(client.get(MAP, params={'company_id': model['company_id']}))
    labels = by_key(payload['kpis'])
    assert labels['cash_low']['label'] == 'رصيد السيولة التقديري بنهاية الأسبوع'
    assert labels['profit']['label'] == 'الربح الأسبوعي بعد التمويل'
    assert labels['cash_low']['unit'] == labels['profit']['unit'] == 'ر.س'
    assert labels['credit_breach']['unit'] == '%'
    assert labels['delays']['unit'] == 'أسبوع'
    demo = by_key(ok(client.get(MAP))['kpis'])
    assert demo['cash_low']['label'] == 'أقل رصيد سيولة'


def test_erp_isolation_for_two_company_planners_and_admin(client, db_path, tmp_path, monkeypatch):
    path = tmp_path / 'scoped-erp.sqlite'
    with get_conn(path) as conn:
        conn.execute('CREATE TABLE metrics (node TEXT, value INTEGER)')
        conn.execute("INSERT INTO metrics VALUES ('cash', 125)")
    monkeypatch.setenv('ATHAR_ERP_FIXC_TEST_URL', f'sqlite:///{path}')
    config = {'kind': 'sql', 'name': 'مصدر الاختبار', 'url_env': 'ATHAR_ERP_FIXC_TEST_URL', 'table': 'metrics',
              'mapping': {'node': 'node', 'value': 'value'}}
    erp = {i: f'/api/companies/{i}/connectors/erp' for i in (1, 2)}
    connectors = {i: ok(client.post(erp[i], json=config | {'name': f'ERP {i}'}), 201)['id'] for i in erp}
    models = {i: ok(client.post('/api/enterprise/models', params={'company_id': i}, json=graph()), 201)['id']
              for i in erp}
    for i, j in ((1, 2), (2, 1)):
        username = f'fixc-planner-{i}'
        new_account(client, 'planner', [i], username)
        with TestClient(client.app) as planner:
            sign_in(planner, username, USER_PASSWORD)
            assert [row['id'] for row in ok(planner.get(erp[i]))] == [connectors[i]]
            assert planner.get(erp[j]).status_code == 404
            assert planner.post(erp[i], json=config).status_code == 403
            assert planner.put(f'{erp[i]}/{connectors[i]}', json=config).status_code == 403
            own_model = f'/api/enterprise/models/{models[i]}/imports/erp'
            assert ok(planner.post(f'{own_model}/{connectors[i]}/preview'))['validation']['ok']
            assert planner.post(f'{own_model}/{connectors[j]}/preview').status_code == 404
            assert planner.post(f'/api/enterprise/models/{models[j]}/imports/erp/{connectors[i]}/preview').status_code == 404
    assert client.put(f'{erp[1]}/{connectors[2]}', json=config).status_code == 404
    assert ok(client.put(f'{erp[1]}/{connectors[1]}', json=config))['id'] == connectors[1]
    onboarding, onboarding_path = create(client)
    assert client.post(f"{onboarding_path}/sections/sales/erp/{connectors[1]}/preview",
                       json={'expected_version': onboarding['version']}).status_code == 404
    assert client.get('/api/connectors/erp').status_code == 404
    with get_conn(db_path) as conn:
        audits = conn.execute("SELECT company_id FROM audit_log WHERE action='erp.create' ORDER BY id").fetchall()
        assert [row['company_id'] for row in audits] == [1, 2]


def test_legacy_erp_migration_preserves_unassigned_configuration(client, db_path):
    with get_conn(db_path) as conn:
        conn.execute('DROP TABLE erp_connectors')
        conn.execute('CREATE TABLE erp_connectors (id INTEGER PRIMARY KEY, config_json TEXT, created_at TEXT)')
        conn.execute("INSERT INTO erp_connectors VALUES (88, '{}', '2026-11-20')")
    initialize(db_path)
    initialize(db_path)
    assert ok(client.get('/api/companies/1/connectors/erp')) == []
    with get_conn(db_path) as conn:
        row = conn.execute('SELECT * FROM erp_connectors WHERE id=88').fetchone()
        assert row['company_id'] is None and row['config_json'] == '{}'
        assert any(row['from'] == 'company_id' and row['table'] == 'companies'
                   for row in conn.execute('PRAGMA foreign_key_list(erp_connectors)'))
