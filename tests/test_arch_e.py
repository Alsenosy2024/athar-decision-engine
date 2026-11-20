"""Acceptance of financial horizons, client plans and bounded optimization."""
import asyncio
from time import monotonic

import pytest
from openpyxl import Workbook

from app.db import get_conn
from app.enterprise.operating_source import poll, stage
from app.intelligence import decision
from app.optimization import contracting, production
from app.templates_registry import load_templates
from test_fix_c_data import weekly_company
from test_p17_onboarding import by_key, commit, manual, ok

WHATIF = '/api/enterprise/whatif'
MAP_BASELINE = '/api/enterprise/map/baseline'


def reconcile(rows):
    for row in rows:
        assert row['opening_cash'] + row['cash_in'] - row['cash_out'] == pytest.approx(row['closing_cash'], abs=1e-4)
    for first, second in zip(rows, rows[1:]):
        assert first['closing_cash'] == second['opening_cash']
        assert first['cumulative_profit'] + second['profit'] == pytest.approx(second['cumulative_profit'])


def test_horizon_ledger_with_receivable_payable_and_inventory_roll_forward(client):
    model = weekly_company(client)
    path = f"/api/companies/{model['company_id']}/onboarding"
    model = commit(client, path, manual(client, path, 'inventory', [{'key': 'inventory', 'value': 100}], model['version']))
    settings = {'expected_version': model['version'],
                'terms': {'collection_lag_weeks': 2, 'payment_lag_weeks': 1, 'inventory_target': 50}}
    updated = ok(client.put(path + '/cash-terms', json=settings))
    assert client.put(path + '/cash-terms', json=settings).status_code == 409
    body = {'model_id': model['model_id'], 'version': updated['version'], 'n': 1,
            'assumptions': [{'node_id': 'demand', 'operation': 'add', 'value': 10}]}
    for weeks in (1, 13, 52, 104):
        result = ok(client.post(WHATIF, json=body | {'weeks': weeks}))
        before, after = [result['cash_ledger'][side] for side in ('before', 'after')]
        assert len(before) == len(after) == weeks == result['context']['calculation_weeks']
        reconcile(before)
        reconcile(after)
        kpis = by_key(result['nodes'])
        assert kpis['pnl.result']['after'] == 1540 * weeks
        assert kpis['cash']['after'] == after[-1]['closing_cash']
        assert result['charts']['cash']['after'] == [row['closing_cash'] for row in after]
        assert after[0]['opening_inventory'] == 100 and after[0]['closing_inventory'] == 50
        assert after[0]['purchased_units'] == 60
        assert (after[0]['opening_cash'], after[0]['supplier_payments'], after[0]['closing_cash']) == (1000, 800, 90)
        assert after[0]['closing_receivables'] == 2750 and after[0]['closing_payables'] == 600
        if weeks == 1:
            assert after[-1]['closing_cash'] == 90
        else:
            assert after[1]['supplier_payments'] == 600
            assert after[2]['collections'] == 2750
            assert after[-1]['closing_cash'] == 200 + 1540 * weeks - 5500 + 1100 + 500
        baseline = ok(client.get(MAP_BASELINE, params={
            'model_id': body['model_id'], 'version': body['version'], 'weeks': weeks, 'include_ledger': True,
        }))
        assert baseline['cash_ledger']['rows'] == before
    old = ok(client.post(WHATIF, json=body | {'weeks': 1, 'version': model['version']}))
    assert old['cash_ledger']['after'][0]['closing_cash'] == 1740


def workbook(path, price):
    records = {
        'lines': [['LineQA', 'خط تشغيل عميل', 0, 0]],
        'skus': [['SKUQA', 'منتج عميل', 4, price, 1, 0, 0]],
        'rates': [['LineQA', 'SKUQA', 10]],
        'orders': [['OrderQA', 'عميل اختبار', 'SKUQA', 10, 0, 10, False]],
        'demand_history': [['SKUQA', 0, 10]],
    }
    book = Workbook()
    book.remove(book.active)
    for name, columns in load_templates()['production'].upload_sheets.items():
        sheet = book.create_sheet(name)
        sheet.append(columns)
        for record in records[name]:
            sheet.append(record)
    book.save(path)
    book.close()


def operating_graph(plan_id):
    def node(key, kind, unit, *, output=None, entity=None, value=None):
        definition = {'kind': 'formula', 'expression': 'قيمة من بيانات العميل'}
        if output:
            definition = {'kind': 'engine', 'engine': 'production_optimizer',
                          'sector': 'production', 'output': output, 'entity_id': entity}
        return {'id': key, 'type': kind, 'unit': unit, 'name_ar': key,
                'baseline': {'kind': 'scalar', 'value': value}, 'definition': definition}

    return {'name': 'تشغيل الإنتاج المعتمد', 'graph': {
        'arms': [{'input_source': 'client_plan', 'sector': 'production', 'approved_plan_id': plan_id}],
        'nodes': [
            node('price.SKUQA', 'pricing', 'SAR/unit', value=12),
            node('line.LineQA', 'production_line', 'units', output='plan.schedule', entity='LineQA'),
            node('pnl.revenue', 'profit_loss', 'SAR', output='ledger.revenue'),
            node('pnl.result', 'profit_loss', 'SAR', output='ledger.profit'),
            node('cash', 'cash', 'SAR', output='ledger.cash'),
        ],
        'edges': [{'source': 'price.SKUQA', 'target': 'pnl.revenue', 'name_ar': 'سعر الشحن المعتمد'}],
    }}


