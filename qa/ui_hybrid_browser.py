"""Fit the hybrid workspace to both viewports and reconcile it with loopback HTTP."""
import argparse
import base64
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from openpyxl import Workbook
from playwright.sync_api import expect, sync_playwright

from app.config import Settings
from qa.phase16_browser import capture, click_workspace_link, login, post_from, server

COMPANY = '/companies/2'


def local_settings(**options):
    return Settings(**(options | {'llm_provider': 'rules', 'forecast_engine': 'stat'}))


def api(page, path, body=None, method='POST'):
    return page.evaluate('([path, method, body]) => api(path, {...post(body ?? undefined), method})',
                         [path, method, body])


def seed(page):
    plain = api(page, COMPANY + '/optimize', {'time_limit': 2})
    plan = api(page, COMPANY + '/plans', {'run_id': plain['run_id'], 'variant': 'optimized'})
    api(page, f'/plans/{plan["id"]}/decide', {'decision': 'approved', 'comment': 'معاينة الديمو'})
    sample = Path('static/templates/actuals_production_template.csv')
    actuals = page.evaluate('''async ([planId, content]) => {
        const form = new FormData();
        form.append('plan_id', planId);
        form.append('file', new Blob([content], {type: 'text/csv'}), 'actuals.csv');
        return api('/companies/2/actuals/upload', {method: 'POST', body: form});
    }''', [plan['id'], sample.read_text()])
    assert actuals['comparison']['rows']
    api(page, '/enterprise/models/demo?company_id=2')
    return plan


def chart_data(locator):
    return locator.evaluate('canvas => Chart.getChart(canvas).data.datasets.map(series => series.data)')


def allocation_plan(page, company, unit):
    line, sku = unit + '-L', unit + '-S'
    workbook = Workbook()
    workbook.remove(workbook.active)
    for title, headers, row in (
        ('lines', 'id name_ar changeover_cost changeover_loss_units', [line, line, 0, 0]),
        ('skus', 'id name_ar unit_cost unit_price volume holding_cost_per_day initial_inventory',
         [sku, sku, 4, 9, 1, 1, 0]),
        ('rates', 'line_id sku_id units_per_day', [line, sku, 100]),
        ('orders', 'id customer_ar sku_id qty due_day penalty_per_day is_export',
         [unit + '-O', unit, sku, 10, 2, 8, False]),
        ('demand_history', 'sku_id week qty', [sku, 1, 10]),
    ):
        sheet = workbook.create_sheet(title)
        sheet.append(headers.split())
        sheet.append(row)
    stream = BytesIO()
    workbook.save(stream)
    uploaded = page.evaluate('''async ([path, encoded]) => {
        const data = new FormData();
        data.append('file', new Blob([Uint8Array.from(atob(encoded), ch => ch.charCodeAt(0))]), 'department.xlsx');
        return api(path + '/datasets/upload', {method: 'POST', body: data});
    }''', [company, base64.b64encode(stream.getvalue()).decode()])
    assert uploaded['validation']['ok'], uploaded
    run = api(page, company + '/optimize', {'dataset_id': uploaded['dataset_id'], 'time_limit': 2})
    plan = api(page, company + '/plans', {'run_id': run['run_id'], 'variant': 'optimized'})
    approved = api(page, f'/plans/{plan["id"]}/decide', {'decision': 'approved', 'comment': 'معاينة التخصيص'})
    assert approved['eligibility']['eligible']
    return approved['id']


