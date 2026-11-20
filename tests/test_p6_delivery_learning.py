import csv
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from io import BytesIO, StringIO
from xml.etree import ElementTree as ET

import pytest

from conftest import TEST_ADMIN
from openpyxl import load_workbook

from app.config import STATIC_DIR


def result(response, status=200):
    assert response.status_code == status, response.text
    return response.json()


def optimize(client, cid, **body):
    return result(client.post(f'/api/companies/{cid}/optimize', json={'time_limit': 1, **body}))


def make_plan(client, cid, run_id, variant='optimized', status=200):
    return result(client.post(f'/api/companies/{cid}/plans', json={
        'run_id': run_id, 'variant': variant,
    }), status)


def decide(client, plan, decision='approved', status=200):
    return result(client.post(f'/api/plans/{plan["id"]}/decide', json={
        'decision': decision, 'comment': 'تمت المراجعة',
    }), status)


def fetch_export(client, plan, fmt):
    return client.get(f'/api/plans/{plan["id"]}/export.{fmt}')


def upload(client, cid, plan, payload, status=200):
    return result(client.post(f'/api/companies/{cid}/actuals/upload', data={'plan_id': plan['id']},
                              files={'file': ('actuals.csv', payload, 'text/csv')}), status)


def sample(template):
    return (STATIC_DIR / 'templates' / f'actuals_{template}_template.csv').read_bytes()


def audit(client, cid):
    return result(client.get(f'/api/companies/{cid}/audit'))


@pytest.fixture
def draft(client):
    return make_plan(client, 1, optimize(client, 1)['run_id'])


@pytest.fixture
def contracting_plan(client, draft):
    return decide(client, draft)


@pytest.fixture
def production_plan(client):
    return decide(client, make_plan(client, 2, optimize(client, 2)['run_id']))


def test_create_draft_and_list_are_company_scoped(client, draft):
    assert draft['status'] == 'draft'
    assert draft['decided_at'] is None
    assert result(client.get('/api/companies/1/plans')) == [draft]
    assert result(client.get('/api/companies/2/plans')) == []
    assert make_plan(client, 2, draft['run_id'], status=404)['detail']['code'] == 'run_not_found'
    assert make_plan(client, 1, draft['run_id'], 'scenario', 422)['detail']['code'] == 'invalid_plan_variant'
    entry = next(row for row in audit(client, 1) if row['action'] == 'plan.create')
    assert entry['entity_id'] == draft['id']
    assert entry['details'] == {'run_id': draft['run_id'], 'variant': 'optimized'}


@pytest.mark.parametrize('fmt', ['xlsx', 'csv', 'json', 'p6.csv', 'p6.xml'])
def test_draft_export_blocked(client, draft, fmt):
    response = fetch_export(client, draft, fmt)
    assert result(response, 409)['detail']['code'] == 'plan_not_approved'
    assert all(not row['action'].startswith('plan.export.') for row in audit(client, 1))


def test_approve_with_comment_and_audit(client, contracting_plan):
    plan = contracting_plan
    assert plan['status'] == 'approved'
    assert plan['decided_at']
    assert (plan['comment'], plan['actor']) == ('تمت المراجعة', TEST_ADMIN)
    entry = next(row for row in audit(client, 1) if row['action'] == 'plan.approve')
    assert (entry['entity'], entry['entity_id'], entry['actor']) == ('plan', plan['id'], plan['actor'])
    assert entry['details'] == {'comment': plan['comment']}
    assert decide(client, plan, 'rejected', 409)['detail']['code'] == 'plan_already_decided'


def test_reject_plan(client, draft):
    rejected = decide(client, draft, 'rejected')
    assert rejected['status'] == 'rejected'
    assert rejected['decided_at']
    assert result(fetch_export(client, rejected, 'json'), 409)
    assert audit(client, 1)[0]['action'] == 'plan.reject'


def test_concurrent_decisions_persist_only_one(client, draft):
    def submit(decision):
        return client.post(f'/api/plans/{draft["id"]}/decide', json={
            'decision': decision, 'comment': '',
        })

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(submit, ['approved', 'rejected']))
    assert sorted(response.status_code for response in responses) == [200, 409]
    decisions = [row for row in audit(client, 1) if row['action'] in {'plan.approve', 'plan.reject'}]
    assert len(decisions) == 1


