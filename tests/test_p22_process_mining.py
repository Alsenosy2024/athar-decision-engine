"""Offline acceptance for event discovery, company isolation and weekly map playback."""
from pathlib import Path
import os
from urllib.parse import urlsplit

from fastapi.testclient import TestClient
from playwright.sync_api import expect, sync_playwright
import pytest

from app.config import Settings
from app.db import get_conn
from app.main import create_app
from app.process_mining import simulation
from app.process_mining.demo import ROUTES, generate_csv
from app.process_mining.discovery import DAY, WEEK, discover
from app.process_mining.events import MAX_BYTES, parse
from app.process_mining.simulation import Scenario, simulate
from conftest import sign_in
from qa.phase16_browser import capture, login, post_from, server
from test_p15_access import USER_PASSWORD, new_account
from test_p21_living_map import graph

MAP = '/api/enterprise/map'
SIMULATE = '/api/enterprise/simulate'
LOG = '/api/companies/1/process-mining'
CSV = '''case_id,activity,timestamp,resource,department
case-1,Ship,2026-01-03T08:00:00Z,secret-resource,Warehouse
case-1,Order,2026-01-01T11:00:00+03:00,Clerk,sales
case-1,Pack,2026-01-02T08:00:00Z,Pack-1,Warehouse
case-1,Recheck,2026-01-04T08:00:00Z,Clerk,المبيعات والطلب
case-2,Order,2026-01-01T09:00:00Z,Clerk,sales
case-2,Pack,2026-01-02T09:00:00Z,Pack-2,Warehouse
'''.encode()
XES = '''<?xml version="1.0" encoding="UTF-8"?>
<log xmlns="http://www.xes-standard.org/">
  <trace>
    <string key="concept:name" value="sample-case"/>
    <event><string key="concept:name" value="Order"/><date key="time:timestamp" value="2026-01-01T08:00:00Z"/>
      <string key="org:resource" value="Clerk"/><string key="org:group" value="sales"/></event>
    <event><string key="concept:name" value="Pack"/><date key="time:timestamp" value="2026-01-02T08:00:00Z"/>
      <string key="org:resource" value="Worker"/><string key="org:group" value="Warehouse"/></event>
  </trace>
</log>'''.encode()


def upload(client, payload=CSV, name='ops.csv', base=LOG):
    return client.post(base + '/import', files={'file': (name, payload)})


def tree_leaves(tree):
    if 'activity' in tree:
        return {tree['activity']} - {None}
    return set().union(*(tree_leaves(child) for child in tree['children']))


def test_observed_roads_median_rework_and_inductive_trees(client):
    uploaded = upload(client)
    assert uploaded.status_code == 200, uploaded.text
    report = uploaded.json()
    assert report['source'] == 'actual' and report['format'] == 'csv'
    assert (report['case_count'], report['event_count']) == (2, 6)
    assert report['starts'] == {'sales': 2}
    assert report['ends'] == {'Warehouse': 1, 'sales': 1}
    assert tree_leaves(report['process_tree']) == {'Order', 'Pack', 'Ship', 'Recheck'}
    assert tree_leaves(report['department_tree']) == {'sales', 'Warehouse'}
    assert 'secret-resource' not in uploaded.text and 'case-1' not in uploaded.text
    transitions = {(row['from_department'], row['to_department']): row for row in report['transitions']}
    for route, (volume, reworks) in {('sales', 'Warehouse'): (2, 0),
                                     ('Warehouse', 'sales'): (1, 1), ('Warehouse', 'Warehouse'): (1, 0)}.items():
        row = transitions[route]
        assert (row['volume'], row['median_duration_days'], row['rework_count']) == (volume, 1, reworks)
    assert report['bottlenecks'][0]['id'] == 'sales'
    assert {row['id']: row['capacity'] for row in report['departments']} == {'sales': 1, 'Warehouse': 3}
    assert client.get(LOG).json() == report
    assert client.get(MAP).json()['flow_source'] == 'default'
    assert client.get(MAP, params={'company_id': 2}).json()['flow_roads'] is None
    current = client.get(MAP, params={'company_id': 1}).json()
    assert current['model'] is None and current['flow_source'] == 'actual'
    assert {row['id'] for row in current['departments']} == {'sales', 'Warehouse'}
    assert len(current['flow_roads']) == 2
    model = client.post('/api/enterprise/models?company_id=1', json=graph()).json()
    attached = client.get(MAP, params={'model_id': model['id']}).json()
    assert attached['flow_roads'] == current['flow_roads']
    assert len(attached['edges']) == len(graph()['graph']['edges'])


