"""Phase 21 contracts and Chromium smoke against real enterprise impact."""
from pathlib import Path
import json
import os
import subprocess
from unittest.mock import patch
from urllib.parse import urlsplit

from fastapi.testclient import TestClient
from playwright.sync_api import expect, sync_playwright
import pytest

from app.db import encode, get_conn
from app.enterprise.living_map import GROUP
from app.enterprise.schema import NodeType
from app.enterprise.whatif import ImpactModel
from app.llm import codex_cli
from qa.phase16_browser import capture, login, post_from, server, click_workspace_link
from conftest import sign_in
from test_p15_access import USER_PASSWORD, new_account

API = '/api/enterprise/map'
QUESTION = 'خفض الطلب 20%'
ASSUMPTIONS = [{'node_id': 'demand', 'operation': 'pct', 'value': -20, 'uncertainty': 0}]


def graph(name='شركة أفق للاختبار'):
    def node(node_id, kind, label, value, unit='units'):
        return {'id': node_id, 'type': kind, 'name_ar': label, 'unit': unit,
                'baseline': {'kind': 'scalar', 'value': value}, 'definition': {'kind': 'formula', 'expression': 'مرجع'}}
    return {'name': name, 'graph': {
        'nodes': [
            node('demand', 'demand', 'الطلب', 100),
            node('capacity', 'production_line', 'الطاقة التشغيلية', 120),
            node('pnl.result', 'profit_loss', 'النتيجة بعد التمويل', 240, 'SAR'),
            node('cash', 'cash', 'رصيد السيولة', 500, 'SAR'),
            node('credit', 'credit', 'حد الائتمان', 1000, 'SAR'),
            node('risk.credit', 'risk_kpi', 'احتمال تجاوز الائتمان', None, '%'),
        ],
        'edges': [
            {'source': 'demand', 'target': 'capacity', 'name_ar': 'تخطيط التشغيل',
             'rule': {'lag_weeks': 2, 'multiplier': 1}},
            {'source': 'capacity', 'target': 'pnl.result', 'name_ar': 'قيمة التشغيل',
             'rule': {'lag_weeks': 1, 'multiplier': 2}},
            {'source': 'pnl.result', 'target': 'cash', 'name_ar': 'تدفق نقدي',
             'rule': {'multiplier': 1}},
            {'source': 'credit', 'target': 'cash', 'name_ar': 'السيولة المتاحة',
             'rule': {'multiplier': 0.1}},
        ],
    }}


def test_map_contract_demo_and_unlinked_company(client):
    values = client.get(API).json()
    assert values['is_demo'] and values['model']['id'] is None and values['has_engines']
    assert set(GROUP) == set(NodeType)
    nodes = {node['id']: node for node in values['nodes']}
    departments = {row['id']: row for row in values['departments']}
    assert {'projects', 'operations', 'inventory', 'procurement', 'finance'} <= departments.keys()
    assert set(nodes) == {key for row in departments.values() for key in row['node_ids']}
    template = client.get('/api/enterprise/templates/enterprise').json()['graph']
    assert {(edge['source'], edge['target']) for edge in values['edges']} == {
        (edge['source'], edge['target']) for edge in template['edges']
    }
    for row in values['edges']:
        assert row['from_department'] == nodes[row['source']]['department']
        assert row['to_department'] == nodes[row['target']]['department']
        assert 0 <= row['weight'] <= 1
    demand = next(row for row in values['edges'] if row['source'].startswith('demand.'))
    assert demand['baseline_volume'] == nodes[demand['source']]['baseline']
    assert demand['weight'] > 0
    cash = next(row for row in values['kpis'] if row['id'] == 'cash_low')
    assert cash['baseline'] is None  # Opening balance is not the period minimum.
    for company in (1, 2):
        empty = client.get(API, params={'company_id': company}).json()
        assert empty['company']['id'] == company and empty['model'] is None and not empty['is_demo']
        stored = client.post(f'/api/enterprise/models/demo?company_id={company}').json()
        attached = client.get(API, params={'company_id': company}).json()
        assert attached['model']['id'] == stored['id'] and not attached['is_demo']
    simulated = client.post('/api/enterprise/whatif', json={
        'model_id': stored['id'], 'version': 1,
        'assumptions': [{'node_id': 'credit', 'operation': 'pct', 'value': 0, 'uncertainty': 1000}], 'n': 4,
    })
    assert simulated.status_code == 200, simulated.text
    result = simulated.json()
    bands = result['fill_rate_quantiles']
    assert 0 <= result['fill_rate_baseline'] <= 100
    assert 0 <= bands['p10'] <= bands['p50'] <= bands['p90'] <= 100


