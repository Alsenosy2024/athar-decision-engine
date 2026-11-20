"""V4 acceptance through the deployed UI on a fresh, isolated SQLite database."""
import os
from pathlib import Path
import re
from unittest.mock import patch

from playwright.sync_api import expect, sync_playwright
import pytest

from app.llm import codex_cli
from qa.phase16_browser import PASSWORD, capture, download_excel, login, post_from as await_post, server, click_workspace_link
from qa.phase17_browser import upload_template
from qa.phase26_video import record_home
from test_p21_living_map import ASSUMPTIONS, QUESTION, codex_parse
from test_p24_department_owners_browser import sign_in

COMPANY = 'شركة مدى للتوزيع'
OWNER = 'مخطط المبيعات'
DECISION = 'زيادة سعر الصندوق'


def post_from(page, path, button):
    return await_post(page, path, button, timeout=15_000)


def local_context(browser, origin, width, errors, **options):
    context = browser.new_context(
        base_url=origin, viewport={'width': width, 'height': 900}, **options,
    )

    def local_only(route):
        if route.request.url.startswith(origin + '/'):
            route.continue_()
        else:
            errors.append('External request: ' + route.request.url)
            route.abort()

    def watch(page):
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.on('console', lambda event: errors.append(event.text) if event.type == 'error' else None)

    context.route('**/*', local_only)
    context.on('page', watch)
    context.on('requestfailed', lambda request: errors.append(f'{request.failure}: {request.url}')
               if request.failure != 'net::ERR_ABORTED' else None)
    context.on('response', lambda response: errors.append(f'{response.status} {response.url.split("?")[0]}')
               if response.status >= 400 else None)
    return context


def new_company(page):
    click_workspace_link(page, 'تجهيز الشركة')
    page.get_by_label('اسم الشركة').fill(COMPANY)
    page.get_by_label('قالب القطاع').select_option('trading')
    company = post_from(page, '/onboarding/companies', page.get_by_role('button', name='أنشئ الشركة'))
    expect(page.get_by_role('heading', name='جاهزية البيانات 0%')).to_be_visible()
    page.get_by_role('button', name='رفع شامل').click()
    upload_template(page)
    review = page.get_by_test_id('onboarding-review')
    expect(review.get_by_role('row').filter(has_text='الطلب الأسبوعي')).to_contain_text('100 فعلي')
    committed = post_from(page, f'/companies/{company["company_id"]}/onboarding/commit',
                          page.get_by_role('button', name='احفظ إصدار جديد'))
    assert committed['version'] == 2
    expect(page.get_by_role('heading', name='جاهزية البيانات 100%')).to_be_visible()
    return company


def update_sales(page, company_id):
    profile = page.request.get('/api/auth/me').json()
    response = page.request.post('/api/users', headers={'X-CSRF-Token': profile['csrf_token']}, data={
        'username': OWNER, 'password': PASSWORD, 'role': 'planner', 'company_ids': [company_id],
    })
    assert response.ok, response.text()
    click_workspace_link(page, 'أصحاب البيانات')
    page.get_by_label('الشركة', exact=True).select_option(str(company_id))
    tile = page.get_by_role('article').filter(has=page.get_by_role('heading', name='المبيعات والطلب'))
    tile.get_by_label('المخطّط').select_option(str(response.json()['id']))
    tile.get_by_role('button', name='حفظ التكليف').click()
    expect(page.get_by_text('اتحدّث تكليف القسم.')).to_be_visible()

    page.get_by_role('button', name='خروج').click()
    sign_in(page, OWNER)
    expect(page.get_by_role('heading', name='بيانات أقسامي')).to_be_visible()
    expect(page.get_by_label('القسم المكلّف بيه').locator('option')).to_have_count(1)
    page.get_by_role('textbox', name='القيمة', exact=True).fill('110')
    page.get_by_role('button', name='راجع الفرق', exact=True).click()
    preview = page.get_by_role('row').filter(has_text='الطلب الأسبوعي')
    expect(preview).to_contain_text('100')
    expect(preview).to_contain_text('110')
    page.get_by_role('button', name='ابعته لاعتماد الأدمن').click()
    expect(page.get_by_role('status').filter(has_text='مستني اعتماد')).to_be_visible()

    page.get_by_role('button', name='خروج').click()
    sign_in(page, 'qa-owner')
    click_workspace_link(page, 'أصحاب البيانات')
    page.get_by_label('الشركة', exact=True).select_option(str(company_id))
    page.get_by_role('link', name='طلبات الاعتماد').click()
    pending = page.locator('details').filter(has_text='مستني اعتماد')
    expect(pending.get_by_role('row').filter(has_text='الطلب الأسبوعي')).to_contain_text('110')
    pending.get_by_label('ملاحظة المراجعة').fill('اتراجعت قراءة الطلب')
    pending.get_by_role('button', name='اعتمد واحفظ إصدار').click()
    expect(page.get_by_role('status').filter(has_text='اتحفظ التحديث في الإصدار 3')).to_be_visible()


