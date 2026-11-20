"""Chromium check of the upload, review, and decision route."""
import argparse
from pathlib import Path
from unittest.mock import patch

from playwright.sync_api import expect, sync_playwright

from app.forecasting.foundation import TimesFMWorker
from qa.phase16_browser import capture, fake_timesfm, login, post_from, server

SCOPE = '/companies/1/ai'


def screenshot(page, screens, name, width):
    capture(page, name, 'h1', None)
    if screens:
        screens.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=screens / f'p20_{name}_{width}.png', full_page=True, animations='disabled')


def exercise(base, width, screens):
    errors, external = [], []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        context = browser.new_context(viewport={'width': width, 'height': 900}, locale='ar-SA')
        page = context.new_page()
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.on('console', lambda message: errors.append(message.text) if message.type == 'error' else None)

        def local_only(route):
            if route.request.url.startswith(base + '/'):
                route.continue_()
            else:
                external.append(route.request.url)
                route.abort()

        page.route('**/*', local_only)
        page.goto(base)
        login(page, 'qa-owner')
        page.get_by_role('link', name='مختبر الذكاء الاصطناعي').click()
        expect(page.get_by_role('heading', name='مختبر الذكاء الاصطناعي')).to_be_visible()
        page.get_by_text('استخدم Codex لمطابقة الحقول الغامضة').uncheck()
        page.get_by_label('اسم النموذج').fill('روافد للتوزيع')
        content = ('Metric,Value,Unit\nCash,500,SAR\nNet profit,1000,SAR\nDemand,100,units\n'
                   'Credit limit,600,SAR\n').encode()
        page.get_by_label('ملفات الشركة').set_input_files(
            {'name': 'rwa fed.csv', 'mimeType': 'text/csv', 'buffer': content},
        )
        page.get_by_label('احكي لي عن شركتك').fill(
            'الطلب -> صافي الربح = 5\nالطلب -> السيولة = -2',
        )
        draft = post_from(page, SCOPE + '/entry', page.get_by_role('button', name='اعرض الفرق للمراجعة'), 15000)
        expect(page.get_by_role('heading', name='فرق البيانات')).to_be_visible()
        expect(page.get_by_text('4 بنود في الفرق')).to_be_visible()
        expect(page.get_by_role('heading', name='العلاقات المقترحة')).to_be_visible()
        assert len(draft['data']['relations']) == 2
        screenshot(page, screens, 'entry', width)
        model = post_from(page, f'{SCOPE}/reviews/{draft["id"]}/decide',
                          page.get_by_role('button', name='اقبل المقترح'), 10000)
        expect(page.get_by_role('status').filter(has_text='اتسجل قبول المراجعة')).to_be_visible()
        assert model['outcome']['version'] == 1

        page.get_by_role('button', name='اختبار قرار', exact=True).click()
        page.get_by_label('وصف هدف القرار').fill('عايز أرفع الربح 10% والسيولة ما تنزلش عن 450 ريال')
        post_from(page, SCOPE + '/goal/interpret', page.get_by_role('button', name='استخرج الهدف'), 10000)
        page.get_by_label('المدخل', exact=True).select_option('demand')
        page.get_by_label('حد أدنى', exact=True).fill('80')
        page.get_by_label('حد أعلى', exact=True).fill('125')
        decision = post_from(page, SCOPE + '/recommend', page.get_by_role('button', name='رتّب التوصيات'), 20000)
        expect(page.get_by_role('heading', name='توصيات القرار')).to_be_visible()
        page.get_by_text('محامي الشيطان', exact=True).first.click()
        expect(page.get_by_text('أضعف فرضية', exact=False).first).to_be_visible()
        assert decision['data']['recommendations'][0]['objective'] == 1125
        save = page.get_by_role('button', name='اقبل واحفظ السيناريو').first
        expect(save).to_be_enabled()
        screenshot(page, screens, 'decision', width)
        saved = post_from(page, f'{SCOPE}/reviews/{decision["id"]}/decide', save, 10000)
        assert saved['outcome']['scenario_id']
        page.get_by_role('button', name='افتح مسار الأثر').first.click()
        answer = post_from(page, SCOPE + '/question', page.get_by_role('button', name='اسأل أثر', exact=True).last, 10000)
        expect(page.get_by_role('heading', name='رد أثر')).to_be_visible()
        assert answer['data']['facts'][0]['evidence']['path'][0]['source'] == 'demand'
        screenshot(page, screens, 'path', width)
        reject = post_from(page, f'{SCOPE}/reviews/{answer["id"]}/decide',
                           page.get_by_role('button', name='ارفض المقترح'), 10000)
        assert reject['status'] == 'rejected'

        page.get_by_label('الشركة', exact=True).select_option('2')
        page.get_by_role('button', name='التوقعات', exact=True).click()
        forecast = post_from(page, '/companies/2/ai/forecast',
                             page.get_by_role('button', name='احسب التوقع'), 15000)
        expect(page.get_by_text('تغطية مستهدفة', exact=False)).to_be_visible()
        assert forecast['data']['confidence'] == 0.8
        assert forecast['data']['interval_method'] == 'mapie_split_conformal'
        assert 'week' in forecast['data']['points'][0]
        screenshot(page, screens, 'forecast', width)
        assert not errors, errors
        assert not external, external
        context.close()
        browser.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--screens', type=Path, default=Path('qa/.work/phase20'))
    parser.add_argument('--width', type=int, nargs='*', default=[1440, 390])
    args = parser.parse_args()
    with patch.object(TimesFMWorker, 'forecast', fake_timesfm), server() as base:
        for width in args.width:
            exercise(base, width, args.screens)
            print(f'Chromium {width}: entry, accept, decision, drill-down, reject, forecast, '
                  '0 console errors, 0 external requests')


if __name__ == '__main__':
    main()