def test_repeating_activity_within_department_counts_as_rework():
    events, _ = parse(CSV.replace(b'case-1,Ship,', b'case-1,Pack,'), 'repeat.csv')
    profile = discover(events)
    internal = next(row for row in profile['transitions']
                    if row['from_department'] == row['to_department'])
    assert internal['rework_count'] == 1


def test_xes_replacement_persists_after_restart(client, db_path):
    old = upload(client).json()
    response = upload(client, XES, 'workflow.XES')
    assert response.status_code == 200, response.text
    report = response.json()
    assert report['format'] == 'xes' and report['revision'] != old['revision']
    assert report['transitions'][0]['volume'] == 1
    changed = client.post(SIMULATE, json={'company_id': 1, 'revision': old['revision']})
    assert changed.status_code == 409 and changed.json()['detail']['code'] == 'event_log_changed'
    with TestClient(create_app(Settings(db_path=db_path, forecast_engine='stat', llm_provider='rules'))) as restarted:
        user = sign_in(restarted)
        assert restarted.get(LOG).json() == report
    with get_conn(db_path) as connection:
        events = connection.execute("SELECT * FROM audit_log WHERE action='process_mining.import'").fetchall()
        assert len(events) == 2 and all(row['company_id'] == 1 and row['user_id'] == user['id'] for row in events)
        persisted = connection.execute('SELECT profile_json FROM company_process_mining').fetchone()[0]
        assert 'sample-case' not in persisted and 'Worker' not in persisted


@pytest.mark.parametrize(('payload', 'name', 'status'), [
    (b'', 'blank.csv', 422),
    (b'x,y,z\na,b,c', 'missing.csv', 422),
    (CSV.replace(b'2026-01-03T08:00:00Z', b'last Thursday'), 'bad.csv', 422),
    (CSV.replace(b'Pack-1,Warehouse', b'Pack-1,Warehouse,extra'), 'bad.csv', 422),
    (CSV, 'misnamed.json', 415),
    (b'x' * (MAX_BYTES + 1), 'large.csv', 413),
    (b'<!DOCTYPE log [<!ENTITY ext SYSTEM "file:///tmp/x">]><log>&ext;</log>', 'unsafe.xes', 422),
    (b'<hello/>', 'bad.xes', 422),
])
def test_invalid_upload_preserves_current_report(client, payload, name, status):
    existing = upload(client).json()
    invalid = upload(client, payload, name)
    assert invalid.status_code == status, invalid.text
    assert invalid.json()['detail']['msg_ar']
    assert client.get(LOG).json() == existing


def test_single_event_uses_labeled_duration_fallback():
    events, _ = parse(CSV.splitlines()[0] + b'\n' + CSV.splitlines()[1], 'single.csv')
    profile = discover(events)
    assert profile['transitions'] == []
    assert profile['departments'][0]['service_seconds'] == DAY
    assert profile['departments'][0]['duration_source'] == 'fallback'
    assert simulate(profile, 2, Scenario(demand_pct=-100), 42)[0]['lead_time_days'] is None


@pytest.mark.parametrize('template', ROUTES)
def test_demo_fixtures_are_seeded_and_template_specific(template):
    log = generate_csv(template)
    assert log == generate_csv(template, seed=42)
    assert log != generate_csv(template, seed=43)
    events, _ = parse(log, 'demo.csv')
    by_case = [event.department for event in events if event.case_id == 'demo-2']
    assert tuple(by_case) == ROUTES[template]


