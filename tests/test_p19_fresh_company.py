"""Accept real onboarding, analysis, and Excel downloads for two fresh traders."""
from functools import partial
from io import BytesIO
import os
from pathlib import Path
from unittest.mock import patch

from openpyxl import load_workbook
from playwright.sync_api import expect, sync_playwright
import pytest

from qa.phase16_browser import capture, check_chart, download_excel, login, post_from, save_scenario, server


submit = partial(post_from, timeout=12_000)


MANUAL = {
    'structure': {'department.sales': 'مبيعات الجملة'},
    'sales': {'demand': 1000},
    'catalog': {'offering.box': 'عبوات تغليف', 'price': 25, 'unit_cost': 10},
    'operations': {'capacity': 1300},
    'inventory': {'inventory': 310},
    'suppliers': {'supplier.delay': 4},
    'workforce': {'workforce': 6},
    'finance': {'cash.opening': 20000, 'ar': 5000, 'ap': 15000, 'credit': 10000,
                'financing': 500, 'pnl.expenses': 4000},
    'risk': {'risk.exposure': 8},
}
WORKBOOK = {
    'structure': {'department.distribution': 'التوزيع'},
    'sales': {'demand': 800},
    'catalog': {'offering.filter': 'فلاتر مياه', 'price': 40, 'unit_cost': 22},
    'operations': {'capacity': 1100},
    'inventory': {'inventory': 240},
    'suppliers': {'supplier.delay': 6},
    'workforce': {'workforce': 5},
    'finance': {'cash.opening': 30000, 'ar': 8000, 'ap': 22000, 'credit': 14000,
                'financing': 300, 'pnl.expenses': 3500},
    'risk': {'risk.exposure': 12},
}


def check_records(model, sections):
    expected = {key: (value, 'estimated' if key == 'risk.exposure' else 'actual')
                for rows in sections.values() for key, value in rows.items()}
    assert {row['key']: (row['value'], row['source']) for row in model['records']} == expected
    assert model['readiness']['score'] == 100
    assert [(row['id'], row['score']) for row in model['readiness']['sections']] == [
        (section, 100) for section in sections
    ]


def commit(page, path, review, expected_version, keys):
    assert review['validation']['ok'] and review['token']
    assert review['version'] == expected_version
    assert {row['key'] for row in review['diff']} == set(keys)
    assert all(row['before'] is None for row in review['diff'])
    before = page.request.get('/api' + path)
    assert before.ok and before.json()['version'] == expected_version
    expect(page.get_by_test_id('onboarding-review')).to_be_visible()
    model = submit(page, path + '/commit', page.get_by_role('button', name='احفظ إصدار جديد'))
    assert model['version'] == expected_version + 1
    return model


def manual_entry(page, path, model):
    fields = {row['id']: row for row in model['readiness']['fields']}
    for index, (section, rows) in enumerate(MANUAL.items()):
        step = page.get_by_role('button', name=fields[section]['name'] + ' 0%', exact=True)
        expect(step).to_have_attribute('aria-current', 'step')
        labels = {field['key']: field['label'] for field in fields[section]['fields']}
        for key, value in rows.items():
            if key in labels:
                page.get_by_role('textbox', name=labels[key], exact=True).fill(str(value))
            else:
                prefix, label = fields[section]['item']
                assert key.startswith(prefix + '.')
                page.get_by_role('button', name='ضيف سجل').click()
                page.get_by_role('textbox', name='معرّف السجل').fill(key.removeprefix(prefix + '.'))
                page.get_by_role('textbox', name=label, exact=True).fill(value)
        if section == 'risk':
            page.get_by_label('مصدر القيمة').select_option('estimated')
        review = submit(page, f'{path}/sections/{section}/manual/preview',
                           page.get_by_role('button', name='راجع الإدخال'))
        model = commit(page, path, review, index + 1, rows)
        assert model['readiness']['sections'][index]['score'] == 100
        if index + 1 < len(MANUAL):
            assert model['readiness']['score'] < 100
            page.get_by_role('button', name='القسم التالي').click()
    return model


def workbook_entry(page, path, shot):
    page.get_by_role('button', name='رفع شامل').click()
    with download_excel(page, page.get_by_role('link', name='نزّل قالب الشركة')) as book:
        assert book.sheetnames == list(WORKBOOK)
        for section, rows in WORKBOOK.items():
            sheet = book[section]
            assert sheet.sheet_view.rightToLeft
            for key, value in rows.items():
                source = 'estimated' if key == 'risk.exposure' else 'actual'
                sheet.append([key, value, None, source])
        buffer = BytesIO()
        book.save(buffer)
    page.get_by_label('ملف Excel', exact=True).set_input_files({
        'name': 'trading-acceptance.xlsx',
        'mimeType': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        'buffer': buffer.getvalue(),
    })
    review = submit(page, path + '/excel/preview', page.get_by_role('button', name='راجع ملف Excel'))
    expect(page.get_by_test_id('onboarding-review')).to_contain_text('فلاتر مياه')
    expect(page.get_by_role('heading', name='جاهزية البيانات 0%')).to_be_visible()
    shot('workbook_review', page.get_by_test_id('onboarding-review'), '#onboarding-panel h1')
    return commit(page, path, review, 1, [key for rows in WORKBOOK.values() for key in rows])