def check_allocation(page, shot):
    company = api(page, '/companies', {'name': 'ديمو تخصيص أقسام الإنتاج', 'template': 'production'})
    root = '/companies/' + str(company['id'])
    departments = [('packing', allocation_plan(page, root, 'packing')),
                   ('dispatch', allocation_plan(page, root, 'dispatch'))]
    page.reload()
    page.get_by_test_id('company-switcher').select_option(str(company['id']))
    click_workspace_link(page, 'مقارنة السيناريوهات')
    panel = page.get_by_test_id('hybrid-allocation')
    panel.locator('summary').click()
    for index, (department, plan) in enumerate(departments):
        panel.get_by_label('معرّف القسم').nth(index).fill(department)
        panel.get_by_label('الخطة المرجعية').nth(index).select_option(str(plan))
    panel.get_by_label('اليوم', exact=True).fill('0')
    panel.get_by_label('الحد', exact=True).fill('1000')
    result = post_from(page, root + '/decision-loop/allocation', panel.get_by_role('button', name='احسب جبهة التخصيص'))
    plot = panel.get_by_test_id('allocation-pareto-chart')
    expect(plot).to_be_visible()
    plot.evaluate('node => node.scrollIntoView({block: "center"})')
    shot('09-allocation', '[data-testid="page-scenarios"] h1')
    points = chart_data(plot)[0]
    assert len(points) == len(result['frontier']) == 1
    assert points[0]['point'] == result['frontier'][0]
    print('Shared allocation: approved departmental plans and real resource check', flush=True)