def home(page, company):
    page.goto('/')
    page.get_by_test_id('company-switcher').select_option(str(company['company_id']))
    stage = page.get_by_test_id('map-stage')
    expect(stage).to_be_visible()
    expect(page.locator('.lm-identity')).to_contain_text(COMPANY)
    expect(page.locator('.lm-shell')).to_contain_text('إصدار النموذج 3')
    expect(stage.locator('[data-department="sales"]')).to_contain_text('110')
    expect(stage).to_have_attribute('data-road-count', re.compile(r'^[1-9]\d*$'))
    expect(page.locator('.lm-brief-status')).to_contain_text('100%')
    expect(page.get_by_test_id('map-briefing').get_by_text('قواعد محلية')).to_be_visible()
    assert stage.locator('canvas').evaluate("""canvas => {
        const pixels = canvas.getContext('2d').getImageData(0, 0, canvas.width, canvas.height).data;
        return pixels.some((value, index) => index % 4 === 3 && value > 0);
    }""")
    return stage


def themes(page, screens):
    for label, theme in [('sand', 'light'), ('night', 'dark')]:
        expect(page.locator('html')).to_have_attribute('data-theme', theme)
        capture(page, label, '.lm-identity h1', None)
        if screens:
            page.screenshot(path=screens / f'p26_{label}_{page.viewport_size["width"]}.png',
                            full_page=True, animations='disabled')
        page.get_by_test_id('theme-toggle').click()


def ask(page, stage, model_id):
    page.get_by_test_id('map-question').fill(QUESTION)
    draft = post_from(page, '/enterprise/chat', page.get_by_test_id('map-ask'))
    assert draft['provider'] == 'codex' and draft['scenario']['assumptions'] == ASSUMPTIONS
    assert draft['result'] is None
    expect(stage).to_have_attribute('data-ripple', 'idle')
    expect(page.get_by_test_id('map-assumption-value')).to_have_value('-20')
    result = post_from(page, '/enterprise/whatif', page.get_by_test_id('map-run'))
    assert (result['model_id'], result['version']) == (model_id, 3)
    by_id = {node['id']: node for node in result['nodes']}
    assert (by_id['demand']['before'], by_id['demand']['after']) == (110, 88)
    assert (by_id['pnl.result']['before'], by_id['pnl.result']['after']) == (1540, 1210)
    assert (by_id['cash']['before'], by_id['cash']['after']) == (1740, 1410)
    assert result['paths']
    expect(stage).to_have_attribute('data-ripple', 'running')
    expect(stage).to_have_attribute('data-ripple', 'complete', timeout=8000)
    expect(page.get_by_text('سيناريو محدد · هامش عدم اليقين صفر', exact=False)).to_be_visible()
    expect(page.get_by_test_id('map-chain')).to_contain_text('الأسبوع')
    week = page.get_by_role('slider', name='أسبوع وصول الأثر')
    final_week = week.get_attribute('max')
    expect(week).to_have_value(final_week)
    week.fill('0')
    expect(stage.locator('[data-department="sales"]')).to_have_attribute('data-tone', 'idle')
    page.get_by_role('button', name='شغّل الأثر').click()
    expect(stage).to_have_attribute('data-ripple', 'running')
    expect(stage).to_have_attribute('data-ripple', 'complete', timeout=8000)
    expect(week).to_have_value(final_week)


