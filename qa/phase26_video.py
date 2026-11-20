"""Record a paced walkthrough of the live V4 map with Playwright."""
from time import monotonic

from playwright.sync_api import expect


def record_home(page, question, company_id):
    assert page.evaluate("!matchMedia('(prefers-reduced-motion: reduce)').matches")
    started = monotonic()

    def hold_until(seconds):
        # Pacing is for the recorded walkthrough. Interactions use Playwright's auto-waiting.
        remaining = seconds - (monotonic() - started)
        if remaining > 0:
            page.wait_for_timeout(1000 * remaining)

    page.goto('/')
    page.get_by_test_id('company-switcher').select_option(str(company_id))
    stage = page.get_by_test_id('map-stage')
    expect(stage).to_be_visible()
    page.evaluate('document.fonts.ready')
    hold_until(6)

    sale = stage.locator('[data-department="sales"]')
    sale.hover()
    hold_until(9)
    sale.click()
    expect(page.get_by_test_id('map-department-detail')).to_be_visible()
    hold_until(14)
    sale.click()

    question_input = page.get_by_test_id('map-question')
    question_input.click()
    question_input.press_sequentially(question, delay=130)
    hold_until(21)
    page.get_by_test_id('map-ask').click()
    expect(page.get_by_test_id('map-assumption-value')).to_have_value('-20')
    hold_until(28)
    page.get_by_test_id('map-run').click()
    expect(stage).to_have_attribute('data-ripple', 'running')
    expect(stage).to_have_attribute('data-ripple', 'complete', timeout=8000)
    expect(page.get_by_test_id('map-impact-kpis')).to_be_visible()
    hold_until(37)

    week = page.get_by_role('slider', name='أسبوع وصول الأثر')
    week.scroll_into_view_if_needed()
    week.fill('0')
    hold_until(41)
    page.get_by_role('button', name='شغّل الأثر').click()
    expect(stage).to_have_attribute('data-ripple', 'running')
    expect(stage).to_have_attribute('data-ripple', 'complete', timeout=8000)
    hold_until(48)

    page.evaluate('window.scrollTo(0, 0)')
    page.get_by_test_id('theme-toggle').click()
    expect(page.locator('html')).to_have_attribute('data-theme', 'dark')
    hold_until(54)
    page.get_by_test_id('map-impact-kpis').scroll_into_view_if_needed()
    hold_until(60)
