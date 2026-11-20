"""Playwright acceptance for the new company setup on desktop and mobile."""
import os
from pathlib import Path

from playwright.sync_api import expect, sync_playwright
import pytest

from qa.phase16_browser import click_workspace_link, login, server
from test_arch_d import CSV, XLSX


@pytest.mark.parametrize(('width', 'height'), [(1440, 900), (390, 844)])
def test_review_raw_company_in_browser(width, height):
    target = os.environ.get('ATHAR_ARCH_D_SCREENS')
    output = Path(target) if target else None
    if output:
        output.mkdir(parents=True, exist_ok=True)
    errors, external = [], []

    def shot(page, name):
        if output:
            page.screenshot(path=output / f'{name}-{width}x{height}.png', animations='disabled')

    with server() as base, sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(base_url=base, viewport={'width': width, 'height': height})
        page.set_default_timeout(15000)
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.on('console', lambda event: errors.append(event.text) if event.type == 'error' else None)

        def local_only(route):
            if route.request.url.startswith(base + '/'):
                route.continue_()
            else:
                external.append(route.request.url)
                route.abort()

        page.route('**/*', local_only)
        page.goto('/')
        login(page, 'qa-owner')
        click_workspace_link(page, 'تجهيز الشركة')
        page.get_by_label('اسم الشركة').fill('تجزئة المدن')
        expect(page.get_by_label('قالب القطاع')).to_have_value('enterprise_company')
        page.get_by_role('button', name='أنشئ الشركة').click()
        expect(page.get_by_role('heading', name='تجزئة المدن')).to_be_visible()
        page.evaluate('document.fonts.ready')
        shot(page, '01-upload')
        page.get_by_label('ملفات CSV أو XLSX', exact=True).set_input_files([CSV, XLSX])
        page.get_by_role('button', name='راجع الملفات').click()
        review = page.get_by_test_id('company-import-review')
        expect(review).to_contain_text('16 قراءة جديدة')
        expect(review).to_contain_text('دليل الحسابات')
        review.get_by_text('الكيانات ومراجعها', exact=True).click()
        review.get_by_text('القيم ومصدر كل قراءة', exact=True).click()
        expect(review).to_contain_text('إيرادات التجزئة')
        expect(review).to_contain_text('net_sales_sar')
        review.scroll_into_view_if_needed()
        shot(page, '02-review')
        if output:
            review.screenshot(path=output / f'03-review-detail-{width}x{height}.png', animations='disabled')
        review.get_by_role('button', name='اعتمد وابنِ الجراف').click()
        expect(page.get_by_role('status').filter(has_text='اتعمدت البيانات')).to_contain_text('2')
        expect(page.get_by_role('heading', name='البيانات المعتمدة')).to_be_visible()
        page.evaluate('window.scrollTo(0, 0)')
        shot(page, '04-approved')
        page.get_by_role('button', name='مراجعة 1 · معتمدة').click()
        expect(review).to_be_visible()
        expect(review.get_by_role('button', name='اعتمد وابنِ الجراف')).to_be_hidden()
        page.get_by_role('link', name='افتح خريطة الشركة').click()
        expect(page.get_by_test_id('map-data-context')).to_contain_text('ملفات الشركة المعتمدة')
        expect(page.get_by_test_id('map-kpis')).to_contain_text('صافي المبيعات')
        expect(page.get_by_test_id('map-kpis')).to_contain_text('500')
        assert page.evaluate('getComputedStyle(document.body).direction') == 'rtl'
        assert 'Thmanyah Serif' in page.evaluate('getComputedStyle(document.body).fontFamily')
        assert page.evaluate('document.fonts.check(\'700 16px "Thmanyah Serif Text"\')')
        assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
        page.evaluate('window.scrollTo(0, 0)')
        shot(page, '05-map')
        click_workspace_link(page, 'ربط البيانات')
        page.get_by_role('button', name='موصل ERP', exact=True).click()
        expect(page.locator('#erp-company-name')).to_have_text('تجزئة المدن')
        expect(page.get_by_role('link', name='اسحب وراجع في تجهيز الشركة')).to_be_visible()
        page.evaluate('window.scrollTo(0, 0)')
        shot(page, '06-connectors')
        assert not errors and not external, (errors, external)
        browser.close()