def compare(page):
    panel = page.locator('.lm-output')
    panel.get_by_text('توصيات لهدف الربح', exact=True).click()
    panel.get_by_label('المدخل القابل للتحكم').select_option('price')
    panel.get_by_label('من', exact=True).fill('25')
    panel.get_by_label('إلى', exact=True).fill('30')
    panel.get_by_label('زيادة الربح المطلوبة (%)').fill('5')
    panel.get_by_label('الحد الأدنى المقبول للسيولة (ر.س)').fill('1700')
    goal = post_from(page, '/enterprise/map/goal-seek', panel.get_by_role('button', name='دور على توصيات'))
    assert goal['recommendations'][0]['target_met'] and goal['recommendations'][0]['cash'] >= 1700
    expect(page.get_by_test_id('map-recommendations')).to_contain_text('محامي الشيطان')

    panel.get_by_text('مقارنة السيناريوهات', exact=True).click()
    label = panel.get_by_label('اسم آخر سيناريو اتحسب')
    save = panel.get_by_role('button', name='احفظ للمقارنة')
    label.fill('طلب أقل')
    first = post_from(page, '/enterprise/scenarios', save)
    panel.get_by_role('button', name='راجع الفرضيات دي').first.click()
    post_from(page, '/enterprise/whatif', page.get_by_test_id('map-run'))
    label.fill(DECISION)
    second = post_from(page, '/enterprise/scenarios', save)
    for scenario in (first, second):
        panel.get_by_label(scenario['name'], exact=True).check()
    comparison = post_from(page, '/enterprise/scenarios/compare', panel.get_by_role('button', name='قارن المختار'))
    assert {row['id'] for row in comparison['scenarios']} == {first['id'], second['id']}
    assert comparison['scenarios'][0]['kpis']['profit'] == 1210
    assert comparison['scenarios'][1]['kpis']['profit'] > 1540
    expect(page.get_by_test_id('map-comparison')).to_contain_text(DECISION)
    return comparison


def approve(page, model_id):
    click_workspace_link(page, 'القرارات والمتابعة')
    base = f'/enterprise/models/{model_id}/prescriptive'
    page.get_by_role('combobox', name='المدخل القابل للتحكم').select_option('price')
    for key, value in [('profit', '5'), ('cash', '1700'), ('min-0', '25'), ('max-0', '30')]:
        page.get_by_test_id(f'p18-{key}').fill(value)
    goal = post_from(page, base + '/goal-seek', page.get_by_test_id('p18-search'))
    assert goal['version'] == 3 and goal['recommendations']
    page.get_by_test_id('p18-draft-name').fill(DECISION)
    draft = post_from(page, base + '/decisions', page.get_by_test_id('p18-save-draft').first)
    assert draft['status'] == 'draft'
    page.get_by_test_id('p18-tab-log').click()
    item = page.get_by_test_id(f'p18-decision-{draft["id"]}')
    item.locator('summary').click()
    approved = post_from(page, base + f'/decisions/{draft["id"]}/decide',
                         item.get_by_role('button', name='اعتمد', exact=True))
    assert approved['status'] == 'approved'
    expect(item).to_contain_text('qa-owner')