@pytest.mark.parametrize(('fixture', 'sheets', 'headers'), [
    ('contracting_plan', ['Activities', 'KPIs', 'CashFlow'],
     ['activity_id', 'project', 'phase', 'start_week', 'finish_week', 'start_date', 'finish_date',
      'resource', 'units']),
    ('production_plan', ['ProductionPlan', 'Shipments', 'Inventory', 'KPIs'],
     ['date', 'line', 'sku', 'qty']),
])
def test_workbook_erp_csv_and_json(client, request, fixture, sheets, headers):
    plan = request.getfixturevalue(fixture)
    response = fetch_export(client, plan, 'xlsx')
    assert response.status_code == 200, response.text[:100]
    workbook = load_workbook(BytesIO(response.content), read_only=True)
    try:
        assert workbook.sheetnames == sheets
        rows = list(workbook[sheets[0]].values)
        assert list(rows[0]) == headers
        assert len(rows) > 1
        if fixture == 'contracting_plan':
            start_week = rows[1][3]
            assert date.fromisoformat(rows[1][5]) == date(2026, 1, 5) + timedelta(weeks=start_week)
        else:
            assert next(workbook['Shipments'].values) == (
                'order', 'sku', 'qty', 'ship_date', 'due_date', 'late_days',
            )
    finally:
        workbook.close()

    response = fetch_export(client, plan, 'csv')
    assert response.status_code == 200
    assert response.content.startswith(b'\xef\xbb\xbf')
    assert list(csv.reader(StringIO(response.content.decode('utf-8-sig')))) == [
        [str(value) for value in row] for row in rows
    ]
    payload = result(fetch_export(client, plan, 'json'))
    snapshot = result(client.get(f'/api/runs/{plan["run_id"]}'))['result']['optimized']
    assert payload['plan'] == plan
    assert payload['rows'] == [dict(zip(headers, row)) for row in rows[1:]]
    assert payload['schedule'] == snapshot['plan']
    assert payload['kpis'] == snapshot['kpis']
    entries = {row['action'] for row in audit(client, plan['company_id'])}
    assert {'plan.export.xlsx', 'plan.export.csv', 'plan.export.json', 'dataset.demo'} <= entries


def test_p6_csv_exact_header_and_predecessors(client, contracting_plan):
    response = fetch_export(client, contracting_plan, 'p6.csv')
    assert response.status_code == 200
    reader = csv.DictReader(StringIO(response.content.decode('utf-8-sig')))
    rows = {row['Activity ID']: row for row in reader}
    assert reader.fieldnames == ['Activity ID', 'Activity Name', 'WBS', 'Planned Start',
                                 'Planned Finish', 'Original Duration', 'Predecessors', 'Resource']
    assert len(rows) == 15
    assert rows['A0']['Predecessors'] == ''
    assert rows['A1']['Predecessors'] == 'A0'
    assert rows['A1']['WBS'] == 'A'
    assert (date.fromisoformat(rows['A1']['Planned Finish'])
            - date.fromisoformat(rows['A1']['Planned Start'])).days == int(rows['A1']['Original Duration'])


def test_p6_xml_dates_wbs_and_links(client, contracting_plan):
    response = fetch_export(client, contracting_plan, 'p6.xml')
    assert response.status_code == 200
    root = ET.fromstring(response.content)
    assert root.tag == 'APIBusinessObjects'
    assert len(root.findall('./Project/WBS')) == 5
    activities = root.findall('./Project/Activity')
    assert len(activities) == 15
    assert len(root.findall('./Project/Relationship')) == 10
    for activity in activities:
        assert date.fromisoformat(activity.findtext('PlannedFinishDate')) > date.fromisoformat(
            activity.findtext('PlannedStartDate'),
        )
    assert audit(client, 1)[0]['action'] == 'plan.export.p6.xml'


@pytest.mark.parametrize('fmt', ['p6.csv', 'p6.xml'])
def test_p6_unavailable_for_production(client, production_plan, fmt):
    assert result(fetch_export(client, production_plan, fmt), 404)['detail']['code'] == 'p6_not_supported'


