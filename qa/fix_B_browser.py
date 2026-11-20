"""Capture fix B against the local app using a temporary demo database."""
import argparse
import json
from pathlib import Path
from unittest.mock import patch

from playwright.sync_api import expect, sync_playwright

from app.config import Settings
from qa.phase16_browser import capture, click_workspace_link, login, post_from, server
from qa.phase17_browser import upload_template


def settings(**options):
    return Settings(**(options | {'llm_provider': 'rules', 'forecast_engine': 'stat'}))


def frame(chart, path):
    chart.screenshot(path=path)
    return chart.evaluate('''canvas => {
        const chart = Chart.getChart(canvas);
        const index = chart.data.labels.indexOf('النتيجة');
        const bar = chart.getDatasetMeta(1).data[index];
        return { ms: performance.now(), x: bar.x, targetX: bar.getProps(['x'], true).x,
                 renderedValue: chart.scales.x.getValueForPixel(bar.x),
                 targetValue: chart.data.datasets[1].data[index] };
    }''')


def motion(page, files, size):
    click_workspace_link(page, 'أثر الفرضية')
    driver = page.get_by_test_id('impact-driver')
    driver.select_option('demand')
    page.get_by_test_id('impact-value').fill('-20')
    run = page.get_by_test_id('impact-run')
    post_from(page, '/enterprise/whatif', run)
    chart = page.get_by_test_id('chart-impact-pnl')
    expect(chart).to_be_visible()
    chart.scroll_into_view_if_needed()
    page.wait_for_function('''() => {
        const chart = Chart.getChart(document.querySelector('[data-testid=chart-impact-pnl]'));
        const bar = chart?.getDatasetMeta(1).data[0];
        return bar && bar.x === bar.getProps(['x'], true).x;
    }''')
    old_id = chart.evaluate('el => Chart.getChart(el).id')
    old_value = chart.evaluate("el => Chart.getChart(el).data.datasets[1].data.at(-1)")
    page.get_by_test_id('impact-value').fill('-60')

    # Virtual time moves animation and leaves the rendered frame fixed for capture.
    page.clock.install()
    page.clock.pause_at(page.evaluate('Date.now()') + 100)
    result = post_from(page, '/enterprise/whatif', run)
    page.clock.run_for(85)
    first = frame(chart, files / f'chart-{size}-t0.png')
    page.clock.run_for(300)
    second = frame(chart, files / f'chart-{size}-t300.png')
    assert chart.evaluate('el => Chart.getChart(el).id') == old_id
    assert second['ms'] - first['ms'] == 300
    assert abs(second['x'] - second['targetX']) < abs(first['x'] - first['targetX'])
    assert first['x'] != second['x']
    profit = next(row['after'] for row in result['nodes'] if row['id'] == 'pnl.result')
    assert first['targetValue'] == second['targetValue'] == profit == 490
    assert 490 < second['renderedValue'] < first['renderedValue'] < old_value == 1090
    page.clock.resume()
    page.wait_for_function('''() => {
        const chart = Chart.getChart(document.querySelector('[data-testid=chart-impact-pnl]'));
        const bar = chart.getDatasetMeta(1).data[chart.data.labels.indexOf('النتيجة')];
        return bar.x === bar.getProps(['x'], true).x;
    }''')
    page.emulate_media(reduced_motion='reduce')
    page.get_by_test_id('impact-value').fill('-40')
    post_from(page, '/enterprise/whatif', run)
    page.wait_for_function('''() => {
        const chart = Chart.getChart(document.querySelector('[data-testid=chart-impact-pnl]'));
        return chart && chart.options.animation === false && chart.data.datasets[1].data.includes(790);
    }''')
    started = first['ms']
    for sample in (first, second):
        sample['ms'] -= started
    return {'initial_value': old_value, 'samples': [first, second],
            'reduced_motion': '790 SAR applied with Chart.js animation disabled'}


