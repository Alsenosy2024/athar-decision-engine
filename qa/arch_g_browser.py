"""Exercise calibration over localhost and capture the Desktop and mobile report."""
import argparse
from dataclasses import replace
import json
from pathlib import Path
from unittest.mock import patch

from playwright.sync_api import expect, sync_playwright

from app.config import Settings
from qa.phase16_browser import PASSWORD, capture, post_from, server
from tests.arch_g_history import acceptance_history


def local_settings(**kwargs):
    return replace(Settings(**kwargs), forecast_engine='auto', llm_provider='rules')


def exercise(base, screens, results):
    nodes, _ = acceptance_history()
    errors = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        context = browser.new_context(viewport={'width': 1440, 'height': 1000}, locale='ar-SA')
        auth = context.request.post(base + '/api/auth/login', headers={'X-Athar-Login': '1'},
                                    data={'username': 'qa-owner', 'password': PASSWORD})
        assert auth.ok, auth.text()
        headers = {'X-CSRF-Token': auth.json()['csrf_token']}
        created = context.request.post(base + '/api/enterprise/models?company_id=1', headers=headers, data={
            'name': 'سجل قبول أثر',
            'graph': {'nodes': [node.model_dump(mode='json') for node in nodes]},
        })
        assert created.ok, created.text()
        model_id = created.json()['id']
        page = context.new_page()
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.on('console', lambda message: errors.append(message.text) if message.type == 'error' else None)

        def local_only(route):
            if route.request.url.startswith(base + '/'):
                route.continue_()
            else:
                errors.append(f'External request: {route.request.url}')
                route.abort()

        page.route('**/*', local_only)
        page.goto(base + f'/ai?company_id=1&model_id={model_id}')
        page.get_by_role('button', name='التوقعات', exact=True).click()
        expect(page.get_by_label('مؤشر التوقع')).to_have_value('sales')
        review = post_from(page, '/companies/1/ai/forecast', page.get_by_role('button', name='احسب التوقع'))
        report = page.get_by_role('region', name='تقرير معايرة التوقع')
        expect(report).to_be_visible()
        expect(report.get_by_role('rowheader', name='TimesFM', exact=True)).to_be_visible()
        expect(report.get_by_role('rowheader', name='Chronos-Bolt', exact=True)).to_be_visible()
        expect(report.get_by_text('المختار', exact=False)).to_be_visible()
        screens.mkdir(parents=True, exist_ok=True)
        for width in (1440, 390):
            page.set_viewport_size({'width': width, 'height': 1000})
            capture(page, f'calibration {width}', '.ai-calibration-table th[scope=row]', None)
            report.screenshot(path=screens / f'calibration_{width}.png', animations='disabled')
            assert report.evaluate('element => element.scrollWidth === element.clientWidth')
        measured = {'sales': review['data']}
        for key in ('cash', 'inventory', 'demand.S1'):
            company_id = 2 if key == 'demand.S1' else 1
            reply = context.request.post(base + f'/api/companies/{company_id}/ai/forecast',
                                         headers=headers, timeout=180_000,
                                         data={'node_id': key, 'model_id': model_id if company_id == 1 else None})
            assert reply.ok, reply.text()
            measured[key] = reply.json()['data']
            assert {'timesfm', 'chronos_bolt'} <= measured[key]['backtests'].keys()
        assert not errors, errors
        results.parent.mkdir(parents=True, exist_ok=True)
        results.write_text(json.dumps(measured, ensure_ascii=False, indent=2))
        context.close()
        browser.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--screens', type=Path, required=True)
    parser.add_argument('--results', type=Path, default=Path('qa/.work/arch_g_results.json'))
    args = parser.parse_args()
    with patch('qa.phase16_browser.Settings', local_settings), server() as base:
        exercise(base, args.screens, args.results)
