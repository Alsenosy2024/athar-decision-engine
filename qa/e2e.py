"""Run both demo workflows in headless Chromium against a disposable SQLite DB."""
import json
import os
import re
import socket
import subprocess
import time
from contextlib import contextmanager
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import urlopen

from openpyxl import load_workbook
from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
SCREENS = ROOT / 'qa' / 'screens'
HOST, PORT = '127.0.0.1', 8780
BASE = f'http://{HOST}:{PORT}'
DESKTOP = {'width': 1440, 'height': 900}
MOBILE = {'width': 390, 'height': 844}
PAGES = ('dashboard', 'data', 'model', 'forecast', 'optimize', 'whatif', 'chat', 'approvals', 'actuals')
TEMPLATES = (('1', 'contracting'), ('2', 'production'))
KPI_KEYS = {
    'contracting': ('total', 'ld', 'interest', 'late_weeks_total', 'peak_od'),
    'production': ('total_cost', 'late_orders', 'changeovers', 'holding_cost', 'peak_od', 'peak_storage_pct'),
}
QUESTION = 'لو الحد الائتماني قل 10% إيه اللي هيحصل؟'
COMMENT = 'تمت مراجعة المؤشرات ومطابقة الخطة للبيانات.'
ACTION_TIMEOUT_MS = 60_000
PIXELS = """canvas => {
    const context = canvas.getContext('2d');
    const pixels = context.getImageData(0, 0, canvas.width, canvas.height).data;
    // Chart.js draws on a transparent backing canvas. CSS supplies the page background.
    let painted = 0;
    for (let index = 3; index < pixels.length; index += 4) {
        if (pixels[index] !== 0) painted++;
    }
    return painted / (pixels.length / 4);
}"""


def port_open():
    with socket.socket() as sock:
        return sock.connect_ex((HOST, PORT)) == 0


def wait_for_health(server):
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline and server.poll() is None:
        try:
            with urlopen(BASE + '/api/health', timeout=2) as response:
                health = json.load(response)
                assert response.status == 200 and health['status'] == 'ok'
                assert health['provider'] == 'rules', health
                print('Health HTTP 200:', json.dumps(health), flush=True)
                return
        except (URLError, TimeoutError):
            time.sleep(0.25)
    raise RuntimeError(f'run.sh failed to become healthy (exit={server.poll()})')


