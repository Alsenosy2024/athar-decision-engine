"""Exercise imports in Chromium with an isolated Athar database and local mock ERP."""
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from pathlib import Path
from urllib.parse import urlsplit
from xml.etree.ElementTree import fromstring

import httpx
from openpyxl import Workbook
from playwright.sync_api import expect, sync_playwright

from phase15_browser import PASSWORD, login

ROOT = Path(__file__).resolve().parents[1]
SCREENS = ROOT / 'qa' / 'screens'
P6_XML = b'''<APIBusinessObjects><Project><ObjectId>1</ObjectId><Id>P6-DEMO</Id>
<Activity><ObjectId>10</ObjectId><Id>A001</Id><Name>Foundations</Name><PlannedDuration>112</PlannedDuration>
<PlannedTotalCost>300000</PlannedTotalCost></Activity>
<Activity><ObjectId>11</ObjectId><Id>A002</Id><Name>Structure</Name><PlannedDuration>224</PlannedDuration>
<PlannedTotalCost>520000</PlannedTotalCost></Activity>
<Relationship><PredecessorActivityObjectId>10</PredecessorActivityObjectId>
<SuccessorActivityObjectId>11</SuccessorActivityObjectId><Type>Finish to Start</Type><Lag>0</Lag></Relationship>
</Project></APIBusinessObjects>'''


class MockERP(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != '/metrics' or self.headers.get('Authorization') != 'Bearer test-only':
            self.send_error(401)
            return
        body = json.dumps({'data': {'rows': [{'metric': 'credit', 'amount': 2800000}]}}).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def check_browser(url):
    errors, external = [], []

    def route_request(route):
        if urlsplit(route.request.url).netloc == urlsplit(url).netloc:
            route.continue_()
        else:
            external.append(route.request.url)
            route.abort()

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={'width': 1440, 'height': 900})
        page.set_default_timeout(8000)
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.on('console', lambda event: errors.append(event.text) if event.type == 'error' else None)
        page.route('**/*', route_request)
        login(page, url, 'qa-owner', '/login#/connectors')
        expect(page.get_by_role('heading', name='ربط البيانات')).to_be_visible()
        page.get_by_role('link', name='افتح النموذج المؤسسي').click()
        page.get_by_test_id('enterprise-save-demo').click()
        expect(page.get_by_text('اتحفظ الديمو كإصدار أول.')).to_be_visible()
        page.get_by_role('link', name='ربط البيانات').click()
        expect(page.get_by_test_id('import-model')).to_have_value('1')
        file = page.get_by_test_id('mapped-file')
        file.set_input_files({'name': 'wrong.csv', 'mimeType': 'text/csv',
                              'buffer': b'metric,total\ncredit,NaN\n'})
        page.get_by_test_id('map-node').select_option('metric')
        page.get_by_test_id('map-value').select_option('total')
        page.get_by_role('button', name='راجع البيانات').click()
        expect(page.get_by_text('أدخل رقمًا صالحًا.')).to_be_visible()
        expect(page.get_by_role('button', name='احفظ إصدار جديد')).to_be_hidden()

        book = Workbook()
        book.active.title = 'قيمة الائتمان'
        book.active.append(['رمز المؤشر', 'قيمة ERP'])
        book.active.append(['credit', 3100000])
        book.active.append(['supplier', 7])
        data = BytesIO()
        book.save(data)
        book.close()
        file.set_input_files({'name': 'balances.xlsx',
                              'mimeType': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                              'buffer': data.getvalue()})
        page.get_by_test_id('map-node').select_option('رمز المؤشر')
        page.get_by_test_id('map-value').select_option('قيمة ERP')
        page.get_by_role('button', name='راجع البيانات').click()
        expect(page.get_by_test_id('mapping-report').get_by_text('3,100,000', exact=True)).to_be_visible()
        for width in (1440, 390):
            page.set_viewport_size({'width': width, 'height': 900})
            page.evaluate('document.fonts.ready')
            assert page.locator('html').get_attribute('dir') == 'rtl'
            assert page.get_by_test_id('import-diff').evaluate('el => el.scrollWidth === el.clientWidth')
            assert page.evaluate('document.documentElement.scrollWidth === innerWidth'), page.evaluate('''() =>
                [...document.querySelectorAll('[data-testid=page-connectors] *')]
                    .filter(el => el.getBoundingClientRect().x < -1 && !el.closest('.table-scroll'))
                    .slice(0, 10).map(el => [el.tagName, el.className, el.getBoundingClientRect().x])
            ''')
            assert page.get_by_role('heading', name='ربط البيانات').evaluate(
                'item => getComputedStyle(item).fontFamily') == '"Thmanyah Serif Display"'
            cdp = page.context.new_cdp_session(page)
            cdp.send('DOM.enable')
            cdp.send('CSS.enable')
            root = cdp.send('DOM.getDocument')['root']['nodeId']
            heading = cdp.send('DOM.querySelector', {'nodeId': root, 'selector': '[data-testid=page-connectors] h1'})
            fonts = cdp.send('CSS.getPlatformFontsForNode', heading)['fonts']
            assert fonts and all(item['isCustomFont'] and 'thmanyah' in item['familyName'].lower() for item in fonts), fonts
            cdp.detach()
            page.screenshot(path=SCREENS / f'p13_mapping_{width}.png', full_page=True, animations='disabled')
        page.get_by_role('button', name='احفظ إصدار جديد').click()
        expect(page.get_by_text('اتحفظ إصدار النموذج رقم 2.')).to_be_visible()

        page.get_by_role('button', name='موصل ERP', exact=True).click()
        page.get_by_label('اسم الموصل', exact=True).fill('أرصدة المالية')
        page.get_by_label('متغير عنوان الاتصال').fill('ATHAR_ERP_REST')
        page.get_by_label('متغير رمز التوثيق').fill('ATHAR_ERP_TOKEN')
        page.get_by_label('مسار قائمة السجلات داخل JSON').fill('data.rows')
        page.get_by_label('حقل معرّف العنصر').fill('metric')
        page.get_by_label('حقل القيمة', exact=True).fill('amount')
        page.get_by_role('button', name='احفظ إعداد الموصل').click()
        expect(page.get_by_text('اتحفظ إعداد الموصل.')).to_be_visible()
        page.get_by_role('button', name='اسحب بالإعداد المحفوظ وراجع').click()
        expect(page.get_by_test_id('mapping-report').get_by_text('2,800,000', exact=True)).to_be_visible()
        page.set_viewport_size({'width': 1440, 'height': 900})
        page.screenshot(path=SCREENS / 'p13_erp_1440.png', full_page=True, animations='disabled')
        page.set_viewport_size({'width': 390, 'height': 900})
        page.get_by_role('button', name='احفظ إصدار جديد').click()
        expect(page.get_by_text('اتحفظ إصدار النموذج رقم 3.')).to_be_visible()

        page.get_by_role('button', name='Primavera P6', exact=True).click()
        page.get_by_label('ملف P6 XML', exact=True).set_input_files(
            {'name': 'sample.xml', 'mimeType': 'application/xml', 'buffer': P6_XML})
        page.get_by_role('button', name='راجع جدول P6').click()
        expect(page.get_by_text('520,000', exact=True)).to_be_visible()
        page.get_by_text('المشاريع في الملف', exact=True).click()
        expect(page.get_by_text('P6-DEMO', exact=True)).to_be_visible()
        page.get_by_text('علاقات الأنشطة', exact=True).click()
        expect(page.get_by_text('Finish to Start', exact=True)).to_be_visible()
        page.get_by_role('button', name='احفظ جدول P6').click()
        expect(page.get_by_text('اتحفظ جدول Primavera.')).to_be_visible()
        with page.expect_download() as event:
            page.get_by_role('link', name='نزّل ملف P6 XML').click()
        exported = event.value.path().read_bytes()
        assert exported == P6_XML
        assert fromstring(exported).findtext('./Project/Activity/PlannedTotalCost') == '300000'
        assert page.evaluate('document.documentElement.scrollWidth === innerWidth')
        assert page.get_by_test_id('p6-activities').evaluate('el => el.scrollWidth === el.clientWidth')
        page.screenshot(path=SCREENS / 'p13_p6_390.png', full_page=True, animations='disabled')
        browser.close()
    assert not errors, errors
    assert not external, external
    print('CSV errors, mapped XLSX, REST GET and P6 re-download passed. Console errors: 0; external requests: 0.')


