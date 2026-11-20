"""H1 local browser flow with optional desktop and mobile evidence captures."""
import os
from pathlib import Path
from unittest.mock import patch

from playwright.sync_api import expect, sync_playwright

from app.forecasting.foundation import TimesFMWorker
from app.llm import codex_cli
from qa.phase16_browser import capture, fake_codex, fake_timesfm, login, post_from, server


def test_hybrid_forecast_replay_and_replan_browser():
    output = os.getenv('ATHAR_H1_SCREENS')
    screens = Path(output) if output else None
    with (patch.object(TimesFMWorker, 'forecast', fake_timesfm),
          patch.object(codex_cli.subprocess, 'run', fake_codex),
          server() as url, sync_playwright() as playwright):
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={'width': 1440, 'height': 970})
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.route('**/*', lambda route: route.continue_() if route.request.url.startswith(url + '/') else route.abort())
        page.goto(url + '/#/optimize')
        login(page, 'qa-owner')
        page.get_by_test_id('company-switcher').select_option('2')
        page.goto(url + '/#/optimize')
        section = page.get_by_test_id('page-optimize')
        expect(section).to_be_visible()
        operate = '/companies/2/optimize'
        post_from(page, operate, section.get_by_test_id('run-optimize'))
        replanned = post_from(page, operate, section.get_by_test_id('replan-optimize'))
        assert replanned['solver']['hint_source'] == 'previous_plan'
        expect(section.get_by_test_id('hint-source')).to_have_text('تلميح الحل من الخطة السابقة')
        capture(page, 'h1_replan', '[data-testid="page-optimize"] h1', screens)

        plan = post_from(page, operate, section.get_by_test_id('forecast-optimize'))
        card = section.get_by_test_id('decision-contract')
        expect(card).to_be_visible()
        expect(card.get_by_role('columnheader', name='طلب مؤكد')).to_be_visible()
        expect(section.get_by_test_id('draft-optimized')).to_be_disabled()
        replayed = post_from(page, operate, card.get_by_test_id('replay-optimize'))
        assert plan['solution_fingerprint'] == replayed['solution_fingerprint']
        expect(card).to_contain_text(replayed['decision_contract']['input_fingerprint'][:16])
        capture(page, 'h1_contract', '[data-testid="page-optimize"] h1', screens)
        page.set_viewport_size({'width': 390, 'height': 844})
        page.wait_for_function('document.documentElement.scrollWidth === innerWidth')
        expect(card).to_be_visible()
        capture(page, 'h1_contract_mobile', '[data-testid="page-optimize"] h1', screens)

        page.set_viewport_size({'width': 1440, 'height': 970})
        page.goto(url + '/#/whatif')
        whatif = page.get_by_test_id('page-whatif')
        expect(whatif.get_by_test_id('run-mc')).to_have_text('قيّم نطاقات التوقع')
        risk = post_from(page, '/companies/2/montecarlo', whatif.get_by_test_id('run-mc'))
        assert set(risk['forecast_shortfall_units']) == {'p10', 'p50', 'p90'}
        expect(whatif.get_by_test_id('pinned-demand-risk')).to_be_visible()
        capture(page, 'h1_pinned_risk', '[data-testid="page-whatif"] h1', screens)
        assert not errors, errors
        browser.close()
