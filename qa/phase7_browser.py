"""Phase 7 screenshot and frontend interaction checks against a disposable local database."""
import argparse
from io import BytesIO
from pathlib import Path
from urllib.parse import urlsplit

from openpyxl import load_workbook
from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
SCREEN_DIR = ROOT / 'qa' / 'screens'
PAGES = ('dashboard', 'data', 'model', 'forecast', 'optimize', 'whatif', 'chat', 'approvals', 'actuals')
SECTORS = [('1', 'contracting'), ('2', 'production')]
EXPECTED_MISSING = (
    '/runs/latest', '/forecast/latest', '/actuals/latest',
)


def enter(page, key):
    page.locator(f'nav a[href="#/{key}"]').click()
    section = page.get_by_test_id('page-' + key)
    expect(section).to_be_visible()
    expect(section.get_by_text('جارٍ تحميل الصفحة…', exact=True)).to_be_hidden()
    return section


def chart_ready(page, key):
    canvas = page.get_by_test_id('chart-' + key)
    expect(canvas).to_be_visible()
    page.wait_for_function('id => { const chart = Chart.getChart(document.querySelector("[data-testid=chart-" + id + "]"));'
                           'return chart?.chartArea?.width > 100 && chart.data.datasets.some(x => x.data.length); }', arg=key)


def shot(page, key, sector):
    page.evaluate('document.fonts.ready')
    clips = page.evaluate('''() => {
        const visible = el => el.getClientRects().length;
        const overflows = [...document.querySelectorAll('main section button, main section .card, main section h1')]
            .filter(el => visible(el) && el.scrollWidth > el.clientWidth + 2);
        return { page: document.documentElement.scrollWidth - innerWidth, elements: overflows.map(x => x.innerText.slice(0, 50)) };
    }''')
    assert clips['page'] <= 1 and not clips['elements'], (key, sector, clips)
    assert page.evaluate('''() => {
        const section = [...document.querySelectorAll('main section')].find(el => el.offsetHeight);
        const title = getComputedStyle(section.querySelector('h1'));
        return document.documentElement.dir === 'rtl' &&
            document.fonts.check('700 16px "Thmanyah Serif Text"', 'البيانات') &&
            document.fonts.check('900 48px "Thmanyah Serif Display"', 'لوحة التحكم') &&
            title.fontFamily === '"Thmanyah Serif Display"';
    }''')
    path = SCREEN_DIR / f'p7_{key}_{sector}.png'
    page.screenshot(path=path, full_page=True, animations='disabled')
    print(f'1440px {sector}/{key} → {path.relative_to(ROOT)}', flush=True)


def request_after(page, suffix, click):
    with page.expect_response(lambda response: urlsplit(response.url).path.endswith(suffix), timeout=60000) as response:
        click()
    assert response.value.ok, (suffix, response.value.status, response.value.text())
    return response.value.json()