def test_contracting_actuals_comparison_and_latest(client, contracting_plan):
    payload = upload(client, 1, contracting_plan, sample('contracting'))
    comparison = payload['comparison']
    assert len(comparison['rows']) == len(comparison['chart']['labels']) == 15
    assert comparison['kpis_actual'] == {'observed_rows': 3, 'duration_weeks': 18, 'cost': 3520}
    a0 = next(row for row in comparison['rows'] if row['activity_id'] == 'A0')
    assert a0['actual']['cost'] == 600
    assert a0['variance']['cost'] == 600 - a0['plan']['cost']
    missing = next(row for row in comparison['rows'] if row['activity_id'] == 'E2')
    assert missing['actual']['cost'] is None and missing['variance']['cost'] is None
    assert result(client.get('/api/companies/1/actuals/latest')) == payload
    assert audit(client, 1)[0]['details'] == {'plan_id': contracting_plan['id'], 'rows': 3}


def test_production_actuals_use_shipments_and_order_cards(client, production_plan):
    comparison = upload(client, 2, production_plan, sample('production'))['comparison']
    assert comparison['kpis_actual']['qty'] == 18500
    assert comparison['kpis_actual']['observed_rows'] == 3
    snapshot = result(fetch_export(client, production_plan, 'json'))
    planned_orders = {row['order'] for row in snapshot['schedule']['shipments']}
    assert {row['order_id'] for row in comparison['rows']} == planned_orders | {'O1', 'O2', 'O3'}
    o1 = next(row for row in comparison['rows'] if row['order_id'] == 'O1')
    assert o1['variance']['qty'] == 7000 - o1['plan']['qty']
    assert o1['sku_id'] and comparison['chart']['metric'] == 'qty'


@pytest.mark.parametrize('line', ['A0,4,4,40', 'ZZ,0,4,40', 'A0,0,4,NaN', 'A0,0,4,40\nA0,0,5,50'])
def test_invalid_actuals_leave_no_records(client, contracting_plan, line):
    content = f'activity_id,actual_start_week,actual_finish_week,actual_cost\n{line}\n'.encode()
    error = upload(client, 1, contracting_plan, content, 422)
    assert error['detail']['code'] == 'invalid_actuals_row'
    assert result(client.get('/api/companies/1/actuals/latest'), 404)
    assert all(row['action'] != 'actuals.upload' for row in audit(client, 1))


def test_actuals_require_company_approved_plan(client, draft):
    assert upload(client, 1, draft, sample('contracting'), 409)
    assert upload(client, 2, draft, sample('contracting'), 404)


def test_contracting_recalibration_changes_forecast_without_double_counting(client, contracting_plan):
    before = result(client.post('/api/companies/1/forecast'))
    assert result(client.post('/api/companies/1/recalibrate'))['calibrations'] == []
    upload(client, 1, contracting_plan, sample('contracting'))
    first = result(client.post('/api/companies/1/recalibrate'))['calibrations']
    assert {row['key'] for row in first} == {'dur_ratio.F', 'dur_ratio.S', 'dur_ratio.X'}
    assert all(row['before'] is None and row['n'] == 21 for row in first)
    assert all({'mu', 'sigma', 'n'} == row['after'].keys() for row in first)
    upload(client, 1, contracting_plan, sample('contracting'))
    second = result(client.post('/api/companies/1/recalibrate'))['calibrations']
    assert all(row['before'] == row['after'] and row['n'] == 21 for row in second)
    after = result(client.post('/api/companies/1/forecast'))
    assert {phase['source'] for phase in after['phases'].values()} == {'calibration'}
    assert [row['p50'] for row in before['series']] != [row['p50'] for row in after['series']]
    assert {row['key'] for row in result(client.get('/api/companies/1/calibrations'))} == {
        'dur_ratio.F', 'dur_ratio.S', 'dur_ratio.X',
    }
    assert sum(row['action'] == 'calibration.update' for row in audit(client, 1)) == 3


