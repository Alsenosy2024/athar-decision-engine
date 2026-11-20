"""Phase 25: public contracts, credential boundaries, framing, and local delivery."""
import json
import os
from contextlib import contextmanager
from hashlib import sha256
from hmac import compare_digest, digest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Event as ThreadEvent, Thread
from urllib.parse import urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient
from playwright.sync_api import expect, sync_playwright

from app.config import Settings
from app.db import audit, get_conn, utc_now
from app.integrations import alerts, credentials, webhooks
from app.prescriptive import records
from qa.phase16_browser import PASSWORD, server
from test_p15_access import USER_PASSWORD, new_account
from test_p21_living_map import ASSUMPTIONS, graph
from conftest import sign_in

API = '/api/v1/companies'
SCOPE = ['kpis:read', 'map:read', 'whatif:run']
SECRET = 'local-webhook-fixture-secret-32-bytes'


@pytest.fixture(autouse=True)
def clean_integration_env(monkeypatch):
    for variable in ('ATHAR_WEBHOOKS', 'ATHAR_EMBED_SECRET', 'ATHAR_FRAME_ANCESTORS'):
        monkeypatch.delenv(variable, raising=False)


def create_key(client, company=1, scopes=SCOPE):
    response = client.post(f'/api/companies/{company}/integration-keys', json={'name': 'ERP', 'scopes': scopes})
    assert response.status_code == 201, response.text
    assert response.headers['Cache-Control'] == 'no-store'
    return response.json()


def headers(key):
    return {'X-Athar-API-Key': key['key']}


def create_model(client, company=1, name='شركة أفق للاختبار'):
    response = client.post(f'/api/enterprise/models?company_id={company}', json=graph(name))
    assert response.status_code == 201, response.text
    return response.json()


def test_key_admin_hash_revocation_and_scope(client, db_path):
    create_model(client)
    key = create_key(client, scopes=['map:read'])
    base = f'{API}/1'
    assert client.get(f'{base}/map').status_code == 401  # A session does not grant v1 access.
    assert client.get(f'{base}/map', headers=headers({'key': 'athar_' + 'a' * 43})).status_code == 401
    assert client.get(f'{base}/map', headers=headers(key)).status_code == 200
    assert client.get(f'{base}/kpis', headers=headers(key)).status_code == 403
    assert client.post(f'{base}/whatif', headers=headers(key), json={'assumptions': ASSUMPTIONS}).status_code == 403
    assert client.get(f'{API}/2/map', headers=headers(key)).status_code == 403
    assets = '/api/companies/1/integration-keys'
    assert client.get(assets).json() == [{k: v for k, v in key.items() if k != 'key'}]
    with get_conn(db_path) as connection:
        row = dict(connection.execute('SELECT * FROM integration_api_keys').fetchone())
        assert row['key_hash'] == sha256(key['key'].encode()).hexdigest()
        assert key['key'] not in str(row)

    with TestClient(client.app) as anonymous:
        assert anonymous.get(assets).status_code == 401
        assert anonymous.get(f'{base}/map', headers=headers(key)).status_code == 200
    new_account(client, 'planner', username='qa-planner')
    sign_in(client, 'qa-planner', USER_PASSWORD)
    assert client.get(assets).status_code == 403
    assert client.post(assets, json={'name': 'ERP', 'scopes': SCOPE}).status_code == 403
    sign_in(client)
    assert client.delete(f'/api/companies/2/integration-keys/{key["id"]}').status_code == 404
    for _ in range(2):
        assert client.delete(f'{assets}/{key["id"]}').status_code == 204
    assert client.get(f'{base}/map', headers=headers(key)).status_code == 401
    with get_conn(db_path) as connection:
        actions = connection.execute("SELECT action, details_json FROM audit_log WHERE action LIKE 'integration.%'").fetchall()
        assert [row['action'] for row in actions] == ['integration.key.create', 'integration.key.revoke']
        assert key['key'] not in str([row['details_json'] for row in actions])


