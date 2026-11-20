"""Authenticated map briefing, grounding, and browser acceptance."""
from dataclasses import replace
from datetime import date, timedelta
import json
import os
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

from fastapi.testclient import TestClient
from playwright.sync_api import expect, sync_playwright
import pytest

from app.db import get_conn
from app.enterprise.whatif import ImpactModel
from app.llm import codex_cli
from conftest import sign_in
from qa.phase16_browser import capture, login, server
from test_p15_access import USER_PASSWORD, new_account
from test_p21_living_map import graph

BRIEFING = '/api/enterprise/map/briefing'
MODELS = '/api/enterprise/models'


def series(values):
    start = date(2026, 1, 5)
    return {'kind': 'series', 'frequency': 'weekly', 'points': [
        {'date': (start + timedelta(weeks=i)).isoformat(), 'value': value}
        for i, value in enumerate(values)
    ]}


def signal_model():
    body = graph('شركة الموجز التجريبية')
    nodes = {row['id']: row for row in body['graph']['nodes']}
    nodes['cash']['baseline'] = series([500] * 9 + [350])
    nodes['pnl.result']['baseline'] = series([240] * 8 + [235, 220])
    body['graph']['edges'][1]['rule']['lag_weeks'] = 0
    return body


def save_model(client, body=None):
    response = client.post(MODELS + '?company_id=1', json=body or signal_model())
    assert response.status_code == 201, response.text
    model_id = response.json()['id']
    configured = client.put(f'{MODELS}/{model_id}/prescriptive/thresholds', json={'cash_minimum': 450})
    assert configured.status_code == 200, configured.text
    return model_id


def test_alerts_trace_history_and_current_refresh(client):
    body = signal_model()
    model_id = save_model(client, body)
    params = {'model_id': model_id, 'version': 1}
    response = client.get(BRIEFING, params=params)
    assert response.status_code == 200, response.text
    assert response.headers['cache-control'] == 'no-store'
    summary = response.json()
    alerts = {row['id']: row for row in summary['alerts']}
    assert alerts['cash_low']['evidence']['value'] == 350
    assert alerts['cash_low']['evidence']['threshold'] == 450
    assert alerts['anomaly:cash']['evidence']['reference'] == 500
    assert alerts['cash_low']['department'] == alerts['anomaly:cash']['department'] == 'finance'
    assert '350' in summary['reply_ar'] and 'راجع' in summary['reply_ar']
    assert summary['history']['cash_low'][-2:] == [500, 350]
    trace = summary['traces']['cash']
    assert ('results', 'finance') in {(e['from_department'], e['to_department']) for e in trace['path']}
    assert trace['coincident_changes'][0]['id'] == 'pnl.result'
    assert trace['coincident_changes'][0]['after'] == 220

    next(node for node in body['graph']['nodes'] if node['id'] == 'cash')['baseline'] = series([500] * 9 + [350, 505])
    assert client.put(f'{MODELS}/{model_id}', json={**body, 'expected_version': 1}).status_code == 200
    fresh = client.get(BRIEFING, params={'model_id': model_id}).json()
    assert fresh['version'] == 2 and fresh['kpis']['cash_low'] == 505
    assert not {row['id'] for row in fresh['alerts']} & {'cash_low', 'anomaly:cash'}
    old = client.get(BRIEFING, params=params).json()
    assert old['historical'] and 'cash_low' not in {row['id'] for row in old['alerts']}


def test_grounding_codex_selection_and_rules_fallback(client, monkeypatch):
    model_id = save_model(client)
    settings = client.app.state.settings
    monkeypatch.setattr(client.app.state, 'settings', replace(settings, llm_provider='codex'))
    with patch.object(codex_cli, 'complete', return_value='{"ids":[1]}') as complete:
        selected = client.get(BRIEFING, params={'model_id': model_id}).json()
        assert selected['provider'] == 'codex' and 'الوسيط السابق 500' in selected['reply_ar']
        assert '350' in complete.call_args.args[1]
    for invalid in ('{"ids":[999999]}', '{"ids":[0],"text":"999999"}', '{"ids":[]}'):
        with patch.object(codex_cli, 'complete', return_value=invalid):
            fallback = client.get(BRIEFING, params={'model_id': model_id}).json()
            assert fallback['provider'] == 'rules' and '999999' not in fallback['reply_ar']
    with patch.object(codex_cli, 'complete', side_effect=TimeoutError):
        assert client.get(BRIEFING, params={'model_id': model_id}).json()['provider'] == 'rules'


def test_estimates_excluded_from_alerts_and_sparklines(client):
    body = signal_model()
    model_id = save_model(client, body)
    refresh = client.post('/api/companies/1/ai/refresh', json={'model_id': model_id, 'remote': False})
    assert refresh.status_code == 201, refresh.text
    cash = next(node for node in body['graph']['nodes'] if node['id'] == 'cash')
    with get_conn(client.app.state.settings.db_path) as connection:
        connection.executemany('INSERT INTO ai_estimated_points VALUES (?, ?, ?, ?, ?)', [
            (model_id, 'cash', point['date'], point['value'], refresh.json()['id'])
            for point in cash['baseline']['points']
        ])
    result = client.get(BRIEFING, params={'model_id': model_id}).json()
    assert 'anomaly:cash' not in {row['id'] for row in result['alerts']}
    assert result['history']['cash_low'] == []