@pytest.mark.parametrize('sector', ['trading', 'retail', 'services', 'custom'])
def test_map_onboarding_templates_and_partial_data(client, sector):
    company = client.post('/api/onboarding/companies', json={'name': 'شركة تجهيز ' + sector, 'sector': sector}).json()
    cid = company['company_id']
    params = {'company_id': cid}
    empty = client.get(API, params=params).json()
    assert empty['company']['template'] == sector
    assert empty['model']['id'] == company['model_id']
    assert empty['readiness'] == 0 and empty['drivers'] == []
    assert all(row['source'] == 'missing' for row in empty['nodes'])
    assert all(edge['baseline_volume'] is None for edge in empty['edges'])

    base = f'/api/companies/{cid}/onboarding'
    review = client.post(base + '/sections/sales/manual/preview', json={
        'expected_version': 1, 'records': [{'key': 'demand', 'value': 120, 'source': 'estimated'}],
    }).json()
    assert client.post(base + '/commit', json={'token': review['token']}).status_code == 200
    partial = client.get(API, params=params).json()
    nodes = {row['id']: row for row in partial['nodes']}
    assert partial['model']['version'] == 2 and 0 < partial['readiness'] < 100
    assert nodes['demand']['baseline'] == nodes['sales.volume']['baseline'] == 120
    assert nodes['demand']['source'] == nodes['sales.volume']['source'] == 'estimated'
    assert nodes['cash']['baseline'] is None
    assert [row['id'] for row in partial['drivers']] == ['demand']
    assert client.get(API, params=params | {'model_id': company['model_id'], 'version': 1}).json()['readiness'] == 0


def test_map_scope_and_goal_csrf(client):
    model = client.post('/api/enterprise/models?company_id=1', json=graph()).json()
    model_id = model['id']
    assert client.get(API, params={'version': 1}).status_code == 422
    assert client.get(API, params={'model_id': model_id, 'company_id': 2}).status_code == 404
    goal = {'model_id': model_id, 'version': 1, 'goal': {
        'trials': 4, 'target_pct': 5, 'cash_floor': 450,
        'controls': [{'node_id': 'demand', 'low': 90, 'high': 120}],
    }}
    new_account(client, 'viewer', (2,))
    with TestClient(client.app) as viewer:
        assert viewer.get(API).status_code == 401
        assert viewer.get(API + '/baseline').status_code == 401
        sign_in(viewer, 'viewer', USER_PASSWORD)
        assert viewer.get(API, params={'company_id': 1}).status_code == 404
        assert viewer.get(API, params={'model_id': model_id}).status_code == 404
        assert viewer.get(API + '/baseline', params={'model_id': model_id}).status_code == 404
        assert viewer.get(API).status_code == 200  # Shared fictional demo.
        assert viewer.post(API + '/goal-seek', json=goal).status_code == 403
    assert client.post(API + '/goal-seek', headers={'X-CSRF-Token': 'bad'}, json=goal).status_code == 403
    reference = client.post(API + '/baseline', json={'model_id': model_id, 'version': 1})
    assert reference.json() == {'profit': 240, 'cash_low': 500, 'credit_breach': None,
                                'delays': None, 'fill_rate': None}
    automatic = client.get(API + '/baseline', params={'model_id': model_id, 'version': 1})
    assert automatic.status_code == 200 and automatic.json() == reference.json()
    result = client.post(API + '/goal-seek', json=goal)
    assert result.status_code == 200, result.text
    first = result.json()['recommendations'][0]
    assert first['target_met'] and first['cash'] >= 450
    assert first['critique']['weakest']['node_id'] == 'demand'