def test_v1_map_kpis_whatif_match_existing_engine(client, db_path):
    model = create_model(client)
    model_id = model['id']
    other = create_model(client, company=2)
    key = create_key(client)
    auth = headers(key)
    base = f'{API}/1'
    live_map = client.get('/api/enterprise/map', params={'company_id': 1, 'model_id': model_id}).json()
    public_map = client.get(f'{base}/map', headers=auth)
    assert public_map.status_code == 200, public_map.text
    assert public_map.headers['Cache-Control'] == 'no-store'
    internal_fields = {'is_demo', 'models', 'flow_source', 'flow_roads', 'process_mining', 'decision_outcomes'}
    assert public_map.json() == {key: value for key, value in live_map.items() if key not in internal_fields}
    baseline = client.get(f'{base}/kpis', headers=auth, params={'weeks': 8}).json()
    assert baseline['weeks'] == 8 and baseline['kpis']['profit'] == 240
    body = {'model_id': model_id, 'assumptions': ASSUMPTIONS, 'weeks': 8, 'n': 4, 'seed': 42}
    core = client.post('/api/enterprise/whatif', json=body).json()
    scenario = client.post(f'{base}/whatif', json=body, headers=auth)
    assert scenario.status_code == 200, scenario.text
    assert scenario.json() == {**core, 'company_id': 1}
    assert any(row['id'] == 'pnl.result' and row['after'] == 200 for row in scenario.json()['nodes'])
    assert client.post(f'{base}/whatif', json={**body, 'model_id': other['id']}, headers=auth).status_code == 404
    assert client.get(f'{base}/map', headers=auth, params={'version': 1}).status_code == 422
    assert client.get(f'{base}/map', headers=auth, params={'model_id': 2**63}).status_code == 422
    assert client.get(f'{base}/map', headers=auth, params={'model_id': model_id, 'version': 2}).status_code == 404
    with get_conn(db_path) as connection:
        assert connection.execute('SELECT COUNT(*) FROM enterprise_scenarios').fetchone()[0] == 0
    assert client.delete(f'/api/enterprise/models/{model_id}').status_code == 204
    assert client.get(f'{base}/map', headers=auth, params={'model_id': model_id, 'version': 1}).status_code == 404


def test_v1_onboarding_company_and_missing_model(client):
    empty_key = create_key(client)
    assert client.get(f'{API}/1/map', headers=headers(empty_key)).status_code == 404
    created = client.post('/api/onboarding/companies', json={'name': 'توزيع الاختبار', 'sector': 'trading'}).json()
    cid = created['company_id']
    key = create_key(client, company=cid)
    public_map = client.get(f'{API}/{cid}/map', headers=headers(key))
    assert public_map.status_code == 200, public_map.text
    assert public_map.json()['readiness'] == 0
    assert public_map.json()['model']['id'] == created['model_id']
    assert all(node['source'] == 'missing' for node in public_map.json()['nodes'])
    baseline = client.get(f'{API}/{cid}/kpis', headers=headers(key), params={'weeks': 104}).json()
    assert baseline['weeks'] == baseline['context']['calculation_weeks'] == 104
    assert all(value is None for value in baseline['kpis'].values())

    onboarding = f'/api/companies/{cid}/onboarding'
    draft = client.post(onboarding + '/sections/sales/manual/preview', json={
        'expected_version': 1, 'records': [{'key': 'demand', 'value': 120, 'source': 'actual'}],
    }).json()
    assert client.post(onboarding + '/commit', json={'token': draft['token']}).status_code == 200
    response = client.post(f'{API}/{cid}/whatif', headers=headers(key),
                           json={'assumptions': ASSUMPTIONS, 'weeks': 1, 'n': 4})
    assert response.status_code == 200, response.text
    result = response.json()
    demand = next(node for node in result['nodes'] if node['id'] == 'demand')
    assert (demand['before'], demand['after'], demand['source'], demand['scenario_source']) == (
        120, 96, 'actual', 'estimated',
    )
    assert result['onboarding']['score'] > 0
    assert result['onboarding']['fields'][0]['item'] == ['department', 'اسم القسم']


def test_openapi_is_versioned_and_specifies_auth(client):
    response = client.get('/api/v1/openapi.json')
    assert response.status_code == 200
    schema = response.json()
    assert len(schema['paths']) == 4
    assert schema['components']['securitySchemes']['AtharApiKey']['name'] == 'X-Athar-API-Key'
    for path, methods in schema['paths'].items():
        assert path.startswith('/api/v1/companies/')
        assert all(operation['security'] == [{'AtharApiKey': []}] for operation in methods.values())
    assert 'ImpactNode' in schema['components']['schemas']