if __name__ == '__main__':
    with tempfile.TemporaryDirectory() as work, ThreadingHTTPServer(('127.0.0.1', 0), MockERP) as erp:
        thread = threading.Thread(target=erp.serve_forever, daemon=True)
        thread.start()
        try:
            with socket.socket() as probe:
                probe.bind(('127.0.0.1', 0))
                port = probe.getsockname()[1]
            env = {**os.environ, 'DB_PATH': str(Path(work) / 'browser.sqlite'), 'LLM_PROVIDER': 'rules',
                   'FORECAST_ENGINE': 'stat', 'ATHAR_ERP_REST': f'http://127.0.0.1:{erp.server_port}/metrics',
                   'ATHAR_ERP_TOKEN': 'test-only', 'ATHAR_ADMIN_USERNAME': 'qa-owner',
                   'ATHAR_ADMIN_PASSWORD': PASSWORD, 'ATHAR_SECURE_COOKIE': '0'}
            server = subprocess.Popen([sys.executable, '-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1',
                                       '--port', str(port), '--log-level', 'warning'], cwd=ROOT, env=env)
            url = f'http://127.0.0.1:{port}'
            try:
                for _ in range(100):
                    if server.poll() is not None:
                        raise RuntimeError('Athar exited during startup')
                    try:
                        if httpx.get(url + '/api/health', timeout=1).is_success:
                            break
                    except httpx.ConnectError:
                        pass
                    time.sleep(0.1)
                else:
                    raise RuntimeError('Athar startup timed out')
                check_browser(url)
            finally:
                server.terminate()
                server.wait(timeout=10)
        finally:
            erp.shutdown()
            thread.join(timeout=5)