def test_simulation_scenarios_and_synthetic_provenance(client):
    assert client.post(SIMULATE, json={'company_id': 1}).status_code == 409
    source = client.post(LOG + '/demo').json()
    assert source['source'] == 'synthetic'
    assert client.get(MAP, params={'company_id': 1}).json()['flow_source'] == 'synthetic'
    body = {'company_id': 1, 'weeks': 5, 'seed': 42}
    baseline = client.post(SIMULATE, json=body)
    assert baseline.status_code == 200, baseline.text
    assert baseline.json() == client.post(SIMULATE, json=body).json()
    increased_capacity = client.post(SIMULATE, json=body | {'scenario': {'capacities': {'projects': 8}}}).json()
    slower = client.post(SIMULATE, json=body | {'scenario': {'service_pct': {'projects': 100}}}).json()
    higher_demand = client.post(SIMULATE, json=body | {'scenario': {'demand_pct': 100}}).json()
    baseline_weeks = baseline.json()['weeks']
    assert increased_capacity['weeks'][-1]['backlog'] < baseline_weeks[-1]['backlog']
    assert slower['weeks'][-1]['backlog'] > baseline_weeks[-1]['backlog']
    assert sum(row['arrivals'] for row in higher_demand['weeks']) > sum(row['arrivals'] for row in baseline_weeks)
    assert [row['week'] for row in baseline_weeks] == [1, 2, 3, 4, 5]
    arrived = completed = 0
    for week in baseline_weeks:
        arrived += week['arrivals']
        completed += week['completed']
        assert arrived - completed == week['backlog']
        assert all(0 <= dept['utilization_pct'] <= 100 for dept in week['departments'])
    assert client.post(SIMULATE, json=body | {'scenario': {'capacities': {'unknown': 1}}}).status_code == 422
    assert client.post(SIMULATE, json=body | {'weeks': 105}).status_code == 422
    assert upload(client).status_code == 200
    assert client.post(LOG + '/demo').status_code == 409


def test_weekly_boundary_backlog_lead_time_and_utilization(monkeypatch):
    profile = {'departments': [
        {'id': 'A', 'capacity': 1, 'service_seconds': 4 * DAY},
        {'id': 'B', 'capacity': 1, 'service_seconds': DAY},
    ]}
    monkeypatch.setattr(simulation, 'arrivals', lambda *args: [(WEEK - DAY, ('A', 'B')), (WEEK - DAY, ('A', 'B'))])
    first, second, third = simulate(profile, 3, Scenario(), 42)
    assert (first['arrivals'], first['completed'], first['backlog'], first['waiting']) == (2, 0, 2, 1)
    assert first['lead_time_days'] is None and first['utilization_pct'] == pytest.approx(100 / 14, abs=.0001)
    assert (second['completed'], second['backlog'], second['waiting'], second['lead_time_days']) == (1, 1, 0, 5)
    assert second['flows'] == [{'from_department': 'A', 'to_department': 'B', 'volume': 1}]
    assert second['departments'][0]['utilization_pct'] == 100
    assert (third['completed'], third['backlog'], third['lead_time_days']) == (1, 0, 9)


def test_company_roles_and_csrf(client):
    assert upload(client).status_code == 200
    new_account(client, 'viewer', (1,))
    new_account(client, 'planner', (2,))
    with TestClient(client.app) as viewer, TestClient(client.app) as planner:
        assert viewer.get(LOG).status_code == 401
        sign_in(viewer, 'viewer', USER_PASSWORD)
        assert viewer.get(LOG).status_code == 200
        assert upload(viewer).status_code == 403
        assert viewer.post(SIMULATE, json={'company_id': 1}).status_code == 403
        sign_in(planner, 'planner', USER_PASSWORD)
        assert planner.get(LOG).status_code == 404
        assert planner.get(MAP, params={'company_id': 1}).status_code == 404
        assert upload(planner).status_code == 404
        assert planner.post(SIMULATE, json={'company_id': 1}).status_code == 404
        assert planner.post(SIMULATE, json={'company_id': 2}, headers={'X-CSRF-Token': 'bad'}).status_code == 403
        assert planner.post('/api/companies/2/process-mining/demo').status_code == 200


