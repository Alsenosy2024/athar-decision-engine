"""Phase 1 browser checks against a running local instance with a disposable DB."""
import argparse
from io import BytesIO
from pathlib import Path
from urllib.parse import urlsplit

from openpyxl import load_workbook
from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
SCREENS = ROOT / 'qa' / 'screens'


def main(base):
    SCREENS.mkdir(exist_ok=True)
    external, app_errors, expected_validation_errors = [], [], []

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        context = browser.new_context(viewport={'width': 1440, 'height': 900})

        def local_only(route):
            if urlsplit(route.request.url).netloc != urlsplit(base).netloc:
                external.append(route.request.url)
                route.abort()
            else:
                route.continue_()

        context.route('**/*', local_only)
        page = context.new_page()
        page.on('pageerror', lambda error: app_errors.append(str(error)))

        def on_console(message):
            if message.type != 'error':
                return
            if ('422' in message.text and
                    urlsplit(message.location.get('url', '')).path.endswith('/datasets/upload')):
                expected_validation_errors.append(message.text)
            else:
                app_errors.append(message.text)

        page.on('console', on_console)
        assert page.goto(base).ok
        expect(page.locator('html')).to_have_attribute('dir', 'rtl')
        expect(page.locator('section[data-testid^="page-"]')).to_have_count(9)
        cards = page.get_by_test_id('active-dataset')
        expect(cards).to_contain_text('البيانات التجريبية')
        expect(cards).to_contain_text('60')
        page.evaluate('document.fonts.ready')
        assert page.evaluate("document.fonts.check('16px Thmanyah Serif Text', 'البيانات')")
        assert page.evaluate("document.fonts.check('900 64px Thmanyah Serif Display', 'البيانات')")
        assert page.evaluate("Chart.version") == '4.4.1'
        assert page.evaluate("Alpine.version") == '3.14.9'
        page.screenshot(path=SCREENS / 'phase1-contracting-data.png', full_page=True)

        for cid, sector, expected_count in [('1', 'contracting', '60'), ('2', 'production', '312')]:
            page.get_by_test_id('company-switcher').select_option(cid)
            expect(cards).to_contain_text(expected_count)
            with page.expect_response(lambda response: response.url.endswith(f'/companies/{cid}/datasets/demo')) as demo:
                page.get_by_test_id('load-demo').click()
            assert demo.value.ok
            expect(page.get_by_role('status')).to_have_text('تم تحميل البيانات التجريبية.')

            with page.expect_download() as download:
                page.get_by_test_id('download-template').click()
            filename = f'{sector}_template.xlsx'
            assert download.value.suggested_filename == filename
            path = download.value.path()
            workbook = load_workbook(BytesIO(path.read_bytes()))
            assert all(sheet.sheet_view.rightToLeft and sheet.max_row == 4 for sheet in workbook.worksheets)
            if sector == 'production':
                workbook['orders']['C2'] = 'INVALID'
                output = BytesIO()
                workbook.save(output)
                invalid = output.getvalue()
            workbook.close()

            page.get_by_test_id('dataset-file').set_input_files(str(ROOT / 'static' / 'templates' / filename))
            with page.expect_response(lambda response: response.url.endswith(f'/companies/{cid}/datasets/upload')) as upload:
                page.get_by_role('button', name='رفع وتحقق').click()
            assert upload.value.ok
            expect(page.get_by_role('status')).to_have_text('تم قبول الملف وتفعيله.')
            expect(cards).to_contain_text(filename)
            print(f'{sector}: demo, workbook download/reopen, XLSX upload OK')

        page.get_by_test_id('dataset-file').set_input_files({
            'name': 'invalid.xlsx',
            'mimeType': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            'buffer': invalid,
        })
        with page.expect_response(lambda response: response.url.endswith('/companies/2/datasets/upload')) as rejected:
            page.get_by_role('button', name='رفع وتحقق').click()
        assert rejected.value.status == 422
        errors = page.get_by_test_id('validation-errors')
        expect(errors).to_contain_text('sku_id')
        expect(errors).to_contain_text('المعرّف غير موجود')
        expect(cards).to_contain_text('production_template.xlsx')
        page.get_by_test_id('theme-toggle').click()
        expect(page.locator('html')).to_have_attribute('data-theme', 'dark')
        page.screenshot(path=SCREENS / 'phase1-production-validation-dark.png', full_page=True)

        page.get_by_role('link', name='نموذج الشركة').click()
        model = page.get_by_test_id('page-model')
        expect(model).to_be_visible()
        expect(model.get_by_text('الإصدار 1')).to_be_visible()
        model.get_by_role('spinbutton', name='حد الائتمان (ريال)').fill('155000')
        with page.expect_response(lambda response: response.request.method == 'PUT' and response.url.endswith('/companies/2/model')) as save:
            model.get_by_role('button', name='حفظ إصدار جديد').click()
        assert save.value.ok
        expect(model.get_by_text('الإصدار 2')).to_be_visible()
        page.set_viewport_size({"width": 1440, "height": 1120})

        page.evaluate("async () => { await document.fonts.ready; window.scrollTo(0, 0); }")
        page.screenshot(path=SCREENS / 'phase1-production-model-dark.png', full_page=True)
        page.reload()
        expect(page.locator('html')).to_have_attribute('data-theme', 'dark')
        page.get_by_role('link', name='البيانات', exact=True).click()
        page.get_by_test_id('theme-toggle').click()
        page.set_viewport_size({'width': 390, 'height': 844})
        expect(cards).to_contain_text('contracting_template.xlsx')
        assert page.evaluate('document.documentElement.scrollWidth === innerWidth')
        page.screenshot(path=SCREENS / 'phase1-contracting-mobile.png', full_page=True)
        browser.close()

    print('Validation 422 details, active dataset retention, model save, theme persistence, mobile RTL OK')
    print(f'console_errors={len(app_errors)} expected_validation_http_errors={len(expected_validation_errors)} external_requests={len(external)}')
    assert not app_errors, app_errors
    assert not external, external


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base', default='http://127.0.0.1:8765')
    main(parser.parse_args().base)
