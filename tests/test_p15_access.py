import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import DDL, encode, get_conn
from app.main import create_app
from conftest import TEST_ADMIN, TEST_PASSWORD, sign_in

USER_PASSWORD = 'local-account-password'
LOGIN_HEADERS = {'X-Athar-Login': '1'}


def new_account(admin, role, companies=(1,), username=None):
    response = admin.post('/api/users', json={'username': username or role, 'role': role,
                                             'password': USER_PASSWORD, 'company_ids': companies})
    assert response.status_code == 201, response.text
    return response.json()


@pytest.fixture
def draft(db_path):
    with get_conn(db_path) as conn:
        conn.execute(
            """INSERT INTO runs (id, company_id, dataset_id, model_id, kind, result_json, status, wall_s, created_at)
               VALUES (88, 1, 1, 1, 'optimize', ?, 'OPTIMAL', 0, 'test')""",
            (encode({'optimized': {'plan': {'activities': []}, 'kpis': {'total': 12}}}),),
        )
        conn.execute(
            "INSERT INTO plans (id, company_id, run_id, variant, status, created_at) "
            "VALUES (99, 1, 88, 'optimized', 'draft', 'test')",
        )
    return 99


def test_anonymous_requests_and_login_csrf(client):
    with TestClient(client.app) as guest:
        assert guest.get('/api/health').status_code == 200
        assert guest.get('/', follow_redirects=False).headers['location'] == '/login'
        assert guest.get('/login').status_code == 200
        for path in ('/companies', '/templates', '/enterprise/models', '/auth/me', '/users'):
            assert guest.get('/api' + path).status_code == 401
        assert guest.get('/api/connectors/erp').status_code == 401
        assert guest.post('/api/enterprise/chat', json={'message': 'راجع الخطة'}).status_code == 401
        credentials = {'username': TEST_ADMIN, 'password': TEST_PASSWORD}
        assert guest.post('/api/auth/login', json=credentials).status_code == 403
        assert guest.post('/api/auth/login', json=credentials | {'password': 'wrong'},
                          headers=LOGIN_HEADERS).status_code == 401
        assert guest.post('/api/auth/login', json=credentials | {'username': 'unknown'},
                          headers=LOGIN_HEADERS).status_code == 401
        assert not guest.cookies


def test_cookie_hash_csrf_rotation_logout_and_https(client, db_path):
    with TestClient(client.app, base_url='https://testserver') as browser:
        credentials = {'username': TEST_ADMIN, 'password': TEST_PASSWORD}
        response = browser.post('/api/auth/login', json=credentials, headers=LOGIN_HEADERS)
        csrf = response.json()['csrf_token']
        cookie = response.headers['set-cookie'].lower()
        assert all(flag in cookie for flag in ('httponly', 'samesite=strict', 'secure', 'max-age=28800'))
        first_token = browser.cookies['athar_session']
        with get_conn(db_path) as conn:
            user = conn.execute('SELECT password_hash FROM users').fetchone()
            assert user['password_hash'].startswith('$argon2id$')
            assert TEST_PASSWORD not in user['password_hash']
            assert first_token not in {row['token_hash'] for row in conn.execute('SELECT * FROM auth_sessions')}
        assert browser.post('/api/companies/1/datasets/demo').status_code == 403
        signed = sign_in(browser)
        assert browser.cookies['athar_session'] != first_token
        assert signed['csrf_token'] != csrf
        assert browser.post('/api/auth/logout', headers={'X-CSRF-Token': csrf}).status_code == 403
        with TestClient(client.app) as replay:
            replay.cookies.set('athar_session', first_token)
            assert replay.get('/api/auth/me').status_code == 401
        second_token = browser.cookies['athar_session']
        assert browser.post('/api/auth/logout').status_code == 204
        with TestClient(client.app) as replay:
            replay.cookies.set('athar_session', second_token)
            assert replay.get('/api/auth/me').status_code == 401


