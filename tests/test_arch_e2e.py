"""Run the four architecture streams together over a real loopback HTTP server."""
import csv
from datetime import date, timedelta
from io import BytesIO, StringIO
import socket
from threading import Thread
from time import monotonic, sleep

import httpx
from openpyxl import Workbook
from playwright.sync_api import expect, sync_playwright
import pytest
import uvicorn

from app.config import Settings
from app.main import create_app
from conftest import sign_in


@pytest.fixture
def http_client(tmp_path):
    app = create_app(Settings(db_path=tmp_path / 'architecture.sqlite', llm_provider='rules',
                              forecast_engine='stat'))
    server = uvicorn.Server(uvicorn.Config(app, log_level='warning', ws='none'))
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        origin = f'http://127.0.0.1:{listener.getsockname()[1]}'
        thread = Thread(target=server.run, kwargs={'sockets': [listener]}, daemon=True)
        thread.start()
        try:
            deadline = monotonic() + 15
            while not server.started:
                assert thread.is_alive() and monotonic() < deadline, 'Uvicorn did not start'
                sleep(.05)
            with httpx.Client(base_url=origin, timeout=30, trust_env=False) as client:
                assert client.get('/api/health').json()['status'] == 'ok'
                sign_in(client)
                yield client
        finally:
            server.should_exit = True
            thread.join(timeout=15)
            assert not thread.is_alive(), 'Uvicorn did not stop'


def retail_files():
    sales_csv = StringIO()
    sales = csv.writer(sales_csv)
    sales.writerow(['txn_day', 'store_id', 'branch_name', 'branch_parent', 'department',
                    'sku', 'units_sold', 'net_sales_sar', 'stock_units'])
    book = Workbook()
    ledger = book.active
    ledger.title = 'الحسابات الأسبوعية'
    ledger.append(['date', 'cost_sar', 'operating_expenses_sar', 'financing_expenses_sar',
                   'cash_balance_sar', 'receivables_sar', 'payables_sar'])
    for index in range(88):
        day = (date(2024, 1, 1) + timedelta(weeks=index)).isoformat()
        season = (30, 60, 30, 0)[index % 4]
        sales.writerow([day, 'RYD', 'الرياض', 'SA', 'الأغذية', 'A01', 10, 200 + season, 22])
        sales.writerow([day, 'JED', 'جدة', 'SA', 'الأجهزة', 'B02', 3, 300, 8])
        ledger.append([day, 200, 50, 10, 1000, 90, 40])
    xlsx = BytesIO()
    book.save(xlsx)
    return [('files', ('retail.csv', sales_csv.getvalue().encode())),
            ('files', ('accounts.xlsx', xlsx.getvalue()))]


def payload(response, status=200):
    assert response.status_code == status, response.text[:2000]
    return response.json()


def reconcile(rows, revenue):
    balance = 1000
    for week, row in enumerate(rows, 1):
        collected = 90 if week == 1 else revenue if week >= 3 else 0
        paid = 40 if week == 1 else 200
        assert row == {
            'week': week, 'opening_cash': balance, 'cash_in': collected, 'cash_out': paid + 60,
            'closing_cash': balance + collected - paid - 60,
            'collections': collected, 'supplier_payments': paid,
            'operating_expenses': 50, 'financing_expenses': 10,
            'closing_receivables': min(week, 2) * revenue, 'closing_payables': 200,
            'opening_inventory': 30, 'purchased_units': 13, 'sold_units': 13, 'closing_inventory': 30,
            'revenue': revenue, 'direct_cost': 200, 'profit': revenue - 260,
            'cumulative_profit': week * (revenue - 260),
        }
        balance += collected - paid - 60


def check_map_in_chromium(client, company_id):
    origin = str(client.base_url).rstrip('/')
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(reduced_motion='reduce')
        page.context.add_cookies([{'name': cookie.name, 'value': cookie.value, 'url': origin}
                                  for cookie in client.cookies.jar])
        page.route('**/*', lambda route: route.continue_() if route.request.url.startswith(origin + '/') else route.abort())
        page.goto(f'{origin}/?company_id={company_id}#/map')
        expect(page.get_by_test_id('map-data-context')).to_contain_text('ملفات الشركة المعتمدة')
        ledger = page.get_by_test_id('cash-ledger')
        revenue = page.get_by_test_id('map-kpis').get_by_role('article').filter(has_text='صافي المبيعات')
        for weeks, closing, sales in [(52, '12,730', '26,000'), (1, '990', '500')]:
            page.get_by_label('أفق الحساب').select_option(str(weeks))
            ledger.locator('summary').click()
            expect(ledger.locator('tbody tr')).to_have_count(weeks)
            expect(ledger.locator('tbody tr').last.locator('td').nth(3)).to_have_text(closing)
            expect(revenue.locator('.lm-ribbon-value bdi')).to_have_text(sales)
        browser.close()


