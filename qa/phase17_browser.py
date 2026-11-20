"""Exercise both onboarding modes against a fresh local app in Chromium."""
import argparse
from io import BytesIO
from pathlib import Path
from urllib.parse import urlsplit

from openpyxl import load_workbook
from playwright.sync_api import expect, sync_playwright

from qa.phase16_browser import capture, check_chart, login, server, click_workspace_link


def upload_template(page):
    with page.expect_download() as download:
        page.get_by_role('link', name='نزّل قالب الشركة').click()
    with download.value.path().open('rb') as file:
        book = load_workbook(file)
    assert len(book.sheetnames) == 9 and all(sheet.sheet_view.rightToLeft for sheet in book)
    book['structure'].append(['department.sales', 'المبيعات'])
    book['sales'].append(['demand', 100])
    for row in [('price', 25), ('unit_cost', 10), ('offering.box', 'صندوق')]:
        book['catalog'].append(row)
    book['operations'].append(['capacity', 120])
    book['inventory'].append(['inventory', 60])
    book['suppliers'].append(['supplier.delay', 5])
    book['workforce'].append(['workforce', 4])
    for row in [('cash.opening', 1000), ('ar', 0), ('ap', 800), ('credit', 500),
                ('financing', 10), ('pnl.expenses', 100)]:
        book['finance'].append(row)
    book['risk'].append(['risk.exposure', 10, None, 'estimated'])
    out = BytesIO()
    book.save(out)
    book.close()
    page.get_by_label('ملف Excel', exact=True).set_input_files({
        'name': 'qa-company.xlsx',
        'mimeType': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        'buffer': out.getvalue(),
    })
    page.get_by_role('button', name='راجع ملف Excel').click()


def run(width, screens=None):
    with server() as url, sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={'width': width, 'height': 900})
        page.set_default_timeout(8000)
        errors, external = [], []
        page.on('console', lambda event: errors.append(event.text) if event.type == 'error' else None)
        page.on('pageerror', lambda error: errors.append(str(error)))
        allowed = urlsplit(url).netloc

        def route_local(route):
            if urlsplit(route.request.url).netloc == allowed:
                route.continue_()
            else:
                external.append(route.request.url)
                route.abort()

        page.route('**/*', route_local)
        try:
            page.goto(url)
            login(page, 'qa-owner')
            click_workspace_link(page, 'تجهيز الشركة')
            page.get_by_label('اسم الشركة').fill('شركة توزيع تجريبية')
            page.get_by_label('قالب القطاع').select_option('trading')
            page.get_by_role('button', name='أنشئ الشركة').click()
            expect(page.get_by_role('heading', name='شركة توزيع تجريبية')).to_be_visible()
            expect(page.get_by_test_id('company-switcher')).to_have_value('3')
            expect(page.get_by_role('heading', name='جاهزية البيانات 0%')).to_be_visible()
            expect(page.get_by_role('button', name='القسم السابق')).to_be_disabled()

            page.get_by_role('button', name='ضيف سجل').click()
            page.get_by_label('معرّف السجل').fill('finance')
            page.get_by_role('textbox', name='اسم القسم').fill('المالية')
            page.get_by_role('button', name='راجع الإدخال').click()
            expect(page.get_by_test_id('onboarding-review')).to_contain_text('المالية')
            expect(page.get_by_role('heading', name='جاهزية البيانات 0%')).to_be_visible()
            page.get_by_role('button', name='احفظ إصدار جديد').click()
            expect(page.get_by_role('status').filter(has_text='اتحفظ الإصدار')).to_contain_text('2')
            page.get_by_role('button', name='القسم التالي').click()
            page.get_by_role('textbox', name='الطلب الأسبوعي', exact=True).fill('١٢٠')
            page.get_by_label('مصدر القيمة').select_option('estimated')
            page.get_by_role('button', name='راجع الإدخال').click()
            expect(page.get_by_test_id('onboarding-review')).to_contain_text('120 مقدّر')
            page.get_by_role('button', name='احفظ إصدار جديد').click()
            expect(page.get_by_role('status').filter(has_text='اتحفظ الإصدار')).to_contain_text('3')
            page.get_by_role('button', name='المالية 0%', exact=True).click()
            expect(page.get_by_text('توقع الكاش محتاج', exact=False)).to_contain_text('ذمم العملاء')
            capture(page, 'onboarding', '#onboarding-panel h1', None)
            if screens:
                page.evaluate('window.scrollTo(0, 0)')
                page.screenshot(path=screens / f'p17_readiness_{width}.png', animations='disabled')

            page.get_by_role('button', name='رفع شامل').click()
            upload_template(page)
            review = page.get_by_test_id('onboarding-review')
            expect(review.get_by_role('row').filter(has_text='الطلب الأسبوعي')).to_contain_text('120 مقدّر')
            expect(review.get_by_role('row').filter(has_text='الطلب الأسبوعي')).to_contain_text('100 فعلي')
            expect(review).to_contain_text('صندوق')
            capture(page, 'review', '#onboarding-panel h1', None)
            if screens:
                review.screenshot(path=screens / f'p17_review_{width}.png', animations='disabled')
            page.get_by_role('button', name='احفظ إصدار جديد').click()
            expect(page.get_by_role('heading', name='جاهزية البيانات 100%')).to_be_visible()
            page.get_by_role('link', name='افتح أثر الفرضية').click()
            expect(page.get_by_test_id('impact-model')).to_have_value('1')
            page.get_by_test_id('impact-driver').select_option('demand')
            page.get_by_test_id('impact-value').fill('10')
            page.get_by_test_id('impact-run').click()
            expect(page.get_by_test_id('impact-ranking')).to_contain_text('مقدّر')
            expect(page.get_by_test_id('impact-ranking')).to_contain_text('فعلي')
            check_chart(page.get_by_test_id('chart-impact-cash'))
            capture(page, 'impact', '[data-testid="page-impact"] h1', None)
            if screens:
                page.get_by_test_id('chart-impact-cash').locator('..').locator('..').screenshot(
                    path=screens / f'p17_impact_{width}.png', animations='disabled')
            assert not errors, errors
            assert not external, external
        finally:
            browser.close()
    print(f'Phase 17 Chromium {width}: 0 console errors, 0 external requests')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--screens', type=Path, required=True)
    args = parser.parse_args()
    args.screens.mkdir(parents=True, exist_ok=True)
    for viewport in (1440, 390):
        run(viewport, args.screens)