def test_map_preserves_invalid_graph_for_inspection(client, db_path):
    body = graph()
    model = client.post('/api/enterprise/models?company_id=1', json=body).json()
    body['graph']['edges'].append({'source': 'cash', 'target': 'demand', 'name_ar': 'دورة', 'rule': {'multiplier': 1}})
    # This version predates rejection of cyclic graphs at the write boundary.
    with get_conn(db_path) as connection:
        connection.execute('UPDATE enterprise_model_versions SET graph_json = ? WHERE model_id = ?',
                           (encode(body['graph']), model['id']))
    result = client.get(API, params={'model_id': model['id']}).json()
    assert result['analysis_error'] and not result['drivers']
    assert len(result['edges']) == len(body['graph']['edges'])


def test_chat_preview_defers_impact_until_confirmation(client):
    model = client.post('/api/enterprise/models?company_id=1', json=graph()).json()
    with patch.object(ImpactModel, 'scenario', side_effect=AssertionError('preview must not run what-if')):
        parsed = client.post('/api/enterprise/chat', json={
            'message': QUESTION, 'model_id': model['id'], 'version': 1, 'parse_only': True,
        }).json()
    assert parsed['scenario']['assumptions'] == ASSUMPTIONS and parsed['result'] is None
    assert parsed['provider'] == 'rules'