@contextmanager
def local_server():
    assert not port_open(), f'Port {PORT} must be free'
    with TemporaryDirectory(prefix='decision-engine-e2e-') as folder:
        env = os.environ | {
            'PORT': str(PORT),
            'DB_PATH': str(Path(folder) / 'e2e.sqlite'),
            'LLM_PROVIDER': 'rules',
        }
        env.pop('SOLVER_TIME_LIMIT', None)
        with (Path(folder) / 'server.log').open('w+') as log:
            server = subprocess.Popen(['./run.sh'], cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
            try:
                wait_for_health(server)
                yield
            except BaseException:
                log.seek(0)
                print(log.read(), flush=True)
                raise
            finally:
                if server.poll() is None:
                    server.terminate()
                    try:
                        server.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        server.kill()
                        server.wait(timeout=5)
                assert not port_open(), f'Port {PORT} still open after stopping run.sh'
                print(f'Port {PORT} closed', flush=True)


def enter(page, key):
    page.locator(f'nav a[href="#/{key}"]').click()
    section = page.get_by_test_id(f'page-{key}')
    expect(section).to_be_visible()
    expect(section.get_by_text('جارٍ تحميل الصفحة…', exact=True)).to_be_hidden()
    expect(section.get_by_role('alert')).to_be_hidden()
    return section


def after_action(page, path, click, *, method='POST'):
    with page.expect_response(
        lambda response: urlsplit(response.url).path == '/api' + path and response.request.method == method,
        timeout=ACTION_TIMEOUT_MS,
    ) as pending:
        click()
    response = pending.value
    assert response.ok, (path, response.status, response.text())
    return response.json()


def screenshot(page, template, key, *, dark=False, mobile=False):
    section = page.get_by_test_id(f'page-{key}')
    expect(section.get_by_role('heading', level=1)).to_be_visible()
    expect(page.locator('html')).to_have_attribute('dir', 'rtl')
    expect(section.get_by_role('alert')).to_be_hidden()
    page.mouse.move(0, 0)
    page.evaluate('document.fonts.ready')
    # Chart.js resizes its canvases on the next rendering frame after a viewport change.
    page.wait_for_function('document.documentElement.scrollWidth <= innerWidth + 1')
    charts = {}
    for chart in section.locator('canvas:visible').all():
        test_id = chart.get_attribute('data-testid')
        page.wait_for_function("""id => {
            const chart = Chart.getChart(document.querySelector('[data-testid="' + id + '"]'));
            return chart?.chartArea?.width > 0 && chart.data.datasets.some(series => series.data.length);
        }""", arg=test_id)
        ratio = chart.evaluate(PIXELS)
        assert ratio > 0.01, (template, key, test_id, ratio)
        if mobile and test_id == 'chart-compare':
            assert chart.evaluate("""canvas => {
                const { chartArea, scales } = Chart.getChart(canvas);
                return chartArea.width < 390 && scales.x.ticks.every(tick =>
                    Array.isArray(tick.label) && tick.label.every(word => !word.includes(' ')));
            }"""), 'Mobile cost labels must wrap by word'
        charts[test_id] = f'{ratio:.1%}'
    index = PAGES.index(key) + 1
    suffix = '_dark' if dark else '_390' if mobile else '_1440'
    name = f'final_{template}_{index:02d}_{key}{suffix}.png'
    page.screenshot(path=SCREENS / name, full_page=True, animations='disabled')
    print(f'{name} — canvas pixels {charts}' if charts else name, flush=True)


def desktop_and_mobile(page, template, key):
    screenshot(page, template, key)
    page.set_viewport_size(MOBILE)
    screenshot(page, template, key, mobile=True)
    page.set_viewport_size(DESKTOP)


def verify_download(page, row, draft, template, optimized):
    path = f"/api/plans/{draft['id']}/export.xlsx"
    with page.expect_response(lambda response: urlsplit(response.url).path == path) as response:
        with page.expect_download() as pending:
            row.get_by_role('button', name='نزّل Excel', exact=False).click()
    assert response.value.ok, (path, response.value.status)
    download = pending.value
    assert download.suggested_filename == f"plan-{draft['id']}.xlsx"
    book = load_workbook(BytesIO(download.path().read_bytes()), read_only=True, data_only=True)
    try:
        sheets = ('Activities', 'KPIs', 'CashFlow') if template == 'contracting' else (
            'ProductionPlan', 'Shipments', 'Inventory', 'KPIs',
        )
        assert tuple(book.sheetnames) == sheets
        assert all(book[name].max_row > 1 for name in sheets)
        rows = book['KPIs'].values
        assert next(rows) == ('kpi', 'value')
        exported = dict(rows)
        assert {key: exported[key] for key in KPI_KEYS[template]} == optimized
    finally:
        book.close()


def sector_flow(page, cid, template):
    base = f'/companies/{cid}'
    page.get_by_test_id('company-switcher').select_option(cid)
    data = enter(page, 'data')
    after_action(page, base + '/datasets/demo', data.get_by_test_id('load-demo').click)
    expect(data.get_by_test_id('active-dataset')).to_contain_text('البيانات التجريبية')
    expect(data.get_by_role('status').filter(has_text='تم تحميل البيانات التجريبية')).to_be_visible()
    screenshot(page, template, 'data')

    model = enter(page, 'model')
    credit = model.get_by_role('spinbutton', name='حد الائتمان', exact=False)
    credit.fill('4000' if template == 'contracting' else '150000')
    after_action(page, base + '/model', model.get_by_role('button', name='حفظ إصدار جديد').click, method='PUT')
    expect(model.get_by_text('الإصدار 2', exact=True)).to_be_visible()
    screenshot(page, template, 'model')

    forecast = enter(page, 'forecast')
    after_action(page, base + '/forecast', forecast.get_by_test_id('run-forecast').click)
    expect(forecast.get_by_test_id('chart-forecast')).to_be_visible()
    screenshot(page, template, 'forecast')

    optimize = enter(page, 'optimize')
    result = after_action(page, base + '/optimize', optimize.get_by_test_id('run-optimize').click)
    baseline, optimized = (
        {key: result[variant]['kpis'][key] for key in KPI_KEYS[template]}
        for variant in ('baseline', 'optimized')
    )
    cost = KPI_KEYS[template][0]
    row = optimize.get_by_role('row', name=re.compile('إجمالي التكلفة'))
    for index, kpis in enumerate((baseline, optimized)):
        formatted = page.evaluate(
            "value => new Intl.NumberFormat('ar-SA-u-ca-gregory-nu-latn', {maximumFractionDigits: 2}).format(value)",
            kpis[cost],
        )
        expect(row.get_by_role('cell').nth(index)).to_have_text(formatted)
    assert optimized[cost] <= baseline[cost], (template, baseline, optimized)
    expect(optimize.get_by_test_id('chart-compare')).to_be_visible()
    desktop_and_mobile(page, template, 'optimize')

    whatif = enter(page, 'whatif')
    contracting = template == 'contracting'
    preset = 'حد الائتمان −10%' if contracting else 'خط متوقف يومين'
    whatif.get_by_role('button', name=preset, exact=True).click()
    with page.expect_request(lambda request: urlsplit(request.url).path == '/api' + base + '/whatif') as scenario_request:
        scenario = after_action(page, base + '/whatif', whatif.get_by_test_id('run-whatif').click)
    payload = scenario_request.value.post_data_json
    assert payload['base_run_id'] == result['run_id']
    change, = payload['changes']
    if contracting:
        assert change == {'type': 'credit_limit_pct', 'value': -10}, change
    else:
        assert change['type'] == 'line_down' and change['days'] == 2 and change['line'], change
    expect(whatif.get_by_role('heading', name='نتيجة السيناريو')).to_be_visible()
    expect(whatif.get_by_text(scenario['changes_ar'][0], exact=True)).to_be_visible()
    risk = after_action(page, base + '/montecarlo', whatif.get_by_test_id('run-mc').click)
    assert risk['n'] == 200
    expect(whatif.get_by_test_id('chart-mc')).to_be_visible()
    screenshot(page, template, 'whatif')

    chat = enter(page, 'chat')
    chat.get_by_test_id('chat-scope').select_option('sector')
    chat.get_by_test_id('chat-input').fill(QUESTION)
    reply = after_action(page, base + '/chat', chat.get_by_test_id('chat-send').click)
    assert reply['action'] == 'whatif' and reply['provider'] == 'rules'
    answer = chat.get_by_role('log').locator('.bubble.assistant p')
    expect(answer).to_have_text(reply['reply_ar'])
    expect(answer).to_contain_text(re.compile(r'\d'))
    expect(chat.get_by_role('link', name='عرض نتيجة السيناريو')).to_be_visible()
    desktop_and_mobile(page, template, 'chat')

    optimize = enter(page, 'optimize')
    draft = after_action(page, base + '/plans', optimize.get_by_test_id('draft-optimized').click)
    assert draft['run_id'] == result['run_id'] and draft['status'] == 'draft', draft
    expect(optimize.get_by_role('status').filter(has_text='تم حفظ مسودة')).to_be_visible()
    approvals = enter(page, 'approvals')
    row = approvals.get_by_role('row').filter(has_text=re.compile(rf"خطة {draft['id']}\b"))
    row.get_by_role('button', name='راجع المسودة').click()
    approvals.get_by_role('textbox', name='المراجع').fill('مراجع التخطيط')
    approvals.get_by_role('textbox', name='تعليق').fill(COMMENT)
    decision = after_action(page, f"/plans/{draft['id']}/decide", approvals.get_by_test_id('save-decision').click)
    assert decision['status'] == 'approved', decision
    expect(row.get_by_text('معتمدة', exact=True)).to_be_visible()
    expect(row.get_by_text(COMMENT, exact=True)).to_be_visible()
    verify_download(page, row, draft, template, optimized)
    expect(approvals.get_by_text('تصدير خطة · XLSX')).to_be_visible()
    screenshot(page, template, 'approvals')

    actuals = enter(page, 'actuals')
    actuals.get_by_label('الخطة المعتمدة').select_option(str(draft['id']))
    actuals.get_by_test_id('actuals-file').set_input_files(ROOT / 'static' / 'templates' / f'actuals_{template}_template.csv')
    uploaded = after_action(page, base + '/actuals/upload', actuals.get_by_test_id('upload-actuals').click)
    assert uploaded['comparison']['kpis_actual']['observed_rows'] > 0
    expect(actuals.get_by_test_id('chart-pva')).to_be_visible()
    calibration = after_action(page, base + '/recalibrate', actuals.get_by_test_id('recalibrate').click)
    assert calibration['calibrations']
    expect(actuals.get_by_role('status').filter(has_text=calibration['msg_ar'])).to_be_visible()
    screenshot(page, template, 'actuals')

    dashboard = enter(page, 'dashboard')
    expect(dashboard.get_by_test_id('chart-' + ('gantt' if contracting else 'plan'))).to_be_visible()
    expect(dashboard.get_by_test_id('chart-cash')).to_be_visible()
    desktop_and_mobile(page, template, 'dashboard')
    page.get_by_test_id('theme-toggle').click()
    expect(page.locator('html')).to_have_attribute('data-theme', 'dark')
    screenshot(page, template, 'dashboard', dark=True)
    page.get_by_test_id('theme-toggle').click()
    expect(page.locator('html')).to_have_attribute('data-theme', 'light')

    risk_keys = ('p_any_late', 'p_credit_breach', 'late_weeks', 'total_cost', 'peak_od') if contracting else (
        'p_any_late', 'p_credit_breach', 'p_storage_overflow', 'fill_rate', 'late_orders',
    )
    risk_summary = {'n': risk['n'], **{key: risk[key] for key in risk_keys}}
    for key, value in list(risk_summary.items()):
        if isinstance(value, dict):
            risk_summary[key] = {k: v for k, v in value.items() if k != 'hist'}
    print(f'{template} KPIs:', json.dumps({
        'solver': result['solver']['status'], 'baseline': baseline, 'optimized': optimized,
        'saving_pct': round(100 * (1 - optimized[cost] / baseline[cost]), 2),
        'scenario_cost': scenario['scenario']['kpis'][cost],
    }), flush=True)
    print(f'{template} Monte Carlo:', json.dumps(risk_summary), flush=True)
    print(f'E2E OK {template}', flush=True)


def main():
    SCREENS.mkdir(parents=True, exist_ok=True)
    errors, external = [], []
    with local_server(), sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        print(f'Chromium {browser.version}', flush=True)
        context = browser.new_context(viewport=DESKTOP, accept_downloads=True, service_workers='block')

        def local_only(route):
            if urlsplit(route.request.url).hostname == HOST:
                route.continue_()
            else:
                external.append(route.request.url)
                route.abort()

        context.route('**/*', local_only)
        page = context.new_page()
        page.on('pageerror', lambda error: errors.append(error.stack or str(error)))

        def console(message):
            if message.type == 'error':
                errors.append(f"{message.location.get('url', '')}: {message.text}")

        page.on('console', console)
        try:
            assert page.goto(BASE).ok
            expect(page.get_by_test_id('company-switcher').locator('option')).to_have_count(len(TEMPLATES))
            expect(page.get_by_test_id('page-dashboard').get_by_text('لسه مفيش خطة للشركة')).to_be_visible()
            for cid, template in TEMPLATES:
                sector_flow(page, cid, template)
        finally:
            print(f'console_errors={len(errors)} external_requests={len(external)}', flush=True)
            for diagnostic in errors + external:
                print(diagnostic, flush=True)
            browser.close()
        assert not errors and not external, 'Browser reported errors or external requests'


if __name__ == '__main__':
    main()