def ready_company(page, files, size):
    click_workspace_link(page, 'تجهيز الشركة')
    page.get_by_label('اسم الشركة').fill('شركة توزيع اختبار أثر')
    page.get_by_label('قالب القطاع').select_option('trading')
    page.get_by_role('button', name='أنشئ الشركة').click()
    expect(page.get_by_role('heading', name='جاهزية البيانات 0%')).to_be_visible()
    click_workspace_link(page, 'خريطة الشركة')
    expect(page.get_by_role('link', name='ابدأ بتجهيز البيانات')).to_be_visible()
    click_workspace_link(page, 'تجهيز الشركة')
    page.get_by_role('button', name='رفع شامل').click()
    upload_template(page)
    page.get_by_role('button', name='احفظ إصدار جديد').click()
    expect(page.get_by_role('heading', name='جاهزية البيانات 100%')).to_be_visible()
    click_workspace_link(page, 'خريطة الشركة')
    ribbon = page.get_by_test_id('map-kpis')
    expect(ribbon.locator('article').first).to_contain_text('1,390')
    expect(ribbon.locator('article').nth(1)).to_contain_text('1,590')
    page.wait_for_function("getComputedStyle(document.querySelector('.lm-input')).opacity === '1'")
    page.evaluate('window.scrollTo(0, 0)')
    page.screenshot(path=files / f'after-ready-{size}.png', animations='disabled')

    page.get_by_label('مدخل للتعديل').select_option('demand')
    page.get_by_role('button', name='ضيف', exact=True).click()
    page.get_by_test_id('map-assumption-value').fill('80')
    assumption = page.locator('.lm-edit')
    assumption.locator('summary').click()
    assumption.locator('input[x-model="row.uncertainty"]').fill('10')
    response = post_from(page, '/enterprise/whatif', page.get_by_test_id('map-run'))
    profit = next(row for row in response['nodes'] if row['id'] == 'pnl.result')
    assert profit['quantiles']['p10'] < profit['quantiles']['p50'] < profit['quantiles']['p90']
    stage = page.get_by_test_id('map-stage')
    expect(stage).to_have_attribute('data-ripple', 'complete', timeout=8000)
    expect(page.get_by_test_id('map-impact-kpis').get_by_text('P10 · متشائم').first).to_be_visible()
    expect(ribbon.locator('article').first).to_have_attribute('data-tone', 'critical')
    if size == '1440x900':
        page.locator('.lm-output').screenshot(path=files / 'map-percentiles-1440x900.png')
        week = page.get_by_role('slider', name='أسبوع وصول الأثر')
        week.fill('0')
        expect(stage.locator('[data-department="results"]')).to_have_attribute('data-tone', 'idle')
        page.get_by_role('button', name='شغّل الأثر').click()
        expect(stage).to_have_attribute('data-ripple', 'running')
        expect(stage).to_have_attribute('data-ripple', 'complete', timeout=8000)
        expect(stage.locator('[data-department="results"]')).to_have_attribute('data-tone', 'critical')
        stage.screenshot(path=files / 'map-impact-1440x900.png')
        page.emulate_media(reduced_motion='reduce')
        week.fill('0')
        page.get_by_role('button', name='شغّل الأثر').click()
        expect(stage).to_have_attribute('data-ripple', 'complete')
        expect(week).to_have_value(week.get_attribute('max'))
        page.emulate_media(reduced_motion='no-preference')
        goal = page.locator('.lm-output')
        goal.get_by_text('توصيات لهدف الربح', exact=True).click()
        goal.get_by_label('المدخل القابل للتحكم').select_option('price')
        goal.get_by_label('إلى', exact=True).fill('30')
        goal.get_by_label('زيادة الربح المطلوبة (%)').fill('5')
        goal.get_by_label('الحد الأدنى المقبول للسيولة (ر.س)').fill('1500')
        advice = post_from(page, '/enterprise/map/goal-seek', goal.get_by_role('button', name='دور على توصيات'))
        assert advice['evaluated_trials'] == 4 and advice['recommendations'][0]['target_met']
        recommendations = page.get_by_test_id('map-recommendations')
        expect(recommendations).to_contain_text('مرونة افتراضية للقطاع')
        expect(page.locator('.executive-strip .executive-actions')).to_contain_text('البديل الأعلى ترتيبًا')
        recommendations.screenshot(path=files / 'map-recommendations-1440x900.png')


def exercise(browser, origin, files, width, height):
    size = f'{width}x{height}'
    page = browser.new_page(base_url=origin, viewport={'width': width, 'height': height},
                            locale='ar-SA', has_touch=width == 390)
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.on('console', lambda event: errors.append(event.text) if event.type == 'error' else None)
    page.route('**/*', lambda route: route.continue_() if route.request.url.startswith(origin + '/') else route.abort())
    page.goto('/#/map')
    login(page, 'qa-owner')
    with page.expect_response(lambda response: '/api/enterprise/map/baseline?' in response.url,
                              timeout=90_000) as baseline:
        page.get_by_role('button', name='افتح ديمو روافد').click()
    assert baseline.value.ok, baseline.value.text()
    executive = page.get_by_test_id('map-kpis')
    expect(executive.locator('article')).to_have_count(4)
    profit = page.evaluate('value => number(value)', baseline.value.json()['kpis']['profit'])
    expect(executive.locator('.lm-ribbon-value bdi').first).to_have_text(profit)
    capture(page, size, '.lm-identity h1', None)
    assert executive.locator('article').first.bounding_box()['y'] < height
    page.screenshot(path=files / f'after-{size}.png', animations='disabled')

    if width == 390:
        toggle = page.get_by_role('button', name='القائمة')
        expect(toggle).to_have_attribute('aria-expanded', 'false')
        toggle.focus()
        page.keyboard.press('Enter')
        expect(toggle).to_have_attribute('aria-expanded', 'true')
        assert page.evaluate('document.documentElement.scrollWidth') == 390
        page.screenshot(path=files / f'mobile-menu-{size}.png')
        page.keyboard.press('Tab')
        expect(page.locator('#workspace-nav a:focus')).to_have_count(1)
        page.keyboard.press('Escape')
        expect(toggle).to_be_focused()
        expect(toggle).to_have_attribute('aria-expanded', 'false')
        toggle.tap()
        expect(toggle).to_have_attribute('aria-expanded', 'true')
        toggle.tap()
        expect(toggle).to_have_attribute('aria-expanded', 'false')
        page.get_by_test_id('theme-toggle').click()
        expect(page.locator('html')).to_have_attribute('data-theme', 'dark')
        assert page.evaluate('document.documentElement.scrollWidth') == 390
        page.wait_for_timeout(800)
        page.screenshot(path=files / f'after-dark-{size}.png', animations='disabled')
        page.get_by_test_id('theme-toggle').click()

    ready_company(page, files, size)
    evidence = motion(page, files, size)
    assert page.evaluate('document.documentElement.scrollWidth') == width
    assert not errors, errors
    page.close()
    return evidence


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('screens', type=Path)
    output = parser.parse_args().screens
    output.mkdir(parents=True, exist_ok=True)
    with patch('qa.phase16_browser.Settings', side_effect=settings), server() as origin, sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        evidence = {f'{width}x{height}': exercise(browser, origin, output, width, height)
                    for width, height in [(1440, 900), (390, 844)]}
        (output / 'motion-evidence.json').write_text(json.dumps(evidence, indent=2) + '\n')
        browser.close()


if __name__ == '__main__':
    main()