def codex_parse(command, *, cwd, env, input, timeout):
    payload = json.loads(input.split('\nInput: ', 1)[1])
    assert payload['message'] == QUESTION
    assert 'demand' in {row['id'] for row in payload['drivers']}
    answer = json.dumps({'intent': 'what_if', 'assumptions': ASSUMPTIONS})
    result = {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': answer}}
    return subprocess.CompletedProcess(command, 0, json.dumps(result) + '\n{"type":"turn.completed"}', '')


@pytest.mark.parametrize('width', [1440, 390])
def test_map_browser(width):
    errors = []
    with (server() as origin, sync_playwright() as playwright,
          patch.object(codex_cli, 'find_codex', return_value=Path('/tmp/codex-fixture')),
          patch.object(codex_cli, '_run', side_effect=codex_parse) as codex):
        browser = playwright.chromium.launch()
        page = browser.new_page(base_url=origin, viewport={'width': width, 'height': 900})
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
        page.goto('/')
        login(page, 'qa-owner')
        page.get_by_role('button', name='افتح ديمو روافد').click()
        stage = page.get_by_test_id('map-stage')
        expect(stage).to_be_visible()
        expect(page.get_by_role('heading', name='خريطة الشركة')).to_be_visible()
        assert page.url == origin + '/'
        expect(stage.locator('[data-department]')).to_have_count(10)
        page.wait_for_function("Number(document.querySelector('.lm-stage').dataset.roadCount) > 0")
        assert stage.locator('canvas').evaluate("""el => {
            const img = el.getContext('2d').getImageData(0, 0, el.width, el.height);
            return img.data.some((value, index) => index % 4 === 3 && value > 0);
        }""")

        def shot(name, element, theme=False):
            capture(page, name, '.lm-identity h1', None)
            if destination := os.environ.get('ATHAR_P21_SCREENS'):
                directory = Path(destination)
                directory.mkdir(parents=True, exist_ok=True)
                element.screenshot(path=directory / f'p21_{name}_{width}.png', animations='disabled')
            if theme:
                expect(page.locator('html')).to_have_attribute('data-theme', 'dark')

        shot('sand', page.locator('.lm-shell'))
        page.get_by_test_id('theme-toggle').click()
        shot('night', page.locator('.lm-shell'), theme=True)
        page.get_by_test_id('theme-toggle').click()
        executive = page.get_by_test_id('map-kpis')
        expect(executive.locator('article')).to_have_count(4)
        expect(executive.locator('.lm-ribbon-value bdi').first).not_to_have_text('—', timeout=90_000)
        expect(executive.locator('.lm-ribbon-value bdi').nth(1)).not_to_have_text('—')
        shot('baseline', page.locator('.lm-shell'))

        profile = page.request.get('/api/auth/me').json()
        headers = {'X-CSRF-Token': profile['csrf_token']}
        model = page.request.post('/api/enterprise/models?company_id=1', data=graph(), headers=headers)
        assert model.ok, model.text()
        page.reload()
        expect(page.locator('.lm-identity')).to_contain_text('شركة أفق للاختبار')
        expect(stage.locator('[data-department]')).to_have_count(5)
        expect(stage).to_have_attribute('data-road-count', '3')

        page.get_by_test_id('map-question').fill(QUESTION)
        draft = post_from(page, '/enterprise/chat', page.get_by_test_id('map-ask'))
        assert draft['provider'] == 'codex' and draft['scenario']['assumptions'] == ASSUMPTIONS and draft['result'] is None
        expect(page.get_by_label('أفق الحساب')).to_have_value('52')
        expect(stage).to_have_attribute('data-ripple', 'idle')
        expect(page.get_by_test_id('map-assumption-value')).to_have_value('-20')
        expect(page.get_by_role('slider', name='منزلق الطلب')).to_have_value('-20')
        result = post_from(page, '/enterprise/whatif', page.get_by_test_id('map-run'))
        assert result['model_id'] == model.json()['id'] and result['version'] == 1
        assert result['paths'] and max(edge['rule']['lag_weeks'] for edge in result['paths']) == 2
        expect(stage).to_have_attribute('data-ripple', 'running')
        week = page.get_by_role('slider', name='أسبوع وصول الأثر')
        if width == 1440 and os.environ.get('ATHAR_P21_SCREENS'):
            expect(week).to_have_value('2')
            shot('ripple', stage)
        expect(stage).to_have_attribute('data-ripple', 'complete', timeout=8000)
        expect(page.get_by_text('سيناريو محدد · هامش عدم اليقين صفر', exact=False)).to_be_visible()
        expect(page.get_by_test_id('map-impact-kpis').locator('.lm-bands:visible')).to_have_count(0)
        expect(page.get_by_test_id('map-ranked')).to_contain_text('الأرباح والخسائر')
        expect(page.get_by_test_id('map-chain')).to_contain_text('الأسبوع 4')
        expect(week).to_have_value('4')
        week.fill('1')
        expect(page.get_by_test_id('map-chain')).not_to_contain_text('الأسبوع 4')
        week.fill('4')
        page.get_by_role('combobox', name='تركيز على قسم').select_option('operations')
        expect(page.get_by_test_id('map-department-detail')).to_contain_text('الطاقة التشغيلية')
        expect(stage.locator('[data-department="operations"]')).to_have_attribute('data-tone', 'critical')
        shot('impact', page.locator('.lm-shell'))

        panel = page.locator('.lm-output')
        panel.get_by_text('توصيات لهدف الربح', exact=True).click()
        panel.get_by_label('المدخل القابل للتحكم').select_option('demand')
        panel.get_by_label('من', exact=True).fill('90')
        panel.get_by_label('إلى', exact=True).fill('120')
        panel.get_by_label('زيادة الربح المطلوبة (%)').fill('5')
        panel.get_by_label('الحد الأدنى المقبول للسيولة (ر.س)').fill('450')
        recommended = post_from(page, '/enterprise/map/goal-seek', panel.get_by_role('button', name='دور على توصيات'))
        assert recommended['recommendations'][0]['target_met']
        expect(page.get_by_test_id('map-recommendations')).to_contain_text('محامي الشيطان')

        panel.get_by_text('مقارنة السيناريوهات', exact=True).click()
        panel.get_by_label('اسم آخر سيناريو اتحسب').fill('طلب أقل')
        first = post_from(page, '/enterprise/scenarios', panel.get_by_role('button', name='احفظ للمقارنة'))
        panel.get_by_role('button', name='راجع الفرضيات دي').first.click()
        post_from(page, '/enterprise/whatif', page.get_by_test_id('map-run'))
        panel.get_by_label('اسم آخر سيناريو اتحسب').fill('بديل من التوصية')
        second = post_from(page, '/enterprise/scenarios', panel.get_by_role('button', name='احفظ للمقارنة'))
        panel.get_by_label('طلب أقل', exact=True).check()
        panel.get_by_label('بديل من التوصية', exact=True).check()
        compared = post_from(page, '/enterprise/scenarios/compare', panel.get_by_role('button', name='قارن المختار'))
        assert {item['id'] for item in compared['scenarios']} == {first['id'], second['id']}
        expect(page.get_by_test_id('map-comparison')).to_contain_text('بديل من التوصية')
        click_workspace_link(page, 'لوحة التحكم')
        expect(page.get_by_test_id('page-dashboard')).to_be_visible()
        click_workspace_link(page, 'خريطة الشركة')
        expect(stage).to_have_attribute('data-ripple', 'idle')
        assert not errors, errors
        assert codex.call_count == 1
        browser.close()