def exercise(browser, base, destination, width, height, theme, seed_data=False):
    context = browser.new_context(base_url=base, viewport={'width': width, 'height': height})
    context.add_init_script(f"localStorage.setItem('decision-theme', '{theme}')")
    page = context.new_page()
    page.set_default_timeout(20000)
    errors, external = [], []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.on('console', lambda event: errors.append(event.text) if event.type == 'error' else None)

    def local_only(route):
        if route.request.url.startswith(base + '/'):
            route.continue_()
        else:
            external.append(route.request.url)
            route.abort()

    page.route('**/*', local_only)
    page.goto('/')
    login(page, 'qa-owner')
    page.get_by_test_id('company-switcher').select_option('2')
    if seed_data:
        plan = seed(page)
    else:
        plan = next(row for row in api(page, COMPANY + '/plans', method='GET') if row['status'] == 'approved')

    suffix = ('desktop' if width == 1440 else 'mobile') + '-' + theme

    def shot(name, heading):
        capture(page, name, heading, None)
        expect(page.locator('html')).to_have_attribute('data-theme', theme)
        page.wait_for_timeout(850)
        page.screenshot(path=destination / f'{name}-{suffix}.png', animations='disabled')

    scenarios = page.get_by_test_id('page-scenarios')
    click_workspace_link(page, 'مقارنة السيناريوهات')
    expect(scenarios.get_by_test_id('hybrid-risk')).to_be_visible()
    if seed_data:
        expect(scenarios.get_by_text('مفيش تقييم مخاطر بالتوقع لمجموعة البيانات النشطة.')).to_be_visible()
        hybrid = post_from(page, COMPANY + '/optimize', scenarios.get_by_role('button', name='شغّل تقييم بالتوقع'))
        plain = api(page, COMPANY + '/optimize', {'time_limit': 2})
        api(page, COMPANY + '/montecarlo', {'run_id': plain['run_id'], 'n': 100, 'seed': 42})
        assert api(page, '/ui' + COMPANY + '/hybrid-run', method='GET')['id'] == hybrid['run_id']
    cost = page.get_by_test_id('candidate-cost-chart')
    scatter = page.get_by_test_id('production-pareto-chart')
    expect(cost).to_be_visible()
    expect(scatter).to_be_visible()
    page.wait_for_function('''() => ['candidate-cost-chart', 'production-pareto-chart'].every(id =>
        Chart.getChart(document.querySelector('[data-testid="' + id + '"]'))?.width > 0)''')
    saved = api(page, '/ui' + COMPANY + '/hybrid-run', method='GET')
    risk = saved['risk']
    assert chart_data(cost) == [[r['p50_cost_sar'] for r in risk['candidates']],
                                [r['cvar_cost_sar'] for r in risk['candidates']]]
    assert [(p['x'], p['y']) for p in chart_data(scatter)[0]] == [
        (row['objectives']['cost_sar'], row['objectives']['service_loss_pct']) for row in risk['frontier']]
    benchmark = post_from(page, COMPANY + '/ai/decision-benchmark',
                          scenarios.get_by_role('button', name='شغّل البنشمارك'))
    pair = next(row for row in benchmark['comparisons']
                if row['reference'] == 'current' and row['challenger'] == 'scenario')
    ci = page.get_by_test_id('regret-ci')
    expect(ci).to_be_visible()
    assert ci.locator('bdi').all_text_contents() == page.evaluate('values => values.map(signed)', pair['ci95_sar'])
    heading = '[data-testid="page-scenarios"] h1'
    page.evaluate('window.scrollTo(0, 0)')
    shot('03-scenarios', heading)
    if width == 390:
        cost.evaluate('canvas => canvas.closest("section").scrollIntoView({block:"center"})')
        shot('06-candidate-cost', heading)
        scatter.evaluate('canvas => canvas.closest("section").scrollIntoView({block:"center"})')
        shot('07-pareto', heading)
    page.get_by_test_id('regret-card').evaluate('card => card.scrollIntoView({block:"center"})')
    shot('08-regret', heading)

    click_workspace_link(page, 'لوحة التحكم')
    home = page.get_by_test_id('page-dashboard')
    expect(home.locator('.now-fan canvas')).to_be_visible()
    expect(home.locator('.now-histogram canvas')).to_be_visible()
    page.evaluate('window.scrollTo(0, 0)')
    shot('01-home', '[data-testid="page-dashboard"] h1')

    click_workspace_link(page, 'القرارات والمتابعة')
    decisions = page.get_by_test_id('page-prescriptive')
    cards = decisions.locator('.now-decision')
    plan_card = cards.filter(has_text=f'خطة التشغيل {plan["id"]}')
    expect(plan_card.get_by_test_id('decision-eligibility')).to_have_attribute('data-eligible', 'true')
    decision_data = api(page, '/ui' + COMPANY + '/now', method='GET')['decisions']
    assert next(row for row in decision_data if row['id'] == f'sector-{plan["id"]}')['eligibility']['eligible']
    plan_card.get_by_role('button', name='اشرح').click()
    expect(decisions.get_by_role('dialog')).to_contain_text('مؤهلة للاعتماد')
    decisions.get_by_role('button', name='اقفل').click()
    page.evaluate('window.scrollTo(0, 0)')
    shot('02-decisions', '[data-testid="page-prescriptive"] h1')

    click_workspace_link(page, 'خريطة الشركة')
    stage = page.get_by_test_id('map-stage')
    expect(stage).to_be_visible()
    assert stage.bounding_box()['height'] >= .6 * height
    page.wait_for_function('typeof window.focusEntity === "function"')
    camera = stage.locator('.lm-camera')
    expect(camera).to_have_attribute('data-focused', 'false')
    stage.evaluate('stage => stage.scrollIntoView({block:"center"})')
    map_heading = '[data-testid="page-map"] .lm-identity h1'
    shot('04-map-before', map_heading)
    page.evaluate("window.focusEntity('supplier')")
    expect(camera).to_have_attribute('data-focused', 'true')
    expect(page.locator('.lm-focus-name')).to_be_visible()
    stage.evaluate('stage => stage.scrollIntoView({block:"center"})')
    shot('05-map-after-focusEntity', map_heading)
    page.get_by_role('button', name='رجوع للكل').click()
    expect(camera).to_have_attribute('data-focused', 'false')
    if seed_data:
        check_allocation(page, shot)
    assert not errors, errors
    assert not external, external
    context.close()
    print(f'{suffix}: real API charts, paired CI, eligibility, map focus; 0 browser errors', flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('destination', type=Path)
    args = parser.parse_args()
    args.destination.mkdir(parents=True, exist_ok=True)
    with patch('qa.phase16_browser.Settings', local_settings), server() as base, sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        for width, height in [(1440, 900), (390, 844)]:
            for theme in ('light', 'dark'):
                exercise(browser, base, args.destination, width, height, theme, seed_data=width == 1440 and theme == 'light')
        browser.close()


if __name__ == '__main__':
    main()