def analysis_and_exports(page, model, baseline, delta, tornado_spans, unit_cost, goal_kpis, shot):
    name, model_id, version = (model[key] for key in ('name', 'model_id', 'version'))
    profit, cash = baseline
    page.get_by_role('link', name='افتح أثر الفرضية').click()
    expect(page.get_by_test_id('impact-model')).to_have_value(str(model_id))
    page.get_by_test_id('impact-driver').select_option('demand')
    page.get_by_test_id('impact-value').fill('10')
    impact = submit(page, '/enterprise/whatif', page.get_by_test_id('impact-run'))
    assert (impact['model_id'], impact['version']) == (model_id, version)
    nodes = {row['id']: row for row in impact['nodes']}
    for node_id, value in [('pnl.result', profit), ('cash', cash)]:
        row = nodes[node_id]
        assert (row['before'], row['after'], row['delta']) == pytest.approx((value, value + delta, delta))
        assert row['quantiles'] == pytest.approx(dict.fromkeys(('p10', 'p50', 'p90'), value + delta))
    assert {('demand', 'sales.volume'), ('pnl.result', 'cash')} <= {
        (edge['source'], edge['target']) for edge in impact['paths']
    }
    check_chart(page.get_by_test_id('chart-impact-cash'))
    ranking = page.get_by_test_id('impact-ranking')
    expect(ranking).to_contain_text('فعلي')
    expect(ranking).to_contain_text('مقدّر')
    shot('impact', ranking, '[data-testid="page-impact"] h1')
    shock = save_scenario(page, 'زيادة الطلب')
    page.get_by_test_id('impact-value').fill('0')
    neutral = save_scenario(page, 'خط الأساس')
    assert all((item['company_id'], item['model_id'], item['version']) ==
               (model['company_id'], model_id, version) for item in (neutral, shock))

    page.get_by_role('link', name='مقارنة السيناريوهات', exact=True).click()
    expect(page.get_by_test_id('scenario-scope')).to_have_value(f'{model_id}:{version}')
    for scenario in (neutral, shock):
        page.get_by_role('checkbox', name=scenario['name']).check()
    comparison = submit(page, '/enterprise/scenarios/compare', page.get_by_test_id('compare-scenarios'))
    assert (comparison['settings']['n'], comparison['settings']['seed']) == (200, 42)
    assert [row['kpis']['profit'] for row in comparison['scenarios']] == pytest.approx([profit, profit + delta])
    with download_excel(page, page.get_by_role('button', name='نزّل المقارنة Excel')) as book:
        assert all(sheet.sheet_view.rightToLeft for sheet in book)
        assert book['الإعدادات']['A2'].value == name and book['الإعدادات']['B2'].value == version
        rows = list(book['المقارنة'].values)
        for line, expected in [(1, profit), (2, cash)]:
            assert [rows[line][rows[0].index(item['name'])] for item in (neutral, shock)] == pytest.approx(
                [expected, expected + delta]
            )

    scenarios_page = page.get_by_test_id('page-scenarios')
    for driver in ('demand', 'unit_cost'):
        scenarios_page.locator(f'input[type="checkbox"][value="{driver}"]').check()
    page.get_by_test_id('tornado-percent').fill('10')
    tornado = submit(page, '/enterprise/scenarios/tornado', page.get_by_test_id('run-tornado'))
    assert (tornado['model_id'], tornado['version'], tornado['baseline'], tornado['percent']) == (
        model_id, version, profit, 10
    )
    for row, (driver, span) in zip(tornado['rows'], tornado_spans, strict=True):
        assert row['driver']['id'] == driver and row['span'] == pytest.approx(span)
        direction = 1 if driver == 'demand' else -1
        assert (row['low']['delta'], row['high']['delta']) == pytest.approx(
            (-direction * span / 2, direction * span / 2)
        )
    chart = page.get_by_test_id('chart-tornado')
    check_chart(chart)
    shot('tornado', chart.locator('..').locator('..'), '[data-testid="page-scenarios"] h1')
    with download_excel(page, page.get_by_role('button', name='نزّل الحساسية Excel')) as book:
        assert all(sheet.sheet_view.rightToLeft for sheet in book)
        assert book['الإعدادات']['A2'].value == name and book['الإعدادات']['B2'].value == version
        assert book['الإعدادات']['E2'].value == profit
        for result, row in zip(tornado['rows'], list(book['الحساسية'].values)[1:], strict=True):
            assert row[1] == result['driver']['id'] and row[7] == pytest.approx(result['span'])

    page.get_by_role('link', name='القرارات والمتابعة', exact=True).click()
    expect(page.get_by_test_id('p18-model')).to_have_value(str(model_id))
    page.get_by_label('المدخل القابل للتحكم').select_option('unit_cost')
    page.get_by_test_id('p18-profit').fill('10')
    page.get_by_test_id('p18-cash').fill(str(cash + 500))
    page.get_by_test_id('p18-min-0').fill(str(unit_cost[0]))
    page.get_by_test_id('p18-max-0').fill(str(unit_cost[1]))
    result = submit(page, f'/enterprise/models/{model_id}/prescriptive/goal-seek', page.get_by_test_id('p18-search'))
    assert result['version'] == version and not result['baseline_meets_goal']
    assert result['baseline']['profit'] == profit and result['baseline']['cash_low'] == cash
    best = result['recommendations'][0]
    assert best['actions'] == {'unit_cost': goal_kpis[0]}
    assert (best['kpis']['profit'], best['kpis']['cash_low']) == pytest.approx(goal_kpis[1:])
    assert best['kpis']['profit'] >= profit * 1.10 and best['kpis']['cash_low'] >= cash + 500
    recommendations = page.get_by_test_id('p18-recommendations')
    expect(recommendations).to_contain_text('نتيجة البحث')
    shot('goal', recommendations, '[data-testid="page-prescriptive"] h1')


