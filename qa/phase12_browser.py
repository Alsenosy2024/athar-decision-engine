"""Check Phase 12 in Chromium against a temporary local app and SQLite database."""
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from openpyxl import load_workbook
from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
SCREENS = ROOT / 'qa' / 'screens'


def verify_download(page, label, row_check):
    with page.expect_download(timeout=120_000) as download:
        page.get_by_role('button', name=label).click()
    with download.value.path().open('rb') as file:
        book = load_workbook(file)
        try:
            assert all(sheet.sheet_view.rightToLeft for sheet in book)
            row_check(book)
        finally:
            book.close()


def matches_excel(book, sheet, cell, value):
    assert abs(book[sheet][cell].value - value) < 1e-6


def capture(page):
    for width in (1440, 390):
        page.set_viewport_size({'width': width, 'height': 900})
        page.evaluate('document.fonts.ready')
        assert page.locator('html').get_attribute('dir') == 'rtl'
        assert page.evaluate('getComputedStyle(document.body).fontFamily') == '"Thmanyah Serif Text"'
        canvas = page.get_by_test_id('chart-tornado')
        page.wait_for_function("""() => {
            const canvas = document.querySelector('[data-testid=chart-tornado]');
            const chart = Chart.getChart(canvas);
            return chart?.chartArea?.width > 110 && chart.data.datasets.length === 2
                && Math.abs(chart.width - canvas.parentElement.clientWidth) <= 1;
        }""")
        assert page.evaluate('document.documentElement.scrollWidth === innerWidth')
        assert canvas.evaluate("""canvas => {
            const chart = Chart.getChart(canvas);
            return chart.getDatasetMeta(1).data.some(bar => bar.width > 5);
        }""")
        page.screenshot(path=SCREENS / f'p12_scenarios_{width}.png', full_page=True, animations='disabled')


def check_browser(url):
    errors, external = [], []

    def local_only(route):
        if urlsplit(route.request.url).netloc == urlsplit(url).netloc:
            route.continue_()
        else:
            external.append(route.request.url)
            route.abort()

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={'width': 1440, 'height': 900})
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.on('console', lambda event: errors.append(event.text) if event.type == 'error' else None)
        page.route('**/*', local_only)
        page.goto(url + '/#/enterprise')
        page.get_by_test_id('enterprise-save-demo').click()
        expect(page.get_by_text('اتحفظ الديمو كإصدار أول.')).to_be_visible()
        page.get_by_role('link', name='احسب أثر فرضية').click()
        expect(page.get_by_test_id('impact-model')).to_have_value('1')
        expect(page.get_by_test_id('impact-driver').first).to_have_value('credit')
        page.get_by_test_id('impact-value').first.fill('0')

        def save_named(name):
            page.get_by_role('button', name='سمّي السيناريو واحفظه').click()
            page.get_by_label('اسم السيناريو').fill(name)
            page.get_by_role('button', name='احفظ السيناريو', exact=True).click()
            expect(page.get_by_text(f'اتحفظ «{name}».')).to_be_visible()

        save_named('الحد الحالي')
        page.get_by_test_id('impact-value').first.fill('-15')
        page.get_by_test_id('impact-uncertainty').first.fill('300000')
        page.get_by_role('button', name='أضف فرضية').click()
        page.get_by_test_id('impact-driver').nth(1).select_option('supplier')
        page.get_by_test_id('impact-operation').nth(1).select_option('add')
        page.get_by_test_id('impact-value').nth(1).fill('7')
        save_named('ضغط التوريد والائتمان')
        page.get_by_role('link', name='قارن السيناريوهات').click()
        scope = page.get_by_test_id('scenario-scope')
        expect(scope).to_have_value('1:1')
        scope.select_option('demo')
        expect(page.get_by_text('احفظ سيناريوهين على الأقل من صفحة أثر الفرضية.')).to_be_visible()
        scope.select_option('1:1')
        page.get_by_role('checkbox', name='الحد الحالي').check()
        page.get_by_role('checkbox', name='ضغط التوريد والائتمان').check()
        with page.expect_response(lambda r: urlsplit(r.url).path.endswith('/scenarios/compare'), timeout=120_000) as event:
            page.get_by_test_id('compare-scenarios').click()
        assert event.value.ok, event.value.text()
        comparison = event.value.json()
        expect(page.get_by_test_id('scenario-comparison').locator('tbody tr')).to_have_count(5)
        assert comparison['version'] == 1 and 0 < comparison['scenarios'][1]['kpis']['credit_breach'] < 100
        print('Comparison:', [row['kpis'] for row in comparison['scenarios']], flush=True)
        verify_download(page, 'نزّل المقارنة Excel',
                        lambda book: matches_excel(book, 'المقارنة', 'E2', comparison['scenarios'][1]['kpis']['profit']))

        page.get_by_test_id('tornado-percent').fill('20')
        for driver in ('مهلة مورد الخامات', 'مهلة سداد الخامات', 'سعة المخزن المبرد'):
            page.get_by_role('checkbox', name=driver, exact=True).check()
        with page.expect_response(lambda r: urlsplit(r.url).path.endswith('/scenarios/tornado'), timeout=120_000) as event:
            page.get_by_test_id('run-tornado').click()
        assert event.value.ok, event.value.text()
        tornado = event.value.json()
        assert tornado['percent'] == 20 and len(tornado['rows']) == 3
        assert tornado['rows'][0]['span'] > 0
        expect(page.get_by_test_id('tornado-result')).to_be_visible()
        print('Tornado:', [(row['driver']['id'], row['span']) for row in tornado['rows']], flush=True)
        verify_download(page, 'نزّل الحساسية Excel',
                        lambda book: matches_excel(book, 'الحساسية', 'H2', tornado['rows'][0]['span']))
        page.evaluate('window.scrollTo(0, 0)')
        capture(page)
        assert not errors, errors
        assert not external, external
        print('Two workbooks reopened, RTL and Thmanyah verified at 1440/390, 0 browser errors, 0 external requests.')
        browser.close()


def main():
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        port = listener.getsockname()[1]
    url = f'http://127.0.0.1:{port}'
    with tempfile.TemporaryDirectory(prefix='decision-phase12-') as folder:
        env = os.environ | {'LLM_PROVIDER': 'rules', 'FORECAST_ENGINE': 'stat',
                            'DB_PATH': str(Path(folder) / 'qa.sqlite')}
        with (Path(folder) / 'server.log').open('w+') as log:
            server = subprocess.Popen([sys.executable, '-m', 'uvicorn', 'app.main:app',
                                       '--host', '127.0.0.1', '--port', str(port)],
                                      cwd=ROOT, stdout=log, stderr=log, env=env)
            try:
                for _ in range(100):
                    if server.poll() is not None:
                        log.seek(0)
                        raise RuntimeError(log.read())
                    try:
                        if httpx.get(url + '/api/health').is_success:
                            break
                    except httpx.ConnectError:
                        pass
                    time.sleep(.1)
                else:
                    raise RuntimeError('Server startup timed out')
                check_browser(url)
            finally:
                server.terminate()
                server.wait(timeout=10)


if __name__ == '__main__':
    main()
