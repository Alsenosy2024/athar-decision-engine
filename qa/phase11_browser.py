"""Exercise Phase 11 in Chromium with a temporary local API and SQLite DB."""
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from playwright.sync_api import expect, sync_playwright

root = Path(__file__).resolve().parents[1]
screens = root / 'qa' / 'screens'
url = 'http://127.0.0.1:18781'

with tempfile.TemporaryDirectory(prefix='decision-phase11-') as folder:
    env = os.environ | {'LLM_PROVIDER': 'rules', 'FORECAST_ENGINE': 'stat',
                        'DB_PATH': str(Path(folder) / 'qa.sqlite')}
    with (Path(folder) / 'server.log').open('w+') as log:
        server = subprocess.Popen([sys.executable, '-m', 'uvicorn', 'app.main:app',
                                   '--host', '127.0.0.1', '--port', '18781'],
                                  cwd=root, stdout=log, stderr=log, env=env)
        try:
            for _ in range(60):
                if server.poll() is not None:
                    log.seek(0)
                    raise RuntimeError(log.read())
                try:
                    if httpx.get(url + '/api/health').is_success:
                        break
                except httpx.ConnectError:
                    time.sleep(.1)
            else:
                raise RuntimeError('Server startup timed out')
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch()
                errors, external = [], []
                previous = None

                def local_only(route):
                    if urlsplit(route.request.url).netloc == '127.0.0.1:18781':
                        route.continue_()
                    else:
                        external.append(route.request.url)
                        route.abort()

                for width in (1440, 390):
                    page = browser.new_page(viewport={'width': width, 'height': 900})
                    page.on('pageerror', lambda error: errors.append(str(error)))
                    page.on('console', lambda event: errors.append(event.text) if event.type == 'error' else None)
                    page.route('**/*', local_only)
                    page.goto(url + '/#/enterprise')
                    if width == 1440:
                        page.get_by_test_id('enterprise-save-demo').click()
                        expect(page.get_by_text('اتحفظ الديمو كإصدار أول.')).to_be_visible()
                    page.get_by_role('link', name='احسب أثر فرضية').click()
                    expect(page.get_by_test_id('impact-driver').first).to_have_value('credit')
                    expect(page.get_by_test_id('impact-model')).to_have_value('1')
                    page.get_by_test_id('impact-value').first.fill('-15')
                    page.get_by_test_id('impact-uncertainty').first.fill('300000')
                    page.get_by_role('button', name='أضف فرضية').click()
                    page.get_by_test_id('impact-driver').nth(1).select_option('supplier')
                    page.get_by_test_id('impact-operation').nth(1).select_option('add')
                    page.get_by_test_id('impact-value').nth(1).fill('7')
                    with page.expect_response(lambda r: r.url.endswith('/api/enterprise/whatif') and r.request.method == 'POST',
                                              timeout=100_000) as event:
                        page.get_by_test_id('impact-run').click()
                    response = event.value
                    assert response.ok, response.text()
                    result = response.json()
                    assert result['version'] == 1 and result['simulation']['n'] == 200
                    assert set(result['optimizers_run']['scenario']) == {'production', 'contracting'}
                    assert len(result['paths']) > 15
                    if previous is not None:
                        assert result == previous, 'Identical seed and inputs produced different results'
                    previous = result
                    expect(page.get_by_test_id('impact-result')).to_be_visible()
                    page.get_by_test_id('impact-focus-select').select_option('pnl.result')
                    expect(page.get_by_test_id('impact-graph').locator('.impact-focus h3')).to_have_text('النتيجة بعد التمويل')
                    assert page.get_by_test_id('impact-graph').get_by_role('button').count() > 1
                    for key in ('pnl', 'cash'):
                        id = 'chart-impact-' + key
                        page.wait_for_function('id => { const chart = Chart.getChart(document.querySelector(`[data-testid="${id}"]`)); return chart?.chartArea?.width > 100 && chart.data.datasets.length === 2 }', arg=id)
                    assert page.get_by_test_id('chart-impact-pnl').evaluate('canvas => Chart.getChart(canvas).data.labels.length') == 5
                    page.evaluate('document.fonts.ready')
                    assert page.evaluate('document.documentElement.scrollWidth === innerWidth')
                    assert page.locator('html').get_attribute('dir') == 'rtl'
                    assert page.evaluate('getComputedStyle(document.body).fontFamily') == '"Thmanyah Serif Text"'
                    page.screenshot(path=screens / f'p11_impact_{width}.png', full_page=True, animations='disabled')
                    rows = {row['id']: row for row in result['nodes']}
                    print(width, len(result['paths']), {key: (rows[key]['before'], rows[key]['after'])
                                                        for key in ('pnl.result', 'cash', 'risk.credit')}, flush=True)
                    page.close()
                assert not errors, errors
                assert not external, external
                print('0 console/page errors, 0 external requests', flush=True)
        finally:
            server.terminate()
            server.wait(timeout=10)
