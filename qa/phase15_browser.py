"""Check Phase 15 against Chromium, a loopback server and a fresh SQLite DB."""
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
PASSWORD = 'phase15-browser-only-password'


def login(page, base, username, target='/'):
    page.goto(base + target)
    expect(page.get_by_role('heading', name='تسجيل الدخول')).to_be_visible()
    page.get_by_label('اسم المستخدم').fill(username)
    page.get_by_label('كلمة السر').fill(PASSWORD)
    page.get_by_role('button', name='ادخل', exact=True).click()
    expect(page.get_by_test_id('company-switcher')).to_be_visible()


def create_account(page, username, role):
    page.locator('[name="account"]').select_option('')
    page.get_by_label('اسم المستخدم').fill(username)
    page.get_by_label('الدور').select_option(role)
    page.get_by_role('checkbox', name='شركة البناء النموذجية').check()
    page.locator('[name="password"]').fill(PASSWORD)
    page.get_by_role('button', name='احفظ الحساب').click()
    expect(page.get_by_role('status')).to_have_text('اتحفظ الحساب والصلاحيات.')


def shot(page, filename, heading):
    page.evaluate('() => document.fonts.ready')
    assert page.locator('html').get_attribute('dir') == 'rtl'
    assert page.evaluate('document.documentElement.scrollWidth === innerWidth')
    client = page.context.new_cdp_session(page)
    client.send('DOM.enable')
    client.send('CSS.enable')
    root = client.send('DOM.getDocument')['root']['nodeId']
    node = client.send('DOM.querySelector', {'nodeId': root, 'selector': heading})['nodeId']
    fonts = client.send('CSS.getPlatformFontsForNode', {'nodeId': node})['fonts']
    assert any(row['isCustomFont'] and 'thmanyah' in row['familyName'].lower() for row in fonts), fonts
    client.detach()
    page.screenshot(path=SCREENS / filename, full_page=True, animations='disabled')


def exercise(base):
    page_errors, console_errors, external = [], [], []

    def local_only(route):
        if urlsplit(route.request.url).netloc == urlsplit(base).netloc:
            route.continue_()
        else:
            external.append(route.request.url)
            route.abort()

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()

        def new_page(width):
            page = browser.new_page(viewport={'width': width, 'height': 900})
            page.on('pageerror', lambda event: page_errors.append(str(event)))
            page.on('console', lambda event: console_errors.append(event.text) if event.type == 'error' else None)
            page.route('**/*', local_only)
            return page

        admin = new_page(1440)
        admin.goto(base)
        shot(admin, 'p15_login_1440.png', 'h1')
        login(admin, base, 'qa-owner')
        admin.get_by_role('link', name='المستخدمين').click()
        expect(admin.get_by_role('button', name='احفظ الحساب')).to_be_visible()
        create_account(admin, 'مخطط الاختبار', 'planner')
        create_account(admin, 'مراجع الاختبار', 'approver')
        shot(admin, 'p15_users_1440.png', 'h1')

        planner = new_page(1440)
        login(planner, base, 'مخطط الاختبار')
        expect(planner.get_by_test_id('company-switcher').locator('option')).to_have_count(1)
        planner.get_by_role('link', name='التحسين', exact=True).click()
        optimize = planner.get_by_test_id('page-optimize')
        with planner.expect_response(lambda response: response.url.endswith('/companies/1/optimize'),
                                     timeout=30_000) as run:
            optimize.get_by_test_id('run-optimize').click()
        assert run.value.ok, run.value.text()
        with planner.expect_response(lambda response: response.url.endswith('/companies/1/plans')
                                     and response.request.method == 'POST') as created:
            optimize.get_by_test_id('draft-optimized').click()
        assert created.value.ok, created.value.text()
        plan = created.value.json()
        planner.get_by_role('link', name='الاعتمادات والتصدير').click()
        expect(planner.get_by_text('مسودة', exact=True)).to_be_visible()
        expect(planner.get_by_role('button', name='راجع المسودة')).to_be_hidden()

        review = new_page(390)
        review.goto(base)
        shot(review, 'p15_login_390.png', 'h1')
        login(review, base, 'مراجع الاختبار')
        review.get_by_role('link', name='الاعتمادات والتصدير').click()
        review.get_by_role('button', name='راجع المسودة').click()
        assert review.get_by_test_id('page-approvals').locator('input').count() == 0
        review.get_by_label('تعليق').fill('اتراجعت من الحساب المخصص.')
        shot(review, 'p15_review_390.png', '[data-testid="page-approvals"] h1')
        with review.expect_response(lambda response: response.url.endswith(f'/plans/{plan["id"]}/decide')) as event:
            review.get_by_test_id('save-decision').click()
        assert event.value.ok, event.value.text()
        result = event.value.json()
        assert result['actor'] == 'مراجع الاختبار' and result['decided_by_user_id'] > 0
        expect(review.get_by_text('معتمدة', exact=True)).to_be_visible()

        with review.expect_download() as download:
            review.get_by_role('button', name='نزّل Excel للخطة ' + str(plan['id'])).click()
        with download.value.path().open('rb') as file:
            workbook = load_workbook(file, read_only=True, data_only=True)
            try:
                assert {'Activities', 'KPIs', 'CashFlow'} == set(workbook.sheetnames)
                assert workbook['Activities'].max_row > 1
            finally:
                workbook.close()
        review.get_by_role('button', name='خروج', exact=True).click()
        expect(review.get_by_role('heading', name='تسجيل الدخول')).to_be_visible()
        assert not review.context.cookies()
        print('Chromium: admin → scoped planner → draft → approver → Excel → logout', flush=True)
        browser.close()
    assert not page_errors and not console_errors and not external, (page_errors, console_errors, external)
    print('page_errors=0 console_errors=0 external_requests=0')


def main():
    with tempfile.TemporaryDirectory(prefix='athar-p15-browser-') as directory:
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            port = listener.getsockname()[1]
        base = f'http://127.0.0.1:{port}'
        env = os.environ | {'DB_PATH': str(Path(directory) / 'qa.sqlite'), 'FORECAST_ENGINE': 'stat',
                            'LLM_PROVIDER': 'rules', 'ATHAR_ADMIN_USERNAME': 'qa-owner',
                            'ATHAR_ADMIN_PASSWORD': PASSWORD, 'ATHAR_SECURE_COOKIE': '0'}
        with subprocess.Popen([sys.executable, '-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1',
                               '--port', str(port), '--log-level', 'warning'], cwd=ROOT, env=env) as server:
            try:
                deadline = time.monotonic() + 15
                while True:
                    try:
                        httpx.get(base + '/api/health', timeout=1).raise_for_status()
                        break
                    except httpx.TransportError:
                        if server.poll() is not None or time.monotonic() >= deadline:
                            raise
                        time.sleep(0.1)
                exercise(base)
            finally:
                server.terminate()
                server.wait(timeout=10)


if __name__ == '__main__':
    main()