def test_production_recalibration_clips_ratio_and_replaces_corrected_rows(client, production_plan):
    header = b'order_id,actual_ship_day,actual_qty\n'
    before = result(client.post('/api/companies/2/forecast'))
    observed = upload(client, 2, production_plan, header + b'O1,3,1000000\n')
    sku = next(row['sku_id'] for row in observed['comparison']['rows'] if row['order_id'] == 'O1')
    first = result(client.post('/api/companies/2/recalibrate'))['calibrations']
    assert len(first) == 1
    assert (first[0]['key'], first[0]['n']) == (f'demand_bias.{sku}', 1)
    assert first[0]['after'] == pytest.approx(1.3)
    upload(client, 2, production_plan, header + b'O1,3,1\n')
    second = result(client.post('/api/companies/2/recalibrate'))['calibrations'][0]
    assert second['before'] == pytest.approx(1.3)
    assert second['after'] == pytest.approx(0.85)
    assert second['n'] == 1
    after = result(client.post('/api/companies/2/forecast'))
    old_series = next(row for row in before['series'] if row['key'] == sku)
    new_series = next(row for row in after['series'] if row['key'] == sku)
    assert old_series['p50'] != new_series['p50']


def test_approved_scenario_uses_historical_model_and_scenario_inputs(client):
    base = optimize(client, 2, weights={'holding': 0.5})
    scenario = result(client.post('/api/companies/2/whatif', json={
        'base_run_id': base['run_id'], 'changes': [
            {'type': 'weight_change', 'name': 'holding', 'value': 4, 'relative': True},
            {'type': 'weight_change', 'name': 'holding', 'value': 2, 'relative': True},
            {'type': 'demand_pct', 'value': -10},
        ],
    }))
    plan = decide(client, make_plan(client, 2, scenario['run_id'], 'scenario'))
    result(client.put('/api/companies/2/model', json={'parameters': {'plan_start_date': '2027-01-04'}}))
    workbook = load_workbook(BytesIO(fetch_export(client, plan, 'xlsx').content), read_only=True)
    try:
        shipments = list(workbook['Shipments'].values)[1:]
        assert shipments and all(row[3].startswith('2026-02-') for row in shipments)
        assert next(row[2] for row in shipments if row[0] == 'O1') == 6300
    finally:
        workbook.close()
    assert {'dataset.demo', 'model.update', 'plan.export.xlsx'} <= {row['action'] for row in audit(client, 2)}


def test_chat_total_selects_approved_or_optimized_and_labels_scenario(client):
    base = optimize(client, 1)
    scenario = result(client.post('/api/companies/1/whatif', json={
        'base_run_id': base['run_id'], 'changes': [{'type': 'credit_limit_pct', 'value': 20}],
    }))

    def ask(message):
        return result(client.post('/api/companies/1/chat', json={'message': message}))

    optimized_answer = ask('كام إجمالي التكلفة؟')
    assert optimized_answer['run_id'] == base['run_id']
    assert f'الخطة المحسّنة من التشغيل رقم {base["run_id"]}' in optimized_answer['reply_ar']
    assert optimized_answer['kpis'] == base['optimized']['kpis']

    plan = decide(client, make_plan(client, 1, base['run_id']))
    latest = optimize(client, 1)
    assert latest['run_id'] > plan['run_id']
    approved_answer = ask('كام إجمالي التكلفة؟')
    assert approved_answer['run_id'] == base['run_id']
    assert f'الخطة المعتمدة رقم {plan["id"]}' in approved_answer['reply_ar']

    scenario_answer = ask('كام إجمالي التكلفة في السيناريو؟')
    assert scenario_answer['run_id'] == scenario['run_id']
    assert f'السيناريو رقم {scenario["run_id"]}' in scenario_answer['reply_ar']
    assert scenario_answer['kpis'] == scenario['scenario']['kpis']

    scenario_plan = decide(client, make_plan(client, 1, scenario['run_id'], 'scenario'))
    approved_scenario = ask('كام إجمالي التكلفة؟')
    assert approved_scenario['run_id'] == scenario['run_id']
    assert f'الخطة المعتمدة رقم {scenario_plan["id"]}' in approved_scenario['reply_ar']
    assert 'السيناريو' in approved_scenario['reply_ar']
