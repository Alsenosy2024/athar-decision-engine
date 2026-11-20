import os
import re
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import expect, sync_playwright
import httpx

from qa.phase16_browser import PASSWORD, capture, server


def sign_in(page, username):
    expect(page.get_by_role('heading', name='تسجيل الدخول')).to_be_visible()
    page.get_by_label('اسم المستخدم').fill(username)
    page.get_by_label('كلمة السر').fill(PASSWORD)
    page.get_by_role('button', name='ادخل').click()


def prepare(url):
    with httpx.Client(base_url=url) as api:
        login = api.post('/api/auth/login', json={'username': 'qa-owner', 'password': PASSWORD},
                         headers={'X-Athar-Login': '1'})
        assert login.is_success
        api.headers['X-CSRF-Token'] = login.json()['csrf_token']
        company = api.post('/api/onboarding/companies', json={'name': 'شركة الأفق للتوزيع', 'sector': 'retail'})
        assert company.is_success, company.text
        cid = company.json()['company_id']
        owner = api.post('/api/users', json={
            'username': 'مخطط المبيعات', 'password': PASSWORD, 'role': 'planner', 'company_ids': [cid],
        })
        assert owner.is_success, owner.text
        return cid, str(owner.json()['id'])


@pytest.mark.parametrize('width', [1440, 390])
def test_department_owner_browser(width):
    with server() as url, sync_playwright() as playwright:
        cid, owner = prepare(url)
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={'width': width, 'height': 900})
        page.set_default_timeout(10000)
        errors, external = [], []
        page.on('console', lambda event: errors.append(event.text) if event.type == 'error' else None)
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.on('response', lambda response: errors.append(response.url) if response.status >= 400 else None)

        def local_only(route):
            if urlsplit(route.request.url).netloc == urlsplit(url).netloc:
                route.continue_()
            else:
                external.append(route.request.url)
                route.abort()

        page.route('**/*', local_only)
        screens_env = os.getenv('ATHAR_P24_SCREENS')
        screens = Path(screens_env) if screens_env else None

        def inspect(name, locator=None):
            capture(page, name, 'main h1', None)
            if screens:
                screens.mkdir(parents=True, exist_ok=True)
                target = screens / f'p24_{name}_{width}.png'
                if locator:
                    locator.screenshot(path=target, animations='disabled')
                else:
                    page.screenshot(path=target, animations='disabled')

        try:
            page.goto(url)
            sign_in(page, 'qa-owner')
            page.get_by_role('link', name='أصحاب البيانات').click()
            expect(page.get_by_role('heading', name='تكليف الأقسام')).to_be_visible()
            expect(page.get_by_label('الشركة', exact=True)).to_have_value(str(cid))
            tile = page.get_by_role('article').filter(has=page.get_by_role('heading', name='المبيعات والطلب'))
            tile.get_by_label('المخطّط').select_option(owner)
            tile.get_by_role('button', name='حفظ التكليف').click()
            expect(page.get_by_text('اتحدّث تكليف القسم.')).to_be_visible()
            expect(tile.get_by_label('المخطّط')).to_have_value(owner)
            inspect('assignment')

            page.get_by_role('button', name='خروج').click()
            sign_in(page, 'مخطط المبيعات')
            expect(page.get_by_role('heading', name='بيانات أقسامي')).to_be_visible()
            expect(page.get_by_label('القسم المكلّف بيه').locator('option')).to_have_count(1)
            expect(page.get_by_role('heading', name='المبيعات والطلب')).to_be_visible()
            expect(page.get_by_role('heading', name='المالية')).to_have_count(0)
            if width == 390:
                page.get_by_role('button', name='الوضع الداكن').click()
            page.get_by_role('textbox', name='القيمة', exact=True).fill('121')
            page.get_by_role('button', name='راجع الفرق', exact=True).click()
            expect(page.get_by_role('row').filter(has_text='الطلب الأسبوعي')).to_contain_text('121')
            page.get_by_role('button', name='الرجوع للتعديل').click()
            page.get_by_role('button', name='رفع قالب Excel').click()
            expect(page.get_by_role('link', name='نزّل قالب القسم')).to_be_visible()
            page.get_by_role('button', name='استخراج ذكي').click()
            page.get_by_label('ملف القسم').set_input_files({
                'name': 'sales.txt', 'mimeType': 'text/plain', 'buffer': 'الطلب = 125'.encode(),
            })
            page.get_by_role('button', name='راجع الفرق', exact=True).click()
            preview = page.locator('section').filter(has=page.get_by_role('heading', name='راجع الفرق قبل الإرسال'))
            expect(preview.get_by_role('row').filter(has_text='الطلب الأسبوعي')).to_contain_text('125')
            inspect('preview', preview)
            preview.get_by_role('button', name='ابعته لاعتماد الأدمن').click()
            expect(page.get_by_role('status').filter(has_text='مستني اعتماد')).to_be_visible()
            expect(page.get_by_role('heading', name='المبيعات والطلب')).to_be_visible()

            page.get_by_role('button', name='خروج').click()
            sign_in(page, 'qa-owner')
            page.get_by_role('link', name='أصحاب البيانات').click()
            page.get_by_role('link', name='طلبات الاعتماد').click()
            item = page.locator('details').filter(has_text='مستني اعتماد')
            expect(item.get_by_role('row').filter(has_text='الطلب الأسبوعي')).to_contain_text('125')
            item.get_by_label('ملاحظة المراجعة').fill('اتراجعت قراءة الطلب')
            inspect('approval', item)
            item.get_by_role('button', name='اعتمد واحفظ إصدار').click()
            expect(page.get_by_role('status').filter(has_text='اتحفظ التحديث في الإصدار 2')).to_be_visible()
            expect(tile).to_contain_text(re.compile(r'الجاهزية 100\D*%'))
            assert not errors, errors
            assert not external, external
        finally:
            browser.close()
