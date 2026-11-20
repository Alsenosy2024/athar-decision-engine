"""Exercise H2 controls using the project's real local Playwright server."""
import os
from pathlib import Path

from playwright.sync_api import expect, sync_playwright
import pytest

from qa.phase16_browser import capture, click_workspace_link, login, post_from, server


@pytest.mark.parametrize(('width', 'height'), [(1440, 900), (390, 844)])
def test_h2_solver_record_gate_and_sector_chat(width, height):
    screens = os.getenv('ATHAR_H2_SCREENS')
    destination = Path(screens) if screens else None
    errors = []
    with server() as base, sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(base_url=base, viewport={'width': width, 'height': height})
        page.set_default_timeout(15000)
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.on('console', lambda event: errors.append(event.text) if event.type == 'error' else None)
        page.route('**/*', lambda route: route.continue_() if route.request.url.startswith(base + '/') else route.abort())
        page.goto('/')
        login(page, 'qa-owner')
        page.get_by_test_id('company-switcher').select_option('1')

        def fixture(method, path, data):
            response = page.evaluate('''async ({method, path, data}) => {
                const response = await api(path, {method, headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify(data)});
                return response;
            }''', {'method': method, 'path': path, 'data': data})
            return response

        # Create a legitimate soft-policy draft, then tighten the policy before review.
        root = '/companies/1'
        fixture('PUT', root + '/model', {'constraints': {'credit_limit': 'soft'}, 'parameters': {'credit_limit': 0}})
        # Keep the setup local even though the shared browser helper defaults to Codex.
        run = fixture('POST', root + '/chat', {'message': 'شغّل التحسين', 'remote': False})
        fixture('POST', root + '/plans', {'run_id': run['run_id'], 'variant': 'optimized'})
        fixture('PUT', root + '/model', {'constraints': {'credit_limit': 'hard'}})
        fixture('POST', root + '/chat', {'message': 'شغّل التحسين', 'remote': False})

        click_workspace_link(page, 'التحسين')
        optimize = page.get_by_test_id('page-optimize')
        record = optimize.get_by_test_id('solution-record')
        expect(record).to_contain_text('غير مؤهلة للاعتماد')
        expect(record).to_contain_text('تجاوز')
        expect(optimize.get_by_test_id('draft-optimized')).to_be_disabled()
        capture(page, 'h2_optimize', '[data-testid="page-optimize"] h1', destination)

        optimize.get_by_role('link', name='اسأل أثر عن الحل').click()
        chat = page.get_by_test_id('page-chat')
        expect(chat.get_by_test_id('chat-scope')).to_have_value('sector')
        expect(chat.get_by_test_id('chat-provider')).to_have_text('قواعد محلية')
        chat.get_by_test_id('chat-input').fill('فين القيود الخانقة؟')
        reply = post_from(page, root + '/chat', chat.get_by_test_id('chat-send'))
        assert reply['evidence'] and reply['provider'] == 'rules'
        evidence = chat.get_by_test_id('chat-evidence').last
        evidence.get_by_text('دليل الأرقام').click()
        expect(evidence.get_by_role('link').first).to_have_attribute('href', f"/api/runs/{reply['run_id']}")
        evidence.scroll_into_view_if_needed()
        capture(page, 'h2_chat', '[data-testid="page-chat"] h1', destination)

        chat.get_by_test_id('chat-input').fill('لو حد الائتمان يبقى ٠؟')
        changed = post_from(page, root + '/chat', chat.get_by_test_id('chat-send'))
        assert changed['action'] == 'whatif'
        chat.get_by_role('link', name='عرض نتيجة السيناريو').click()
        whatif = page.get_by_test_id('page-whatif')
        expect(whatif.get_by_test_id('solution-record')).to_contain_text('غير مؤهلة للاعتماد')
        expect(whatif.get_by_test_id('draft-scenario')).to_be_disabled()
        capture(page, 'h2_whatif', '[data-testid="page-whatif"] h1', destination)

        click_workspace_link(page, 'الاعتمادات والتصدير')
        approvals = page.get_by_test_id('page-approvals')
        approvals.get_by_role('button', name='راجع المسودة').click()
        expect(approvals.get_by_role('combobox', name='القرار')).to_have_value('rejected')
        assert approvals.locator('option[value="approved"]').evaluate('option => option.disabled')
        expect(approvals.get_by_text('فيه تجاوز لقيد إلزامي').first).to_be_visible()
        capture(page, 'h2_approvals', '[data-testid="page-approvals"] h1', destination)
        assert not errors, errors
        browser.close()