def export_comparison(page, comparison):
    click_workspace_link(page, 'مقارنة السيناريوهات')
    for row in comparison['scenarios']:
        page.get_by_role('checkbox', name=row['name']).check()
    post_from(page, '/enterprise/scenarios/compare', page.get_by_test_id('compare-scenarios'))
    with download_excel(page, page.get_by_role('button', name='نزّل المقارنة Excel')) as book:
        sheet = book['المقارنة']
        assert sheet.sheet_view.rightToLeft
        rows = list(sheet.values)
        for row in comparison['scenarios']:
            assert rows[1][rows[0].index(row['name'])] == row['kpis']['profit']


def embed(browser, page, origin, width, errors, company):
    company_id = company['company_id']
    response = page.request.post(f'/api/companies/{company_id}/integration-keys', data={
        'name': 'QA ERP', 'scopes': ['map:read'],
    }, headers={'X-CSRF-Token': page.request.get('/api/auth/me').json()['csrf_token']})
    assert response.ok, response.text()
    token = page.request.post(f'/api/v1/companies/{company_id}/embed-token', data={},
                              headers={'X-Athar-API-Key': response.json()['key']})
    assert token.ok, token.text()
    context = local_context(browser, origin, width, errors)
    try:
        assert not context.cookies()
        frame = context.new_page()
        assert frame.goto(token.json()['path']).ok
        expect(frame.locator('#embed-company')).to_contain_text(COMPANY)
        expect(frame.locator('#embed-stage')).to_have_attribute('data-road-count', re.compile(r'^[1-9]\d*$'))
        expect(frame.locator('#embed-stage canvas')).to_be_visible()
        capture(frame, 'embed', 'h1', None)
    finally:
        context.close()


def record_demo(browser, page, origin, errors, company_id, destination):
    cdp = browser.new_browser_cdp_session()
    try:
        renderer = cdp.send('SystemInfo.getInfo')['gpu']['auxAttributes']['glRenderer']
        assert 'ANGLE Metal Renderer' in renderer, renderer
        print(renderer)
    finally:
        cdp.detach()
    context = local_context(browser, origin, 1440, errors, storage_state=page.context.storage_state(),
                            record_video_dir=destination.parent, record_video_size={'width': 1440, 'height': 900},
                            reduced_motion='no-preference')
    try:
        demo = context.new_page()
        record_home(demo, QUESTION, company_id)
    finally:
        context.close()
    demo.video.save_as(destination)
    demo.video.delete()


@pytest.mark.parametrize('width', [1440, 390])
def test_v4_fresh_company_journey(width, monkeypatch):
    for var in ('ATHAR_WEBHOOKS', 'ATHAR_EMBED_SECRET', 'ATHAR_FRAME_ANCESTORS'):
        monkeypatch.delenv(var, raising=False)
    errors = []
    screens_value = os.getenv('ATHAR_P26_SCREENS')
    screens = Path(screens_value) if screens_value else None
    if screens:
        screens.mkdir(parents=True, exist_ok=True)
    with (server() as origin, sync_playwright() as playwright,
          patch.object(codex_cli, 'find_codex', return_value=Path('/fixture/bin/codex')),
          patch.object(codex_cli, '_run', side_effect=codex_parse) as codex):
        browser = playwright.chromium.launch(channel='chromium', args=['--use-angle=metal', '--ignore-gpu-blocklist'])
        try:
            page = local_context(browser, origin, width, errors).new_page()
            page.goto('/')
            login(page, 'qa-owner')
            company = new_company(page)
            update_sales(page, company['company_id'])
            stage = home(page, company)
            themes(page, screens)
            ask(page, stage, company['model_id'])
            comparison = compare(page)
            approve(page, company['model_id'])
            export_comparison(page, comparison)
            embed(browser, page, origin, width, errors, company)
            assert codex.call_count == 1
            if width == 1440 and (video := os.getenv('ATHAR_P26_VIDEO')):
                record_demo(browser, page, origin, errors, company['company_id'], Path(video))
                assert codex.call_count == 2
            assert not errors, errors
        finally:
            browser.close()
