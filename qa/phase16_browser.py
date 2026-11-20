"""Phase 16: real Chromium and HTTP, with optional live Codex and TimesFM."""
import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import socket
import subprocess
from tempfile import TemporaryDirectory
from threading import Thread
from time import monotonic, sleep
from unittest.mock import patch

from openpyxl import load_workbook
from playwright.sync_api import expect, sync_playwright
import uvicorn
from ortools.sat.python import cp_model

from app.config import Settings
from app.forecasting.foundation import TimesFMWorker
from app.llm import codex_cli
from app.llm.enterprise import EXAMPLE
from app.main import create_app

PASSWORD = 'phase16-browser-fixture-password'
REVIEWER = 'مراجع الأثر'
BRAND = 'أثر | Athar'
TAGLINE = 'كل قرار.. وأثره على شركتك كلها'
ASSUMPTIONS = [
    {'node_id': 'credit', 'operation': 'pct', 'value': -15, 'uncertainty': 0},
    {'node_id': 'supplier', 'operation': 'add', 'value': 14, 'uncertainty': 0},
]


def fake_timesfm(self, inputs, horizon):
    return [[[values[-1] * factor] * horizon for factor in (0.9, 1, 1.1)] for values in inputs]


def fake_codex(command, *, cwd, env, input, timeout):
    payload = json.loads(input.split('\nInput: ', 1)[1])
    if 'message' in payload:
        assert payload['message'] == EXAMPLE
        assert {'credit', 'supplier'} <= {driver['id'] for driver in payload['drivers']}
        answer = {'intent': 'what_if', 'assumptions': ASSUMPTIONS}
    else:
        answer = {'reply_ar': payload['grounded_ar']}
    transcript = [
        {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': json.dumps(answer)}},
        {'type': 'turn.completed'},
    ]
    return subprocess.CompletedProcess(command, 0, '\n'.join(map(json.dumps, transcript)), '')


@contextmanager
def server():
    with TemporaryDirectory(prefix='athar-p16-') as directory, socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        url = f'http://127.0.0.1:{listener.getsockname()[1]}'
        with patch.dict(os.environ, {'ATHAR_ADMIN_USERNAME': 'qa-owner',
                                     'ATHAR_ADMIN_PASSWORD': PASSWORD, 'ATHAR_SECURE_COOKIE': '0'}):
            application = create_app(Settings(db_path=Path(directory) / 'qa.sqlite',
                                              llm_provider='codex', forecast_engine='timesfm',
                                              solver_time_limit=5))
            runner = uvicorn.Server(uvicorn.Config(application, log_level='warning', ws='none'))
            thread = Thread(target=runner.run, kwargs={'sockets': [listener]}, daemon=True)
            thread.start()
            try:
                deadline = monotonic() + 15
                while not runner.started:
                    assert thread.is_alive() and monotonic() < deadline, 'QA server did not start'
                    sleep(0.05)
                yield url
            finally:
                runner.should_exit = True
                thread.join(timeout=15)
                assert not thread.is_alive(), 'QA server did not stop'


def post_from(page, path, button, timeout=225_000):
    with page.expect_response(lambda response: response.request.method == 'POST'
                              and response.url.split('?', 1)[0].endswith('/api' + path),
                              timeout=timeout) as event:
        button.click()
    assert event.value.ok, event.value.text()
    return event.value.json()


def login(page, username):
    expect(page.get_by_role('heading', name='تسجيل الدخول')).to_be_visible()
    page.get_by_label('اسم المستخدم').fill(username)
    page.get_by_label('كلمة السر').fill(PASSWORD)
    page.get_by_role('button', name='ادخل', exact=True).click()
    expect(page.get_by_test_id('company-switcher')).to_be_visible()


def click_workspace_link(page, name):
    # The heading can appear before CSS loads and Alpine initializes the menu.
    toggle = page.locator('button[aria-controls="workspace-nav"][aria-expanded]')
    toggle.wait_for(state='attached')
    if toggle.is_visible() and toggle.get_attribute('aria-expanded') == 'false':
        toggle.click()
    page.locator('#workspace-nav').get_by_role('link', name=name, exact=True).click()


def capture(page, name, heading, screens):
    expect(page.locator(heading)).to_be_visible()
    page.evaluate('document.fonts.ready')
    # Chromium reports the heading's actual fonts after it has painted.
    page.evaluate('new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))')
    assert page.locator('html').get_attribute('dir') == 'rtl'
    assert page.evaluate('document.documentElement.scrollWidth === innerWidth'), name
    client = page.context.new_cdp_session(page)
    try:
        client.send('DOM.enable')
        client.send('CSS.enable')
        root = client.send('DOM.getDocument')['root']['nodeId']
        node = client.send('DOM.querySelector', {'nodeId': root, 'selector': heading})['nodeId']
        fonts = client.send('CSS.getPlatformFontsForNode', {'nodeId': node})['fonts']
        assert any(row['isCustomFont'] and 'thmanyah' in row['familyName'].lower() for row in fonts), fonts
    finally:
        client.detach()
    if screens:
        screens.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=screens / f'p16_{name}_{page.viewport_size["width"]}.png',
                        full_page=True, animations='disabled')