def sector_flow(page, cid, sector):
    api = f'/companies/{cid}'
    page.get_by_test_id('company-switcher').select_option(cid)
    data = enter(page, 'data')
    request_after(page, api + '/datasets/demo', page.get_by_test_id('load-demo').click)
    expect(data.get_by_test_id('active-dataset')).to_contain_text('البيانات التجريبية')
    expect(data.get_by_role('status').filter(has_text='تم تحميل البيانات التجريبية')).to_be_visible()
    shot(page, 'data', sector)

    model = enter(page, 'model')
    credit = model.get_by_role('spinbutton', name='حد الائتمان', exact=False)
    expect(credit).to_be_visible()
    credit.fill('4000' if sector == 'contracting' else '150000')
    request_after(page, api + '/model', model.get_by_role('button', name='حفظ إصدار جديد').click)
    expect(model.get_by_text('الإصدار 2', exact=True)).to_be_visible()
    shot(page, 'model', sector)

    enter(page, 'forecast')
    forecast = request_after(page, api + '/forecast', page.get_by_test_id('run-forecast').click)
    chart_ready(page, 'forecast')
    if sector == 'production':
        page.get_by_test_id('forecast-series').select_option('S2')
        expected = next(item['p50'][-1] for item in forecast['series'] if item['key'] == 'S2')
        page.wait_for_function(
            """value => Chart.getChart(document.querySelector('[data-testid=chart-forecast]')).data.datasets[3].data.at(-1) === value""",
            arg=expected,
        )
    shot(page, 'forecast', sector)

    optimize = enter(page, 'optimize')
    result = request_after(page, api + '/optimize', page.get_by_test_id('run-optimize').click)
    chart_ready(page, 'compare')
    draft = request_after(page, api + '/plans', page.get_by_test_id('draft-optimized').click)
    expect(optimize.get_by_role('status').filter(has_text='تم حفظ مسودة')).to_be_visible()
    shot(page, 'optimize', sector)
    key = 'total' if sector == 'contracting' else 'total_cost'
    print(f"{sector}: {result['solver']['status']} baseline={result['baseline']['kpis'][key]} "
          f"optimized={result['optimized']['kpis'][key]} wall_s={result['solver']['wall_s']:.3f}")

    enter(page, 'dashboard')
    chart_ready(page, 'gantt' if sector == 'contracting' else 'plan')
    chart_ready(page, 'cash')
    shot(page, 'dashboard', sector)

    scenarios = enter(page, 'whatif')
    preset = 'فريق غير متاح' if sector == 'contracting' else 'خط متوقف يومين'
    scenarios.get_by_role('button', name=preset).click()
    expect(scenarios.get_by_role('spinbutton')).to_have_count(3 if sector == 'contracting' else 2)
    scenarios.get_by_role('button', name='حد الائتمان −10%').click()
    request_after(page, api + '/whatif', page.get_by_test_id('run-whatif').click)
    request_after(page, api + '/montecarlo', page.get_by_test_id('run-mc').click)
    chart_ready(page, 'mc')
    request_after(page, api + '/plans', page.get_by_test_id('draft-scenario').click)
    expect(scenarios.get_by_role('status').filter(has_text='تم حفظ مسودة')).to_be_visible()
    shot(page, 'whatif', sector)

    chat = enter(page, 'chat')
    question = 'لو الحد الائتماني قل 10% إيه اللي هيحصل؟'
    chat.get_by_role('button', name=question).click()
    reply = request_after(page, api + '/chat', page.get_by_test_id('chat-send').click)
    assert reply['action'] == 'whatif' and reply['provider'] == 'rules'
    expect(chat.get_by_role('link', name='عرض نتيجة السيناريو')).to_be_visible()
    shot(page, 'chat', sector)

    approvals = enter(page, 'approvals')
    row = approvals.get_by_role('row').filter(has_text=f"خطة {draft['id']}")
    row.get_by_role('button', name='راجع المسودة').click()
    approvals.get_by_role('textbox', name='المراجع').fill('مراجع التخطيط')
    approvals.get_by_role('textbox', name='تعليق').fill('تمت مراجعة المؤشرات ومطابقة الخطة للبيانات.')
    request_after(page, f"/plans/{draft['id']}/decide", page.get_by_test_id('save-decision').click)
    expect(row.get_by_text('معتمدة', exact=True)).to_be_visible()
    with page.expect_download() as file:
        row.get_by_role('button', name='نزّل Excel', exact=False).click()
    workbook = load_workbook(BytesIO(file.value.path().read_bytes()), read_only=True)
    assert 'KPIs' in workbook.sheetnames
    workbook.close()
    expect(approvals.get_by_text('تصدير خطة · XLSX')).to_be_visible()
    shot(page, 'approvals', sector)

    actuals = enter(page, 'actuals')
    actuals.get_by_label('الخطة المعتمدة').select_option(str(draft['id']))
    page.get_by_test_id('actuals-file').set_input_files(ROOT / 'static' / 'templates' / f'actuals_{sector}_template.csv')
    request_after(page, api + '/actuals/upload', page.get_by_test_id('upload-actuals').click)
    chart_ready(page, 'pva')
    calibration = request_after(page, api + '/recalibrate', page.get_by_test_id('recalibrate').click)
    assert calibration['calibrations']
    expect(actuals.get_by_role('status').filter(has_text=calibration['msg_ar'])).to_be_visible()
    shot(page, 'actuals', sector)

    page.set_viewport_size({'width': 390, 'height': 844})
    for key in PAGES:
        enter(page, key)
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1'), (sector, key)
        expect(page.get_by_test_id('page-' + key).locator('h1')).to_be_visible()
    print(f'390px {sector}: all 9 pages fit the viewport', flush=True)
    page.set_viewport_size({'width': 1440, 'height': 900})


def main(base):
    SCREEN_DIR.mkdir(exist_ok=True)
    errors, external, expected_missing = [], [], []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        context = browser.new_context(viewport={'width': 1440, 'height': 900}, accept_downloads=True)

        def local_only(route):
            if urlsplit(route.request.url).netloc == urlsplit(base).netloc:
                route.continue_()
            else:
                external.append(route.request.url)
                route.abort()

        context.route('**/*', local_only)
        page = context.new_page()
        page.on('pageerror', lambda error: errors.append(error.stack or str(error)))

        def console(message):
            if message.type != 'error':
                return
            path = urlsplit(message.location.get('url', '')).path
            if '404' in message.text and path.endswith(EXPECTED_MISSING):
                expected_missing.append(path)
            else:
                errors.append(f'{path}: {message.text}')

        page.on('console', console)
        assert page.goto(base).ok
        expect(page.get_by_test_id('company-switcher').locator('option')).to_have_count(2)
        expect(page.get_by_test_id('page-dashboard').get_by_text('لسه مفيش خطة للشركة')).to_be_visible()
        for cid, sector in SECTORS:
            sector_flow(page, cid, sector)
        page.get_by_test_id('theme-toggle').click()
        expect(page.locator('html')).to_have_attribute('data-theme', 'dark')
        enter(page, 'dashboard')
        chart_ready(page, 'plan')
        page.reload()
        expect(page.locator('html')).to_have_attribute('data-theme', 'dark')
        chart_ready(page, 'gantt')
        page.get_by_test_id('theme-toggle').click()
        expect(page.locator('html')).to_have_attribute('data-theme', 'light')
        print(f'Unexpected console/page errors={len(errors)}; expected empty-result 404s={len(expected_missing)}; external requests={len(external)}')
        assert not errors and not external, (errors, external)
        browser.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base', default='http://127.0.0.1:8780')
    main(parser.parse_args().base)
