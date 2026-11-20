import os
from pathlib import Path

from playwright.sync_api import expect, sync_playwright
import pytest

from app.db import utc_now
from qa.phase16_browser import capture, login, post_from, server, click_workspace_link
from test_p18_prescriptive import model_data


@pytest.mark.parametrize('width', [1440, 390])
def test_prescriptive_browser_workflow(width):
    errors = []
    with server() as base, sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={'width': width, 'height': 900})
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.on('console', lambda event: errors.append(event.text) if event.type == 'error' else None)
        page.on('response', lambda response: errors.append(f'{response.status} {response.url}')
                if response.status >= 400 else None)

        def local_only(route):
            if route.request.url.startswith(base + '/'):
                route.continue_()
            else:
                errors.append('External request: ' + route.request.url)
                route.abort()

        page.route('**/*', local_only)
        page.goto(base + '/#/prescriptive')
        login(page, 'qa-owner')
        csrf = page.request.get(base + '/api/auth/me').json()['csrf_token']
        headers = {'X-CSRF-Token': csrf}
        data = model_data()
        response = page.request.post(base + '/api/enterprise/models?company_id=1', data=data, headers=headers)
        assert response.ok, response.text()
        model_id = response.json()['id']
        model_url = base + f'/api/enterprise/models/{model_id}'
        endpoint = f'/enterprise/models/{model_id}/prescriptive'
        page.reload()

        def screen(name):
            capture(page, name, '[data-testid="page-prescriptive"] h1', None)
            if directory := os.environ.get('ATHAR_P18_SCREENS'):
                destination = Path(directory)
                destination.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=destination / f'p18_{name}_{width}.png', full_page=True, animations='disabled')

        page.get_by_test_id('p18-min-0').fill('10')
        page.get_by_test_id('p18-max-0').fill('13')
        page.get_by_test_id('p18-goal-text').fill('عايز أرفع الربح ١٠٪ بدون ما الكاش ينزل تحت ٥٠')
        parsed = post_from(page, endpoint + '/goal-input', page.get_by_test_id('p18-parse'), timeout=15_000)
        assert parsed == {'profit_pct': 10, 'cash_minimum': 50}
        expect(page.get_by_test_id('p18-cash')).to_have_value('50')
        page.get_by_role('button', name='ضيف مدخل').click()
        page.get_by_test_id('p18-min-1').fill('8')
        page.get_by_test_id('p18-max-1').fill('10')
        search = post_from(page, endpoint + '/goal-seek', page.get_by_test_id('p18-search'), timeout=15_000)
        best, = search['recommendations']
        assert best['kpis']['profit'] == 113 and best['kpis']['cash_low'] == 50
        expect(page.get_by_test_id('p18-recommendations')).to_contain_text('113')
        screen('goal')

        page.get_by_test_id('p18-draft-name').fill('تعديل السعر والتحصيل')
        draft = post_from(page, endpoint + '/decisions', page.get_by_test_id('p18-save-draft'), timeout=15_000)
        page.get_by_test_id('p18-tab-log').click()
        item = page.get_by_test_id(f'p18-decision-{draft["id"]}')
        item.locator('summary').click()
        decision_path = endpoint + f'/decisions/{draft["id"]}'
        post_from(page, decision_path + '/decide', item.get_by_role('button', name='اعتمد', exact=True), timeout=15_000)
        item.get_by_role('button', name='سجّل القيم الفعلية').click()
        page.get_by_test_id('p18-actual-date').fill(utc_now()[:10])
        page.get_by_test_id('p18-actual-profit').fill('110')
        post_from(page, decision_path + '/actuals', item.get_by_role('button', name='احفظ الفعلي'), timeout=15_000)
        expect(page.get_by_test_id('p18-accuracy')).to_contain_text('2.7')
        screen('accuracy')

        page.get_by_test_id('p18-tab-calibration').click()
        page.get_by_test_id('p18-preview').click()
        expect(page.get_by_text('افتراضي', exact=True)).to_have_count(4)
        post_from(page, endpoint + '/calibration', page.get_by_test_id('p18-apply'), timeout=15_000)
        expect(page.get_by_text('اتحفظت المعايرة على الإصدار 1.')).to_be_visible()
        screen('calibration')

        page.get_by_test_id('p18-tab-monitor').click()
        expect(page.get_by_text('مفيش تجاوز للحدود المضبوطة.')).to_be_visible()
        page.get_by_test_id('p18-threshold-cash_minimum').fill('55')
        page.get_by_test_id('p18-threshold-delays_maximum').fill('1')
        page.get_by_test_id('p18-save-thresholds').click()
        expect(page.get_by_test_id('p18-alert')).to_have_count(2)

        data['graph']['nodes'][3]['baseline']['value'] = 60
        changed = page.request.put(model_url, data={**data, 'expected_version': 1}, headers=headers)
        assert changed.ok, changed.text()
        click_workspace_link(page, 'لوحة التحكم')
        click_workspace_link(page, 'القرارات والمتابعة')
        expect(page.get_by_test_id('p18-alert')).to_have_count(1)
        expect(page.get_by_test_id('p18-alert')).to_contain_text('تأخير')
        screen('monitor')
        assert not errors, errors
        browser.close()