@pytest.mark.parametrize('width', [1440, 390])
def test_process_flow_browser(width):
    errors = []
    with server() as origin, sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(base_url=origin, viewport={'width': width, 'height': 900})
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.on('console', lambda message: errors.append(message.text) if message.type == 'error' else None)
        page.on('response', lambda response: errors.append(f'{response.status} {response.url}')
                if response.status >= 400 else None)

        def local_only(route):
            if urlsplit(route.request.url).netloc == urlsplit(origin).netloc:
                route.continue_()
            else:
                errors.append(route.request.url)
                route.abort()

        page.route('**/*', local_only)
        page.goto('/#/map')
        login(page, 'qa-owner')
        source = page.get_by_test_id('process-flow-source')
        expect(source).to_have_text('افتراضي')
        stage = page.get_by_test_id('map-stage')
        if width == 1440:
            csrf = page.request.get('/api/auth/me').json()['csrf_token']
            saved = page.request.post('/api/enterprise/models?company_id=1', data=graph(),
                                      headers={'X-CSRF-Token': csrf})
            assert saved.ok, saved.text()
            page.reload()
            expect(stage).to_have_attribute('data-road-count', '3')
        file = page.get_by_test_id('process-file')
        payload = generate_csv('contracting')
        with page.expect_response(lambda response: response.url.endswith(LOG + '/import'), timeout=90_000) as event:
            file.set_input_files({'name': 'workflow.csv', 'mimeType': 'text/csv', 'buffer': payload})
        assert event.value.ok, event.value.text()
        expect(source).to_have_text('من بيانات فعلية')
        expect(stage).to_have_attribute('data-road-count', '5')
        expect(page.get_by_test_id('process-summary')).to_contain_text('48 حالة')
        page.get_by_text('الانتقالات وإعادة العمل', exact=True).click()
        expect(page.get_by_text('48 انتقال', exact=False).first).to_be_visible()
        page.get_by_text('الأقسام حسب الحمل المقدّر', exact=True).click()
        expect(page.get_by_text('الحمل تقديري', exact=False)).to_be_visible()
        page.get_by_label('عدد أسابيع المحاكاة').fill('4')
        page.get_by_label('تغيّر الطلب (%)').fill('20')
        result = post_from(page, '/enterprise/simulate', page.get_by_test_id('process-simulate'))
        expect(stage).to_have_attribute('data-ripple', 'running')
        slider = page.get_by_role('slider', name='أسبوع المحاكاة')
        week = page.get_by_test_id('process-week')
        expect(week).to_be_visible()
        expect(stage).to_have_attribute('data-ripple', 'complete', timeout=8000)
        expect(slider).to_have_value('4')
        slider.fill('2')
        expect(week).to_contain_text('نتيجة الأسبوع 2')
        expect(week.locator('dd').first).to_have_text(str(result['weeks'][1]['backlog']))
        assert int(stage.get_attribute('data-active-flows')) > 0
        page.get_by_text('انتقالات الأسبوع بين الأقسام', exact=True).click()
        expect(week.locator('details li').first).to_be_visible()
        capture(page, f'p22_{width}', '#lm-process-title', None)
        if destination := os.environ.get('ATHAR_P22_SCREENS'):
            directory = Path(destination)
            directory.mkdir(parents=True, exist_ok=True)
            if width == 390:
                page.get_by_test_id('theme-toggle').click()
                expect(page.locator('html')).to_have_attribute('data-theme', 'dark')
            page.locator('.lm-map-panel').screenshot(path=directory / f'p22_process_{width}.png', animations='disabled')
        if width == 1440:
            page.get_by_role('combobox', name='مدخل للتعديل').select_option('demand')
            page.get_by_role('button', name='ضيف', exact=True).click()
            page.get_by_test_id('map-assumption-value').fill('80')
            outcome = post_from(page, '/enterprise/whatif', page.get_by_test_id('map-run'))
            assert outcome['paths']
            expect(source).to_have_text('افتراضي')
            expect(stage).to_have_attribute('data-road-count', '3')
            page.get_by_role('button', name='ارجع لتدفقات السجل').click()
            expect(source).to_have_text('من بيانات فعلية')
            expect(stage).to_have_attribute('data-road-count', '5')
            expect(slider).to_have_value('1')
        expect(page.get_by_test_id('company-switcher')).to_have_value('1')
        page.get_by_test_id('company-switcher').select_option('2')
        expect(source).to_have_text('افتراضي')
        expect(week).to_have_count(0)
        assert not errors, errors
        browser.close()
