"""Chromium acceptance for edge approval and closing a measured decision."""
import os
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

from app.db import utc_now
from qa.phase16_browser import login, post_from, server
from test_p18_prescriptive import draft_body, model_data


def test_edge_approval_and_closed_decision_in_workspace():
    with server() as base, sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={'width': 1440, 'height': 1000}, reduced_motion='reduce')
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto(base + '/#/map')
        login(page, 'qa-owner')
        csrf = page.request.get(base + '/api/auth/me').json()['csrf_token']
        headers = {'X-CSRF-Token': csrf}
        body = model_data()
        body['graph']['edges'].append({
            'source': 'price', 'target': 'terms', 'name_ar': 'أثر السعر على شروط التحصيل',
            'status': 'draft', 'unit': '1', 'rule': {'mode': 'elasticity', 'multiplier': 1}, 'evidence': 'كشف بيانات التحصيل',
        })
        response = page.request.post(base + '/api/enterprise/models?company_id=1', data=body, headers=headers)
        assert response.ok, response.text()
        model_id = response.json()['id']
        page.reload()
        edges = page.get_by_test_id('map-edge-contracts')
        expect(edges).to_be_visible()
        edges.locator('summary').click()
        expect(edges).to_contain_text('كشف بيانات التحصيل')
        expect(edges).to_contain_text('qa-owner')

        def screenshot(name, target):
            if directory := os.environ.get('ATHAR_ARCH_F_SCREENS'):
                destination = Path(directory)
                destination.mkdir(parents=True, exist_ok=True)
                target.scroll_into_view_if_needed()
                page.screenshot(path=destination / f'{name}.png', animations='disabled')

        screenshot('edge-draft', edges)
        approved = post_from(page, f'/enterprise/models/{model_id}/edges/price/terms/approve',
                             edges.get_by_role('button', name='اعتمد العلاقة'))
        assert approved['version'] == 2
        expect(edges).to_be_hidden()
        # Prescriptive controls in this fixture intentionally depend on terms
        # being independently adjustable. Keep that on a fresh model.
        assert page.request.delete(base + f'/api/enterprise/models/{model_id}', headers=headers).ok
        response = page.request.post(base + '/api/enterprise/models?company_id=1', data=model_data(), headers=headers)
        assert response.ok, response.text()
        model_id = response.json()['id']
        scope = f'/enterprise/models/{model_id}/prescriptive/decisions'
        created = page.request.post(base + '/api' + scope, data=draft_body(), headers=headers)
        assert created.ok, created.text()
        decision_id = created.json()['id']
        page.goto(base + '/#/prescriptive')
        page.get_by_test_id('p18-tab-log').click()
        decision = page.get_by_test_id(f'p18-decision-{decision_id}')
        decision.locator('summary').click()
        post_from(page, f'{scope}/{decision_id}/decide', decision.get_by_role('button', name='اعتمد', exact=True))
        decision.get_by_role('button', name='سجّل القيم الفعلية').click()
        page.get_by_test_id('p18-actual-date').fill(utc_now()[:10])
        page.get_by_test_id('p18-actual-profit').fill('100')
        page.get_by_test_id('p18-actual-cash_low').fill('50')
        decision.get_by_label('مرجع القياس').fill('دفتر الحسابات')
        decision.get_by_role('checkbox', name='اقفل القرار بالقيم الفعلية').check()
        post_from(page, f'{scope}/{decision_id}/actuals', decision.get_by_role('button', name='احفظ الفعلي'))
        expect(decision.get_by_test_id('decision-outcome-state')).to_have_text('مقفل بالفعلي')
        expect(decision).to_contain_text('108')
        screenshot('decision-closed', decision)
        page.goto(base + '/#/map')
        map_outcomes = page.get_by_test_id('map-decision-outcomes')
        expect(map_outcomes).to_be_visible()
        map_outcomes.locator('summary').click()
        expect(map_outcomes).to_contain_text('مقفل بالفعلي')
        expect(map_outcomes).to_contain_text('108')
        screenshot('map-outcome', map_outcomes)
        assert page.evaluate("getComputedStyle(document.body).fontFamily").startswith('"Thmanyah Serif')
        page.goto(base + f'/ai?company_id=1&model_id={model_id}')
        page.get_by_role('button', name='اختبار قرار', exact=True).click()
        page.get_by_label('المدخل', exact=True).select_option('price')
        page.get_by_label('حد أدنى', exact=True).fill('10')
        page.get_by_label('حد أعلى', exact=True).fill('13')
        page.get_by_label('التحسّن المطلوب (%، اختياري)').fill('5')
        result = post_from(page, '/companies/1/ai/recommend',
                           page.get_by_role('button', name='رتّب التوصيات'), timeout=15_000)
        assert result['data']['recommendations'][0]['disposition'] == 'eligible_decision'
        decision_control = page.get_by_role('button', name='احفظ السيناريو ومسودة القرار').first
        expect(decision_control).to_be_visible()
        screenshot('lab-recommendation', decision_control)
        assert not errors, errors
        browser.close()