def test_embed_signature_expiry_revocation_and_snapshot(client, monkeypatch):
    now = [1_700_000_000]
    monkeypatch.setattr(credentials, 'time', type('Clock', (), {'time': lambda: now[0]}))
    model_id = create_model(client, name='نموذج الإصدار الأول </script>')['id']
    key = create_key(client, scopes=['map:read'])
    endpoint = f'{API}/1/embed-token'
    assert client.post(endpoint, json={'expires_in': 301}, headers=headers(key)).status_code == 422
    response = client.post(endpoint, json={'expires_in': 2}, headers=headers(key))
    assert response.status_code == 201
    link = response.json()['path']
    assert response.json()['expires_at'] == now[0] + 2
    assert client.get('/embed/map').status_code == 401
    assert client.get(link + 'a').status_code == 401
    assert client.get(link[:-1] + ('A' if link[-1] != 'A' else 'B')).status_code == 401

    updated = client.put(f'/api/enterprise/models/{model_id}', json=graph('نموذج الإصدار الثاني') | {
        'expected_version': 1,
    })
    assert updated.status_code == 200, updated.text
    document = client.get(link)
    assert document.status_code == 200
    assert 'نموذج الإصدار الأول \\u003c/script>' in document.text and 'نموذج الإصدار الثاني' not in document.text
    assert document.headers['Referrer-Policy'] == 'no-referrer'
    assert document.headers['Cache-Control'] == 'no-store'
    assert document.headers['Content-Security-Policy'].endswith("frame-ancestors 'self'")
    now[0] += 2
    assert client.get(link).status_code == 401
    new_link = client.post(endpoint, json={}, headers=headers(key)).json()['path']
    assert client.delete(f'/api/companies/1/integration-keys/{key["id"]}').status_code == 204
    assert client.get(new_link).status_code == 401


@pytest.mark.parametrize('origins', ['*', 'https://erp.example/path', "https://erp.example; 'self'", "'none' 'self'"])
def test_invalid_frame_policy(origins, monkeypatch):
    monkeypatch.setenv('ATHAR_FRAME_ANCESTORS', origins)
    with pytest.raises(ValueError):
        credentials.from_env()


@pytest.fixture
def receiver():
    requests = []
    received = ThreadEvent()

    class Receiver(BaseHTTPRequestHandler):
        def do_POST(self):
            data = self.rfile.read(int(self.headers['Content-Length']))
            requests.append((dict(self.headers), data))
            self.send_response(503 if len(requests) == 1 else 204)
            self.end_headers()
            received.set()

        def log_message(self, *_args):
            pass

    with ThreadingHTTPServer(('127.0.0.1', 0), Receiver) as httpd:
        thread = Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            yield f'http://127.0.0.1:{httpd.server_port}/receiver', requests, received
        finally:
            httpd.shutdown()
            thread.join()