def test_session_expiry(client, db_path):
    with get_conn(db_path) as conn:
        conn.execute('UPDATE auth_sessions SET expires_at = 0')
    assert client.get('/api/companies').status_code == 401


def test_bootstrap_when_credentials_present_once(db_path, monkeypatch):
    monkeypatch.delenv('ATHAR_ADMIN_PASSWORD')
    settings = Settings(db_path=db_path, forecast_engine='stat')
    with TestClient(create_app(settings)) as locked:
        assert locked.get('/api/companies').status_code == 401
        assert locked.post('/api/auth/login', headers=LOGIN_HEADERS,
                           json={'username': TEST_ADMIN, 'password': TEST_PASSWORD}).status_code == 401
    monkeypatch.setenv('ATHAR_ADMIN_PASSWORD', TEST_PASSWORD)
    with TestClient(create_app(settings)) as first:
        assert sign_in(first)['role'] == 'admin'
    monkeypatch.setenv('ATHAR_ADMIN_USERNAME', 'another')
    monkeypatch.setenv('ATHAR_ADMIN_PASSWORD', USER_PASSWORD)
    with TestClient(create_app(settings)) as next_start:
        sign_in(next_start)
        assert [row['username'] for row in next_start.get('/api/users').json()] == [TEST_ADMIN]


def test_management_changes_access_and_reset_revokes_sessions(client):
    viewer = new_account(client, 'viewer')
    assert client.post('/api/users', json={'username': 'VIEWER', 'password': USER_PASSWORD,
                                          'role': 'planner'}).status_code == 409
    assert client.post('/api/users', json={'username': 'bad', 'password': USER_PASSWORD,
                                          'role': 'planner', 'company_ids': [999]}).status_code == 422
    admin = client.get('/api/auth/me').json()
    assert client.put(f'/api/users/{admin["id"]}', json={'role': 'viewer'}).status_code == 409
    assert client.get('/users').status_code == 200
    path = f'/api/users/{viewer["id"]}'
    with TestClient(client.app) as browser:
        sign_in(browser, 'viewer', USER_PASSWORD)
        assert browser.get('/users').status_code == 403
        assert browser.get('/api/users').status_code == 403
        assert browser.put(path, json={'role': 'admin'}).status_code == 403
        assert client.put(path, json={'role': 'planner', 'company_ids': [2]}).status_code == 200
        assert [row['id'] for row in browser.get('/api/companies').json()] == [2]
        assert browser.get('/api/companies/1/model').status_code == 404
        assert browser.post('/api/companies/2/datasets/demo').status_code == 200
        assert client.put(path, json={'role': 'viewer', 'company_ids': [],
                                      'password': 'the-new-local-password'}).status_code == 200
        assert browser.get('/api/auth/me').status_code == 401


@pytest.mark.parametrize('role, can_write, can_decide', [
    ('viewer', False, False), ('planner', True, False), ('approver', False, True),
])
def test_roles_entity_isolation_and_plan_identity(client, draft, role, can_write, can_decide):
    account = new_account(client, role)
    with TestClient(client.app) as browser:
        sign_in(browser, role, USER_PASSWORD)
        assert [row['id'] for row in browser.get('/api/companies').json()] == [1]
        for path in ('/companies/2/model', '/datasets/2', '/runs/123', '/plans/123/export.json'):
            assert browser.get('/api' + path).status_code == 404
        assert browser.get('/api/datasets/1').status_code == 200
        assert browser.get('/api/runs/88').status_code == 200
        assert browser.post('/api/companies/2/optimize').status_code == (404 if can_write else 403)
        assert browser.post('/api/companies/1/datasets/demo').status_code == (200 if can_write else 403)
        assert browser.post('/api/companies', json={'name': 'Denied', 'template': 'production'}).status_code == 403

        decision = {'decision': 'approved', 'comment': 'قرار الاختبار'}
        path = f'/api/plans/{draft}/decide'
        assert browser.post(path, json=decision | {'actor': 'fake'}).status_code == (422 if can_decide else 403)
        response = browser.post(path, json=decision)
        assert response.status_code == (200 if can_decide else 403)
        events = client.get('/api/companies/1/audit').json()
        if can_decide:
            assert response.json()['actor'] == role
            assert response.json()['decided_by_user_id'] == account['id']
            assert (events[0]['action'], events[0]['actor'], events[0]['user_id']) == (
                'plan.approve', role, account['id'],
            )
        if can_write:
            assert (events[0]['action'], events[0]['actor'], events[0]['user_id']) == (
                'dataset.demo', role, account['id'],
            )