def check_chart(canvas):
    expect(canvas).to_be_visible()
    canvas.evaluate("""canvas => new Promise((resolve, reject) => {
        const end = performance.now() + 5000;
        const check = () => {
            if (Chart.getChart(canvas)?.chartArea?.width > 0) resolve();
            else if (performance.now() >= end) reject(Error('Chart did not render'));
            else requestAnimationFrame(check);
        };
        check();
    })""")


@contextmanager
def download_excel(page, button):
    with page.expect_download() as event:
        button.click()
    with event.value.path().open('rb') as source:
        workbook = load_workbook(source, data_only=True)
    try:
        assert workbook.properties.title == BRAND
        assert workbook.properties.subject == TAGLINE
        yield workbook
    finally:
        workbook.close()


def save_scenario(page, name):
    page.get_by_role('button', name='سمّي السيناريو واحفظه').click()
    page.get_by_label('اسم السيناريو').fill(name)
    save = page.get_by_role('button', name='احفظ السيناريو', exact=True)
    invalid = save.evaluate("""button => Array.from(button.form.elements)
        .filter(node => node.willValidate && !node.validity.valid)
        .map(node => [node.dataset.testid, node.value, node.validationMessage])""")
    assert not invalid, invalid
    return post_from(page, '/enterprise/scenarios', save, timeout=10_000)