def test_approval_and_alert_outbox_signed_retries_and_dedup(client, db_path, receiver):
    url, requests, _ = receiver
    destination = webhooks.Destination(name='test', company_id=1, url=url, secret=SECRET,
                                       events=['alert.triggered', 'decision.approved'])
    destinations = {destination.identity: destination}
    webhooks.initialize(db_path, destinations)
    model_id = create_model(client)['id']
    other_model = create_model(client, company=2)['id']
    base = f'/api/enterprise/models/{model_id}/prescriptive'
    other_base = f'/api/enterprise/models/{other_model}/prescriptive'
    with get_conn(db_path) as connection:
        forecast = {'kpis': {'profit': 240, 'cash_low': 500}, 'weeks': 8}
        approved = records.create_draft(connection, model_id, 1, 'اختبار الاعتماد', forecast)
        rejected = records.create_draft(connection, model_id, 1, 'اختبار الرفض', forecast)
        unrelated = records.create_draft(connection, other_model, 1, 'شركة تانية', forecast)
        audit(connection, 1, 'enterprise.decision.approved', 'enterprise_decision', approved)
        connection.rollback()  # The approval event shares the audit transaction.
    with get_conn(db_path) as connection:
        assert connection.execute('SELECT COUNT(*) FROM integration_webhook_outbox').fetchone()[0] == 0
    solved = client.post('/api/companies/1/optimize', json={'time_limit': 2})
    assert solved.status_code == 200, solved.text
    plan = client.post('/api/companies/1/plans', json={'run_id': solved.json()['run_id'], 'variant': 'optimized'})
    assert plan.status_code == 200, plan.text
    plan_id = plan.json()['id']

    approve = {'decision': 'approved', 'comment': 'تعليق خاص'}
    assert client.post(f'{base}/decisions/{approved}/decide', json=approve).status_code == 200
    assert client.post(f'{base}/decisions/{approved}/decide', json=approve).status_code == 409
    assert client.post(f'/api/plans/{plan_id}/decide', json=approve).status_code == 200
    assert client.post(f'{base}/decisions/{rejected}/decide', json={'decision': 'rejected'}).status_code == 200
    assert client.post(f'{other_base}/decisions/{unrelated}/decide', json=approve).status_code == 200

    settings = Settings(db_path=db_path)
    assert client.put(f'{base}/thresholds', json={'cash_minimum': 600}).status_code == 200
    alerts.refresh_alerts(settings)
    alerts.refresh_alerts(settings)
    # A new revision with the same breach still contributes one incident.
    assert client.put(f'/api/enterprise/models/{model_id}', json=graph() | {'expected_version': 1}).status_code == 200
    alerts.refresh_alerts(settings)
    # Recovery followed by a new breach emits again.
    assert client.put(f'{base}/thresholds', json={'cash_minimum': 400}).status_code == 200
    alerts.refresh_alerts(settings)
    assert client.put(f'{base}/thresholds', json={'cash_minimum': 600}).status_code == 200
    alerts.refresh_alerts(settings)

    webhooks.flush(db_path, destinations, clock=lambda: 1000)
    assert len(requests) == 4
    webhooks.initialize(db_path, destinations)  # A restart retains the pending retry.
    webhooks.flush(db_path, destinations, clock=lambda: 1004)
    assert len(requests) == 4
    webhooks.flush(db_path, destinations, clock=lambda: 1005)
    assert len(requests) == 5
    for header, raw in requests:
        event_id, timestamp = header['X-Athar-Event-ID'], header['X-Athar-Timestamp']
        signature = digest(SECRET.encode(), f'{timestamp}.{event_id}.'.encode() + raw, 'sha256').hex()
        assert compare_digest(header['X-Athar-Signature'], 'sha256=' + signature)
    first, *_, retry = requests
    assert first[0]['X-Athar-Event-ID'] == retry[0]['X-Athar-Event-ID'] and first[1] == retry[1]
    events = [json.loads(item[1]) for item in requests[:4]]
    assert [event['event'] for event in events] == ['decision.approved'] * 2 + ['alert.triggered'] * 2
    assert all(event['company_id'] == 1 for event in events)
    assert events[0]['data'] == {'type': 'enterprise_decision', 'id': approved, 'model_id': model_id, 'version': 1}
    assert events[1]['data']['id'] == plan_id and events[1]['data']['type'] == 'plan'
    assert events[2]['data'] == {'kpi': 'cash_low', 'value': 500, 'threshold': 600, 'direction': 'below',
                                 'model_id': model_id, 'version': 1}
    assert events[3]['data']['version'] == 2
    with get_conn(db_path) as connection:
        rows = connection.execute('SELECT attempts, delivered_at FROM integration_webhook_outbox ORDER BY id').fetchall()
        assert [row['attempts'] for row in rows] == [2, 1, 1, 1]
        assert all(row['delivered_at'] for row in rows)


def test_opt_in_worker_checks_saved_thresholds_and_delivers(client, db_path, receiver, monkeypatch):
    url, requests, received = receiver
    model_id = create_model(client)['id']
    base = f'/api/enterprise/models/{model_id}/prescriptive'
    assert client.put(f'{base}/thresholds', json={'cash_minimum': 600}).status_code == 200
    assert not requests
    monkeypatch.setenv('ATHAR_WEBHOOKS', json.dumps([{
        'name': 'monitor', 'company_id': 1, 'url': url, 'secret': SECRET, 'events': ['alert.triggered'],
    }]))
    from app.main import create_app

    application = create_app(Settings(db_path=db_path, llm_provider='rules', forecast_engine='stat'))
    with TestClient(application):
        assert received.wait(8), 'Startup webhook worker did not deliver to the local receiver'
    event = json.loads(requests[0][1])
    assert event['event'] == 'alert.triggered' and event['data']['model_id'] == model_id