def test_scheduled_snapshot_and_approved_client_plan_replay(client, db_path, tmp_path, monkeypatch):
    path = tmp_path / 'client.xlsx'
    source_calls = []

    def observed_stage(*args):
        source_calls.append(path.exists())
        return stage(*args)

    monkeypatch.setattr('app.enterprise.operating_source.stage', observed_stage)

    async def scheduled():
        stop = asyncio.Event()
        worker = asyncio.create_task(poll(db_path, 2, path, 0.02, stop))
        try:
            async with asyncio.timeout(3):
                while not source_calls:
                    await asyncio.sleep(0.01)
                workbook(path, 12)
                while len(source_calls) < 3:
                    await asyncio.sleep(0.01)
        finally:
            stop.set()
            await worker

    asyncio.run(scheduled())
    assert source_calls[:3] == [False, True, True]
    snapshots = ok(client.get('/api/companies/2/datasets'))
    dataset = next(row for row in snapshots if row['source'] == 'upload')
    assert len([row for row in snapshots if row['source'] == 'upload']) == 1
    assert not dataset['is_active']
    dataset_id = dataset['id']
    assert ok(client.get(f'/api/datasets/{dataset_id}'))['payload']['skus'][0]['unit_price'] == 12
    run = ok(client.post('/api/companies/2/optimize', json={'dataset_id': dataset_id, 'time_limit': .5}))
    plan = ok(client.post('/api/companies/2/plans', json={'run_id': run['run_id'], 'variant': 'optimized'}))
    model_path = '/api/enterprise/models'
    graph = operating_graph(plan['id'])
    assert client.post(model_path, params={'company_id': 2}, json=graph).status_code == 422
    ok(client.post(f"/api/plans/{plan['id']}/decide", json={'decision': 'approved', 'comment': 'مراجعة العينة'}))
    assert client.post(model_path, params={'company_id': 1}, json=graph).status_code == 422
    snapshot = ok(client.post(model_path, params={'company_id': 2}, json=graph), 201)
    body = {'model_id': snapshot['id'], 'version': 1, 'weeks': 2, 'n': 1,
            'assumptions': [{'node_id': 'price.SKUQA', 'value': 15}]}
    result = ok(client.post(WHATIF, json=body))
    rows = by_key(result['nodes'])
    source = result['context']['operating_sources']['production']
    assert (source['dataset_id'], source['plan_id'], source['operating_model_version']) == (dataset_id, plan['id'], 1)
    assert (rows['pnl.revenue']['before'], rows['pnl.revenue']['after']) == (120, 150)
    assert (rows['pnl.result']['before'], rows['pnl.result']['after']) == (80, 110)
    assert 'operating_ledger' in {edge['method'] for edge in result['paths']}
    assert result['optimizers_run'] == {'baseline': [], 'scenario': ['production']}
    booked, collected = result['cash_ledger']['before']
    reconcile([booked, collected])
    assert (booked['revenue'], booked['collections'], collected['collections']) == (120, 0, 120)
    production_in_week_one = sum(row['qty'] for row in run['optimized']['plan']['schedule'] if row['day'] < 7)
    assert booked['produced_units'] == production_in_week_one
    short = ok(client.post(WHATIF, json=body | {'weeks': 1}))
    assert by_key(short['nodes'])['line.LineQA']['before'] == production_in_week_one
    assert booked['shipped_units'] == 10
    assert booked['direct_cost'] == 4 * booked['shipped_units']
    assert ok(client.get(MAP_BASELINE, params={'model_id': snapshot['id'], 'weeks': 2}))['profit'] == 80

    workbook(path, 22)
    new_dataset_id = stage(db_path, 2, path)
    assert dataset_id != new_dataset_id == stage(db_path, 2, path)
    ok(client.put('/api/companies/2/model', json={'parameters': {'opening_cash': 45000}}))
    replay = ok(client.post(WHATIF, json=body))
    assert replay == result
    with get_conn(db_path) as conn:
        hashes = conn.execute('SELECT sha256 FROM operating_source_snapshots WHERE company_id = 2').fetchall()
        assert len(hashes) == 2 and all(len(row['sha256']) == 64 for row in hashes)


@pytest.mark.parametrize('module', [production, contracting])
def test_solver_wall_limit_under_tiny_budget_returns_best_available_plan(module):
    started = monotonic()
    result = module.solve_demo(time_limit=.02, deterministic=True)
    elapsed = monotonic() - started
    solver = result['solver']
    assert solver['time_limit_s'] == .02
    assert solver['wall_s'] < .15 and elapsed < 1
    assert solver['state'] in ('partial', 'complete')
    assert solver['status'] in ('FEASIBLE', 'OPTIMAL')
    assert result['optimized']['plan']
    if solver['state'] == 'partial':
        assert solver['stop_reason'] == 'search_limit'


def test_goal_search_returns_completed_trials_on_deadline(client, monkeypatch):
    model = weekly_company(client)
    from app.enterprise.whatif import ImpactModel

    clock = [0.0]
    monkeypatch.setattr('app.optimization.budget.monotonic', lambda: clock[0])
    evaluate = ImpactModel.scenario

    def advance(*args, **kwargs):
        result = evaluate(*args, **kwargs)
        clock[0] += 6.0
        return result

    monkeypatch.setattr(ImpactModel, 'scenario', advance)
    result = ok(client.post('/api/enterprise/map/goal-seek', json={
        'model_id': model['model_id'],
        'goal': {'trials': 12, 'weeks': 1, 'controls': [{'node_id': 'price', 'low': 25, 'high': 30}]},
    }))
    assert result['search_status'] == {'state': 'partial', 'stop_reason': 'time_limit',
                                       'limit_s': 5, 'attempted_trials': 1}
    assert len(result['recommendations']) == result['evaluated_trials'] == 1