def exercise(base, width, screens):
    errors = []

    def local_only(route):
        if route.request.url.startswith(base + '/'):
            route.continue_()
        else:
            errors.append('External request: ' + route.request.url)
            route.abort()

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        def new_page():
            page = browser.new_page(viewport={'width': width, 'height': 900})
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.on('console', lambda event: errors.append(event.text) if event.type == 'error' else None)
            page.on('response', lambda response: errors.append(f'{response.status} {response.url}')
                    if response.status >= 400 else None)
            page.route('**/*', local_only)
            return page

        page = new_page()
        page.goto(base + '/')
        expect(page).to_have_title('تسجيل الدخول · أثر | Athar')
        expect(page.get_by_text(TAGLINE, exact=True)).to_be_visible()
        capture(page, 'login', 'h1', screens)
        login(page, 'qa-owner')

        def navigate(name, key):
            click_workspace_link(page, name)
            section = page.get_by_test_id('page-' + key)
            expect(section).to_be_visible()
            return section

        def shot(key, name=None):
            capture(page, name or key, f'[data-testid="page-{key}"] h1', screens)

        navigate('النموذج المؤسسي', 'enterprise')
        model = post_from(page, '/enterprise/models/demo', page.get_by_test_id('enterprise-save-demo'))
        assert model['company_id'] == 1 and model['version'] == 1
        expect(page.get_by_test_id('enterprise-graph')).to_be_visible()

        switcher = page.get_by_test_id('company-switcher')
        switcher.select_option('2')
        navigate('التوقعات', 'forecast')
        forecast = post_from(page, '/companies/2/forecast', page.get_by_test_id('run-forecast'))
        assert len(forecast['series']) == 6
        assert all(row['engine'] == 'timesfm' for row in forecast['series'])
        assert all(all(lo <= center <= hi for lo, center, hi in zip(row['p10'], row['p50'], row['p90'], strict=True))
                   for row in forecast['series'])
        expect(page.get_by_test_id('forecast-engine').get_by_text('TimesFM', exact=True)).to_have_count(6)
        check_chart(page.get_by_test_id('chart-forecast'))
        shot('forecast')

        switcher.select_option('1')
        chat = navigate('اسأل أثر', 'chat')
        page.get_by_test_id('chat-scope').select_option('enterprise')
        page.get_by_test_id('chat-model').select_option(str(model['id']))
        chat.get_by_role('checkbox', name='السماح بـCodex لفهم الفرضية').check()
        page.get_by_test_id('chat-input').fill(EXAMPLE)
        started = monotonic()
        reply = post_from(page, '/enterprise/chat', page.get_by_test_id('chat-send'))
        seconds = monotonic() - started
        assert reply['provider'] == 'codex', reply['provider']
        assert (reply['model_id'], reply['version']) == (model['id'], model['version'])
        assert sorted(reply['scenario']['assumptions'], key=lambda row: row['node_id']) == ASSUMPTIONS
        after = {row['node_id']: row for row in reply['result']['assumptions']}
        assert after['credit']['after'] == 3_400_000 and after['supplier']['after'] - after['supplier']['before'] == 14
        expect(page.get_by_test_id('chat-provider')).to_have_text('Codex')
        shot('chat')

        page.get_by_role('link', name='افتح خريطة الأثر').click()
        expect(page.get_by_test_id('impact-model')).to_have_value(str(model['id']))
        expect(page.get_by_test_id('impact-graph')).to_be_visible()
        for index, assumption in enumerate(reply['scenario']['assumptions']):
            expect(page.get_by_test_id('impact-driver').nth(index)).to_have_value(assumption['node_id'])
        expect(page.get_by_test_id('impact-focus-select')).to_have_value(reply['scenario']['assumptions'][0]['node_id'])
        expect(page.get_by_test_id('impact-ranking').locator('tbody tr').first).to_be_visible()
        for key in ('pnl', 'cash'):
            check_chart(page.get_by_test_id('chart-impact-' + key))
        shot('impact')
        shock = save_scenario(page, 'خفض الائتمان وتأخير المورد')
        assert shock['assumptions'] == reply['scenario']['assumptions']
        assert (shock['model_id'], shock['version']) == (model['id'], model['version'])
        page.get_by_role('button', name='حذف الفرضية 2').click()
        page.get_by_test_id('impact-driver').first.select_option('credit')
        page.get_by_test_id('impact-operation').first.select_option('pct')
        page.get_by_test_id('impact-value').first.fill('0')
        baseline = save_scenario(page, 'الحد الحالي')
        assert baseline['assumptions'][0]['value'] == 0

        navigate('مقارنة السيناريوهات', 'scenarios')
        for name in (shock['name'], baseline['name']):
            page.get_by_role('checkbox', name=name).check()
        comparison = post_from(page, '/enterprise/scenarios/compare', page.get_by_test_id('compare-scenarios'))
        assert comparison['settings']['seed'] == 42
        by_id = {row['id']: row['kpis'] for row in comparison['scenarios']}
        profit = next(row for row in reply['result']['nodes'] if row['id'] == 'pnl.result')
        assert by_id[baseline['id']] == comparison['baseline']
        assert by_id[shock['id']]['profit'] == profit['after'], (by_id[shock['id']]['profit'], profit, reply['result']['solver_status'])
        expect(page.get_by_test_id('scenario-comparison').locator('tbody tr')).to_have_count(5)
        with download_excel(page, page.get_by_role('button', name='نزّل المقارنة Excel')) as book:
            rows = list(book['المقارنة'].values)
            assert abs(rows[1][rows[0].index(shock['name'])] - profit['after']) < 1e-8
            assert book['المقارنة'].sheet_view.rightToLeft

        for driver_id in ('supplier', 'storage'):
            page.locator(f'#main [data-testid="page-scenarios"] input[value="{driver_id}"]').check()
        page.get_by_test_id('tornado-percent').fill('20')
        tornado = post_from(page, '/enterprise/scenarios/tornado', page.get_by_test_id('run-tornado'))
        spans = [row['span'] for row in tornado['rows']]
        assert len(spans) == 2 and spans[0] >= spans[1] and spans[0] > 0
        check_chart(page.get_by_test_id('chart-tornado'))
        shot('scenarios')
        with download_excel(page, page.get_by_role('button', name='نزّل الحساسية Excel')) as book:
            assert abs(book['الحساسية']['H2'].value - spans[0]) < 1e-8

        # The approval workflow consumes a sector run. Apply the same credit change to contracting.
        navigate('التحسين', 'optimize')
        post_from(page, '/companies/1/optimize', page.get_by_test_id('run-optimize'))
        whatif = navigate('ماذا لو', 'whatif')
        credit_change = next(row['value'] for row in reply['scenario']['assumptions'] if row['node_id'] == 'credit')
        whatif.get_by_label('نسبة التغيير (%)').fill(str(int(credit_change)))
        sector_run = post_from(page, '/companies/1/whatif', page.get_by_test_id('run-whatif'))
        assert sector_run['scenario']['plan']
        draft = post_from(page, '/companies/1/plans', page.get_by_test_id('draft-scenario'))
        assert draft['status'] == 'draft' and draft['run_id'] == sector_run['run_id']

        page.get_by_role('link', name='المستخدمين').click()
        page.get_by_label('اسم المستخدم').fill(REVIEWER)
        page.get_by_label('الدور').select_option('approver')
        page.get_by_role('checkbox', name='شركة البناء النموذجية').check()
        page.locator('[name="password"]').fill(PASSWORD)
        post_from(page, '/users', page.get_by_role('button', name='احفظ الحساب'))

        reviewer = new_page()
        reviewer.goto(base + '/#/approvals')
        login(reviewer, REVIEWER)
        expect(reviewer.get_by_role('button', name=f'نزّل Excel للخطة {draft["id"]}')).to_be_hidden()
        reviewer.get_by_role('button', name='راجع المسودة').click()
        reviewer.get_by_label('تعليق').fill('اتراجعت خطة المقاولات تحت فرضية خفض حد الائتمان.')
        approved = post_from(reviewer, f'/plans/{draft["id"]}/decide', reviewer.get_by_test_id('save-decision'))
        assert approved['status'] == 'approved' and approved['actor'] == REVIEWER
        assert approved['decided_by_user_id'] > 0
        expect(reviewer.get_by_text('معتمدة', exact=True)).to_be_visible()
        capture(reviewer, 'approvals', '[data-testid="page-approvals"] h1', screens)
        with download_excel(reviewer, reviewer.get_by_role('button', name=f'نزّل Excel للخطة {draft["id"]}')) as book:
            assert book.sheetnames == ['Activities', 'KPIs', 'CashFlow']
            assert book['Activities'].max_row == len(sector_run['scenario']['plan']['activities']) + 1
            assert dict(list(book['KPIs'].values)[1:])['total'] == sector_run['scenario']['kpis']['total']

        assert not errors, errors
        browser.close()
        print(f'{width}px: {len(forecast["series"])} TimesFM series; Codex {seconds:.1f}s; '
              f'profit {profit["before"]} → {profit["after"]}; '
              f'Tornado {[(row["driver"]["id"], row["span"]) for row in tornado["rows"]]}; '
              f'approved by {approved["actor"]}; 3 workbooks; 0 console, page, HTTP or external browser errors',
              flush=True)


def run(width, screens=None, *, live=False):
    if live:
        with server() as base:
            exercise(base, width, screens)
    else:
        solve = cp_model.CpSolver.solve

        def solve_for_browser_fixture(solver, *args, **kwargs):
            # Two independent runs must exhaust the same deterministic search budget
            # even when Chromium and other worktrees are testing on this machine.
            if solver.parameters.num_workers == 1:
                solver.parameters.max_time_in_seconds = max(solver.parameters.max_time_in_seconds, 60)
            return solve(solver, *args, **kwargs)

        with (patch.object(cp_model.CpSolver, 'solve', solve_for_browser_fixture),
              patch.object(TimesFMWorker, 'forecast', fake_timesfm),
              patch.object(codex_cli, 'find_codex', return_value=Path('/fixture/bin/codex')),
              patch.object(codex_cli, '_run', side_effect=fake_codex),
              server() as base):
            exercise(base, width, screens)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true', help='Use real TimesFM and logged-in Codex')
    parser.add_argument('--width', type=int, choices=(390, 1440), default=1440)
    parser.add_argument('--screens', type=Path)
    args = parser.parse_args()
    run(args.width, args.screens, live=args.live)