@contextmanager
def erp_server():
    content = {}

    class Page(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path != '/':
                self.send_response(204)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.end_headers()
            self.wfile.write(content['page'].encode())

        def log_message(self, *_args):
            pass

    with ThreadingHTTPServer(('127.0.0.1', 0), Page) as host:
        thread = Thread(target=host.serve_forever, daemon=True)
        thread.start()
        try:
            yield f'http://127.0.0.1:{host.server_port}', content
        finally:
            host.shutdown()
            thread.join()


@pytest.mark.parametrize('width', [1440, 390])
def test_cross_origin_component_and_iframe(width, monkeypatch):
    with erp_server() as (erp, allowed), erp_server() as (denied, forbidden):
        monkeypatch.setenv('ATHAR_FRAME_ANCESTORS', erp)
        check_embed_in_browser(width, erp, allowed, denied, forbidden)


def check_embed_in_browser(width, erp, allowed, denied, forbidden):
    with server() as origin, httpx.Client(base_url=origin) as admin:
        response = admin.post('/api/auth/login', json={'username': 'qa-owner', 'password': PASSWORD},
                              headers={'X-Athar-Login': '1'})
        assert response.status_code == 200, response.text
        admin.headers['X-CSRF-Token'] = response.json()['csrf_token']
        create_model(admin, name='شركة أفق للتجربة')
        key = create_key(admin, scopes=['map:read'])
        token = admin.post(f'{API}/1/embed-token', json={}, headers=headers(key))
        assert token.status_code == 201
        link = origin + token.json()['path']
        allowed['page'] = forbidden['page'] = f'''<!doctype html>
            <meta name="viewport" content="width=device-width,initial-scale=1">
            <body style="margin:0"><script defer src="{origin}/static/js/athar-map.js"></script>
            <athar-map src="{link}" style="--athar-map-height:850px"></athar-map></body>'''
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page(viewport={'width': width, 'height': 900}, reduced_motion='reduce')
            errors, external, blocked = [], [], []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.on('console', lambda message: errors.append(message.text) if message.type == 'error' else None)
            page.on('response', lambda response: blocked.append(response.status) if response.status >= 400 else None)

            def route_request(route):
                url = urlsplit(route.request.url)
                request_origin = f'{url.scheme}://{url.netloc}'
                if request_origin in (origin, erp, denied):
                    route.continue_()
                else:
                    external.append(route.request.url)
                    route.abort()

            page.route('**/*', route_request)
            page.goto(erp)
            frame = page.frame_locator('athar-map iframe')
            try:
                expect(frame.get_by_role('heading', name='خريطة الشركة')).to_be_visible()
            except AssertionError:
                pytest.fail(f'{errors=} {blocked=} {external=} frames={len(page.frames)}')
            expect(frame.locator('#embed-company')).to_contain_text('شركة أفق للتجربة')
            expect(frame.locator('.lm-department')).to_have_count(5)
            expect(frame.locator('#embed-stage')).to_have_attribute('data-road-count', '3')
            assert frame.locator('html').get_attribute('dir') == 'rtl'
            assert frame.locator('html').evaluate('el => el.scrollWidth === innerWidth')
            assert frame.locator('h1').evaluate('async el => { await document.fonts.ready; '
                                                'return document.fonts.check(getComputedStyle(el).font); }')
            assert frame.locator('canvas').evaluate('el => el.getContext("2d").getImageData(0, 0, el.width, '
                                                     'el.height).data.some(value => value !== 0)')
            screens = os.getenv('ATHAR_P25_SCREENS')
            if screens:
                frame.locator('main').screenshot(path=Path(screens) / f'p25_embed_sand_{width}.png')
            theme = frame.get_by_role('button', name='الوضع الليلي')
            theme.click()
            expect(frame.locator('html')).to_have_attribute('data-theme', 'dark')
            if screens:
                frame.locator('main').screenshot(path=Path(screens) / f'p25_embed_night_{width}.png')
            assert not errors and not blocked and not external

            with page.expect_console_message(predicate=lambda message: 'frame-ancestors' in message.text):
                page.goto(denied)
            expect(page.locator('athar-map iframe')).to_be_visible()
            expect(frame.get_by_role('heading', name='خريطة الشركة')).to_have_count(0)
            browser.close()