def test_direct_ids_cannot_bypass_company_checks(client, draft):
    new_account(client, 'approver', (2,))
    with TestClient(client.app) as outsider:
        sign_in(outsider, 'approver', USER_PASSWORD)
        assert outsider.get('/api/runs/88').status_code == 404
        assert outsider.get('/api/companies/1/p6-imports').status_code == 404
        assert outsider.get(f'/api/plans/{draft}/export.json').status_code == 404
        assert outsider.post(f'/api/plans/{draft}/decide', json={
            'decision': 'approved', 'comment': '',
        }).status_code == 404
    assert client.get('/api/companies/1/plans').json()[0]['status'] == 'draft'


def test_enterprise_models_and_scenarios_follow_company_access(client):
    base = '/api/enterprise'
    legacy = client.post(base + '/models/demo').json()
    new_account(client, 'planner')
    new_account(client, 'viewer', (2,))
    with TestClient(client.app) as planner, TestClient(client.app) as viewer:
        sign_in(planner, 'planner', USER_PASSWORD)
        sign_in(viewer, 'viewer', USER_PASSWORD)
        assert planner.post(base + '/models/demo').status_code == 404
        assert planner.post(base + '/models/demo?company_id=2').status_code == 404
        model = planner.post(base + '/models/demo?company_id=1').json()
        assert model['company_id'] == 1
        scenario = planner.post(base + '/scenarios', json={'name': 'مراجعة',
                                'model_id': model['id'], 'assumptions': [
                                    {'node_id': 'credit', 'operation': 'pct', 'value': -10},
                                ]})
        assert scenario.status_code == 201
        assert scenario.json()['company_id'] == 1
        assert [row['id'] for row in planner.get(base + '/models').json()] == [model['id']]
        assert viewer.get(base + '/models').json() == viewer.get(base + '/scenarios').json() == []
        external = f'{base}/models/{model["id"]}'
        for path in (external, external + '/versions', external + '/versions/1',
                     base + f'/whatif/drivers?model_id={model["id"]}'):
            assert viewer.get(path).status_code == 404
        assert viewer.post(base + '/whatif', json={'assumptions': []}).status_code == 403
        assert planner.delete(f'{base}/models/{legacy["id"]}').status_code == 404
        assert planner.put(f'{base}/models/{legacy["id"]}/company/1').status_code == 403
        assert client.put(f'{base}/models/{legacy["id"]}/company/1').status_code == 200
        assert planner.get(f'{base}/models/{legacy["id"]}').status_code == 200
        assert planner.get('/api/companies/1/audit').json()[1]['user_id'] is not None


def test_upgrade_preserves_historical_actor(db_path):
    with get_conn(db_path) as conn:
        conn.executescript(DDL)
        conn.execute(
            """INSERT INTO audit_log (company_id, actor, action, entity, ts)
               VALUES (1, 'مراجع قديم', 'plan.approve', 'plan', 'test')""",
        )
    with TestClient(create_app(Settings(db_path=db_path, forecast_engine='stat'))) as browser:
        sign_in(browser)
        entry = browser.get('/api/companies/1/audit').json()[-1]
        assert entry['actor'] == 'مراجع قديم'
        assert entry['user_id'] is None