def test_retail_from_reviewed_files_to_cash_decision_and_calibration(http_client):
    client = http_client
    company = payload(client.post('/api/onboarding/companies', json={
        'name': 'تجزئة المدن الجديدة', 'sector': 'enterprise_company',
    }), 201)
    cid, mid = company['company_id'], company['model_id']
    lab = f'/api/companies/{cid}/ai'
    model = f'/api/enterprise/models/{mid}'
    map_url = '/api/enterprise/map'
    reviewer = {'username': 'retail-reviewer', 'password': 'retail-review-password',
                'role': 'approver', 'company_ids': [cid]}
    payload(client.post('/api/users', json=reviewer), 201)
    files = retail_files()
    review = payload(client.post(lab + '/entry', data={'model_id': mid, 'remote': 'false'}, files=files), 201)
    assert review['status'] == 'pending' and review['model_version'] == 1
    assert review['data']['validation']['ok']
    assert {source['file'] for source in review['data']['sources']} == {'retail.csv', 'accounts.xlsx'}
    assert all(row['edge']['status'] == 'draft' for row in review['data']['relations'])
    assert payload(client.get(model))['graph']['edges'] == []
    with httpx.Client(base_url=client.base_url, timeout=30, trust_env=False) as approver:
        sign_in(approver, reviewer['username'], reviewer['password'])
        accepted = payload(approver.post(lab + f"/reviews/{review['id']}/decide", json={'status': 'accepted'}))
    assert accepted['outcome'] == {'model_id': mid, 'version': 2}

    settings = payload(client.put(f'/api/companies/{cid}/onboarding/cash-terms', json={
        'expected_version': 2, 'terms': {'collection_lag_weeks': 2, 'payment_lag_weeks': 1},
    }))
    assert settings['version'] == 3
    selection = {'model_id': mid, 'version': 3}
    same = payload(client.post(lab + '/entry', data={'model_id': mid, 'remote': 'false'}, files=files), 201)
    assert same['model_version'] == 3 and same['data']['model'] is None
    saved = payload(client.get(model))['graph']
    dimensions = saved['company_data']
    branch = next(item for item in dimensions['entities'].values() if item['code'] == 'RYD')
    branch_sales = 'sales.' + branch['id']
    leaf_sales = next(key for key, entities in dimensions['node_entities'].items()
                      if key.startswith('detail.sales.') and branch['id'] in entities)
    view = payload(client.get(map_url, params={'company_id': cid, **selection}))
    assert view['model']['version'] == 3
    assert {'الرياض', 'جدة', 'الأغذية', 'الأجهزة'} <= {group['name_ar'] for group in view['departments']}
    rollup = next(edge for edge in view['edges'] if edge['source'] == leaf_sales and edge['target'] == branch_sales)
    assert (rollup['status'], rollup['unit']) == ('approved', '1')
    assert rollup['provenance']['review_id'] == review['id']
    assert rollup['provenance']['actor'] == review['created_by']
    assert rollup['provenance']['approved_by'] == reviewer['username']
    key = payload(client.post(f'/api/companies/{cid}/integration-keys', json={
        'name': 'Retail acceptance', 'scopes': ['map:read', 'whatif:run'],
    }), 201)
    api_headers = {'X-Athar-API-Key': key['key']}
    public = f'/api/v1/companies/{cid}'
    public_map = payload(client.get(public + '/map', headers=api_headers, params=selection))
    assert public_map['has_cash_ledger'] and public_map['edges'] == view['edges']
    old_baseline = payload(client.get(map_url + '/baseline', params={
        'model_id': mid, 'version': 2, 'weeks': 1, 'include_ledger': 'true',
    }))
    assert old_baseline['cash_ledger']['rows'][0]['closing_cash'] == 1290

    for weeks, profit, cash in [(1, 260, 990), (52, 13_520, 13_730)]:
        body = {
            **selection, 'weeks': weeks, 'n': 1,
            'assumptions': [{'node_id': branch_sales, 'operation': 'add', 'value': 20}],
        }
        result = payload(client.post('/api/enterprise/whatif', json=body))
        assert payload(client.post(public + '/whatif', headers=api_headers, json=body)) == result | {'company_id': cid}
        assert result['context']['calculation_weeks'] == weeks
        indicators = {row['id']: row for row in result['nodes']}
        assert indicators['pnl.revenue']['after'] == 520 * weeks
        assert indicators['pnl.result']['after'] == profit
        assert indicators['cash']['after'] == cash
        cash_path = [edge for edge in result['paths'] if edge['source'] == 'pnl.revenue' and edge['target'] == 'cash']
        assert [edge['lag_weeks'] for edge in cash_path] == ([2] if weeks == 52 else [])
        before, after = (result['cash_ledger'][key] for key in ('before', 'after'))
        assert len(before) == len(after) == weeks
        reconcile(before, 500)
        reconcile(after, 520)
        baseline = payload(client.get(map_url + '/baseline', params={
            **selection, 'weeks': weeks, 'include_ledger': 'true',
        }))
        assert baseline['cash_ledger']['rows'] == before
        assert baseline['period_revenue'] == 500 * weeks
        assert baseline['kpis']['cash_low'] == before[-1]['closing_cash']
        assert baseline['kpis']['profit'] == before[-1]['cumulative_profit']

    goal = payload(client.post(lab + '/recommend', json={'model_id': mid, 'goal': {
        'objective': 'pnl.result', 'target': 13_500, 'cash_floor': 13_000, 'weeks': 52, 'trials': 4,
        'controls': [{'node_id': leaf_sales, 'low': 200, 'high': 220}],
    }}), 201)
    assert goal['data']['recommendations'][0]['disposition'] == 'eligible_decision'
    chosen = payload(client.post(lab + f"/reviews/{goal['id']}/decide", json={'status': 'accepted'}))
    did = chosen['outcome']['decision_id']
    log_url = model + '/prescriptive/decisions'
    decision = payload(client.get(log_url))['decisions'][0]
    assert decision['id'] == did and decision['version'] == 3 and decision['status'] == 'draft'
    contract = decision['outcome_contract']
    assert contract['assumption_source'] == 'lab_search_controls'
    assert contract['constraints']['cash_floor'] == 13_000
    indicator = contract['indicators']['profit']
    assert (indicator['node_id'], indicator['unit'], indicator['weeks'], indicator['basis'],
            indicator['expected'], indicator['required']) == ('pnl.result', 'SAR', 52, 'total_over_period', 13_520, True)
    assert indicator['days'] == 364
    payload(client.post(f'{log_url}/{did}/decide', json={'decision': 'approved'}))
    payload(client.post(f'{log_url}/{did}/actuals', json={
        'observed_on': date.today().isoformat(), 'metrics': {'profit': 13_500}, 'close': True,
        'evidence': 'دفتر التجزئة الاختباري',
    }), 201)
    closed = payload(client.get(map_url, params={'company_id': cid, **selection}))['decision_outcomes'][0]
    assert closed['id'] == did and closed['outcome_status'] == 'closed'
    assert closed['outcome_contract'] == contract and closed['accuracy'][0]['error'] == -20

    edge_report = payload(client.get(model + '/prescriptive/calibration'))
    assert edge_report['version'] == 3
    assert {edge['reason'] for edge in edge_report['edges']} >= {'approved_dimension_rollup', 'approved_financial_identity'}
    calibration = payload(client.post(lab + '/forecast', json={
        'model_id': mid, 'node_id': 'pnl.revenue', 'horizon': 4,
    }), 201)
    assert calibration['model_version'] == 3
    report = calibration['data']
    assert report['history_count'] == 88 and report['evaluation_n'] == 6
    assert report['calibration_n'] == 14 and 'seasonal_naive' in report['backtests']
    assert report['backtests']['stat']['holdout']['n'] == 24
    selected = report['backtests'][report['engine']]['holdout']
    assert 0 <= selected['mae'] <= report['backtests']['stat']['holdout']['mae']
    assert 0 <= selected['coverage'] <= 1
    assert len(selected['coverage_by_horizon']) == 4
    assert payload(client.get(lab + f"/reviews/{calibration['id']}"))['data'] == report
    check_map_in_chromium(client, cid)