def test_demo_briefing_keeps_operational_baseline_on_demand(client):
    with patch.object(ImpactModel, 'baseline', side_effect=AssertionError('unexpected solve')):
        response = client.get(BRIEFING)
    assert response.status_code == 200, response.text
    assert response.json()['model_id'] is None and not response.json()['alerts']


def test_partial_readiness_and_permissions(client):
    model_id = save_model(client)
    assert client.get(BRIEFING, params={'version': 1}).status_code == 422
    partial = client.post('/api/onboarding/companies', json={'name': 'شركة تجهيز جزئي', 'sector': 'trading'}).json()
    partial_id = partial['model_id']
    result = client.get(BRIEFING, params={'model_id': partial_id}).json()
    assert result['readiness'] == 0 and not result['alerts'] and 'كمّل بيانات' in result['reply_ar']
    assert all(value is None for value in result['kpis'].values())
    new_account(client, 'viewer', (2, partial['company_id']))
    with TestClient(client.app) as viewer:
        assert viewer.get(BRIEFING, params={'model_id': model_id}).status_code == 401
        sign_in(viewer, 'viewer', USER_PASSWORD)
        assert viewer.get(BRIEFING, params={'model_id': model_id}).status_code == 404
        assert viewer.get(BRIEFING, params={'model_id': partial_id}).status_code == 200


@pytest.mark.parametrize('width', [1440, 390])
def test_briefing_browser(width):
    errors = []
    with (server() as origin, sync_playwright() as playwright,
          patch.object(codex_cli, 'complete', return_value=json.dumps({'ids': [0, 1]}))):
        browser = playwright.chromium.launch()
        page = browser.new_page(base_url=origin, viewport={'width': width, 'height': 900},
                                reduced_motion='reduce')
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.on('console', lambda event: errors.append(event.text) if event.type == 'error' else None)
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
        headers = {'X-CSRF-Token': page.request.get('/api/auth/me').json()['csrf_token']}
        body = signal_model()
        created = page.request.post(MODELS + '?company_id=1', data=body, headers=headers)
        assert created.ok, created.text()
        model_id = created.json()['id']
        configured = page.request.put(f'{MODELS}/{model_id}/prescriptive/thresholds', headers=headers,
                                      data={'cash_minimum': 450})
        assert configured.ok, configured.text()
        if width == 1440:
            seeded = page.request.post('/api/companies/1/process-mining/demo', headers=headers)
            assert seeded.ok, seeded.text()
        page.reload()
        briefing = page.get_by_test_id('map-briefing')
        expect(briefing).to_contain_text('Codex', timeout=15000)
        expect(briefing).to_contain_text('350')
        stage = page.get_by_test_id('map-stage')
        flow_source = page.get_by_test_id('process-flow-source')
        if width == 1440:
            expect(flow_source).to_have_text('سجل تجريبي')
        finance = stage.locator('[data-department="finance"]')
        expect(finance.get_by_test_id('map-alert-pin')).to_have_text('تنبيهات2')
        finance.click()
        trace = page.get_by_test_id('map-alert-trace')
        expect(trace).to_contain_text('تدفق نقدي')
        expect(trace).to_contain_text('تغيّرات مرصودة')
        expect(stage.locator('[data-department="results"]')).to_have_attribute('data-traced', 'true')
        expect(flow_source).to_have_text('افتراضي')
        trace.get_by_label('تنبيه القسم').select_option('anomaly:cash')
        expect(trace).to_contain_text('الوسيط السابق 500')
        ribbon = page.get_by_test_id('map-kpis')
        expect(ribbon.locator('article').nth(1).locator('svg')).to_be_visible()
        if width == 390:
            page.get_by_test_id('theme-toggle').click()
            expect(page.locator('html')).to_have_attribute('data-theme', 'dark')
        capture(page, 'briefing', '#lm-briefing-title', None)
        if destination := os.environ.get('ATHAR_P23_SCREENS'):
            directory = Path(destination)
            directory.mkdir(parents=True, exist_ok=True)
            page.locator('.lm-shell').screenshot(path=directory / f'p23_briefing_{width}.png', animations='disabled')

        trace.get_by_role('button', name='اقفل التتبّع').click()
        expect(trace).to_have_count(0)
        expect(stage.locator('[data-department="results"]')).to_have_attribute('data-traced', 'false')
        if width == 1440:
            expect(flow_source).to_have_text('سجل تجريبي')
        page.get_by_label('مدخل للتعديل').select_option('demand')
        page.get_by_role('button', name='ضيف', exact=True).click()
        page.get_by_test_id('map-assumption-value').fill('80')
        page.get_by_test_id('map-run').click()
        expect(ribbon.locator('article').nth(1)).to_contain_text('سيناريو محدد')
        expect(ribbon.locator('article').nth(1)).to_contain_text('310')

        next(node for node in body['graph']['nodes'] if node['id'] == 'cash')['baseline'] = series([505] * 10)
        revised = page.request.put(f'{MODELS}/{model_id}', headers=headers, data={**body, 'expected_version': 1})
        assert revised.ok, revised.text()
        briefing.get_by_role('button', name='حدّث الموجز').click()
        expect(page.locator('.lm-brief-status')).to_contain_text('إصدار النموذج 2')
        expect(finance.get_by_test_id('map-alert-pin')).to_be_hidden()
        expect(ribbon.locator('article').nth(1)).to_contain_text('505')
        assert not errors, errors
        browser.close()