@pytest.mark.parametrize(('mode', 'width', 'name', 'baseline', 'delta', 'spans', 'cost', 'goal'), [
    ('manual', 1440, 'مدار للتغليف', (10500, 20500), 1500,
     [('demand', 3000), ('unit_cost', 2000)], (8, 10), (8, 12500, 22500)),
    ('workbook', 390, 'رِواق لتوزيع الفلاتر', (10600, 26600), 1440,
     [('unit_cost', 3520), ('demand', 2880)], (19, 22), (20.5, 11800, 27800)),
])
def test_fresh_trading_company(mode, width, name, baseline, delta, spans, cost, goal):
    unexpected = []
    with (
        patch('app.llm.codex_cli.find_codex', side_effect=AssertionError('Unexpected Codex call')) as codex,
        patch('app.forecasting.foundation.TimesFMWorker.forecast',
              side_effect=AssertionError('Unexpected TimesFM call')) as timesfm,
        server() as base,
        sync_playwright() as playwright,
    ):
        browser = playwright.chromium.launch()
        page = browser.new_page(base_url=base, viewport={'width': width, 'height': 900})
        page.set_default_timeout(12_000)
        page.on('pageerror', lambda error: unexpected.append(str(error)))
        page.on('console', lambda event: unexpected.append(event.text) if event.type == 'error' else None)
        page.on('response', lambda response: unexpected.append(f'{response.status} {response.url}')
                if response.status >= 400 else None)

        def local_only(route):
            if route.request.url.startswith(base + '/'):
                route.continue_()
            else:
                unexpected.append('External request: ' + route.request.url)
                route.abort()

        def shot(stage, element, heading):
            capture(page, stage, heading, None)
            if directory := os.environ.get('ATHAR_P19_SCREENS'):
                destination = Path(directory)
                destination.mkdir(parents=True, exist_ok=True)
                element.screenshot(path=destination / f'p19_{stage}_{width}.png', animations='disabled')

        page.route('**/*', local_only)
        page.goto('/')
        login(page, 'qa-owner')
        page.get_by_role('link', name='تجهيز الشركة', exact=True).click()
        page.get_by_label('اسم الشركة').fill(name)
        page.get_by_label('قالب القطاع').select_option('trading')
        model = submit(page, '/onboarding/companies', page.get_by_role('button', name='أنشئ الشركة'))
        path = f'/companies/{model["company_id"]}/onboarding'
        assert (model['name'], model['sector'], model['version'], model['records']) == (name, 'trading', 1, [])
        assert model['readiness']['score'] == 0 and all(row['score'] == 0 for row in model['readiness']['sections'])
        expect(page.get_by_test_id('company-switcher')).to_have_value(str(model['company_id']))
        expect(page.get_by_role('heading', name='جاهزية البيانات 0%')).to_be_visible()

        if mode == 'manual':
            model = manual_entry(page, path, model)
            check_records(model, MANUAL)
        else:
            model = workbook_entry(page, path, shot)
            check_records(model, WORKBOOK)
        page.reload()
        page.get_by_test_id('company-switcher').select_option(str(model['company_id']))
        expect(page.get_by_role('heading', name=name, exact=True)).to_be_visible()
        expect(page.get_by_role('heading', name='جاهزية البيانات 100%')).to_be_visible()
        shot('readiness', page.locator('.onboarding-readiness'), '#onboarding-panel h1')
        analysis_and_exports(page, model, baseline, delta, spans, cost, goal, shot)
        assert not unexpected, unexpected
        codex.assert_not_called()
        timesfm.assert_not_called()
        browser.close()
