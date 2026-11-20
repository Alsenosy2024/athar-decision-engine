from datetime import date, timedelta

from fastapi.testclient import TestClient
import numpy as np
import pytest

from app.db import encode, get_conn, utc_now
from app.prescriptive.records import accuracy
from conftest import sign_in

PASSWORD = 'phase18-reviewer-password'
BASE = '/api/enterprise/models'


def scalar(key, value, name, kind='profit_loss', unit='SAR'):
    return {'id': key, 'type': kind, 'name_ar': name, 'unit': unit,
            'baseline': {'kind': 'scalar', 'value': value},
            'definition': {'kind': 'formula', 'expression': 'مرجع النموذج'}}


def edge(source, target, multiplier=1, **kwargs):
    return {'source': source, 'target': target, 'name_ar': f'{source} → {target}',
            'rule': {'multiplier': multiplier, **kwargs}}


def series(node, values):
    node['baseline'] = {'kind': 'series', 'frequency': 'weekly',
                        'points': [{'date': str(date(2024, 1, 1) + timedelta(weeks=i)), 'value': float(value)}
                                   for i, value in enumerate(values)]}
    return node


def model_data():
    return {'name': 'شركة توزيع النور', 'graph': {
        'nodes': [
            scalar('price', 10, 'سعر البيع', 'pricing', 'SAR/unit'),
            scalar('terms', 10, 'مدة تحصيل العملاء', 'receivables', 'days'),
            scalar('pnl.result', 100, 'الربح'),
            scalar('cash', 50, 'السيولة', 'cash'),
            scalar('risk.credit', 10, 'مخاطر الائتمان', 'risk_kpi', '%'),
            scalar('risk.delay', 2, 'تأخير التسليم', 'risk_kpi', 'weeks'),
        ],
        'edges': [edge('price', 'pnl.result', 5), edge('terms', 'pnl.result', 1),
                  edge('price', 'cash', -2), edge('terms', 'cash', -3)],
    }}


def save_model(client, data=None, company_id=1):
    response = client.post(BASE, params={'company_id': company_id}, json=data or model_data())
    assert response.status_code == 201, response.text
    return response.json()


def update(client, snapshot, data):
    response = client.put(f'{BASE}/{snapshot["id"]}', json={**data, 'expected_version': snapshot['version']})
    assert response.status_code == 200, response.text
    return response.json()


def endpoint(model):
    return f'{BASE}/{model["id"]}/prescriptive'


def goal_body(version=1):
    return {'version': version, 'profit_pct': 10, 'cash_minimum': 52,
            'controls': [{'node_id': 'price', 'minimum': 10, 'maximum': 12},
                         {'node_id': 'terms', 'minimum': 8, 'maximum': 10}]}


def draft_body():
    return {'goal': goal_body() | {'profit_pct': 8}, 'name': 'تعديل السعر والتحصيل',
            'actions': {'price': 12, 'terms': 8}}


def submit_draft(client, path):
    response = client.post(path + '/decisions', json=draft_body())
    assert response.status_code == 201, response.text
    return response.json()['id']


def test_calibration_applies_bounded_lag_and_fallback_to_version(client):
    rng = np.random.default_rng(42)
    x = rng.integers(95, 115, 72)
    y = np.r_[200, 202, 2 * x[:-2]]
    data = {'name': 'تاريخ المعايرة', 'graph': {
        'nodes': [series(scalar('price', 10, 'السعر'), x),
                  series(scalar('pnl.result', 100, 'الربح'), y),
                  scalar('cash', 50, 'السيولة')],
        'edges': [edge('price', 'pnl.result', 0.5, mode='elasticity'),
                  edge('price', 'cash', 2, lag_weeks=1)],
    }}
    snapshot = save_model(client, data)
    path = endpoint(snapshot) + '/calibration'
    preview = client.get(path).json()
    fitted, fallback = preview['edges']
    assert fitted['rule']['lag_weeks'] == 2
    assert fitted['rule']['multiplier'] == pytest.approx(2 * x[-1] / y[-1])
    assert fitted['holdout_skill'] == pytest.approx(1)
    assert fallback['source_type'] == 'default' and fallback['reason'] == 'missing_history'
    saved = client.post(path, json={'version': 1})
    assert saved.status_code == 200, saved.text
    assert saved.json() == {'version': 2, 'edges': preview['edges']}
    assert client.get(endpoint(snapshot) + '/setup').json()['calibration'] == preview['edges']
    assert client.post(path, json={'version': 1}).status_code == 409

    result = client.post('/api/enterprise/whatif', json={
        'model_id': snapshot['id'], 'version': 2, 'weeks': 3, 'n': 1,
        'assumptions': [{'node_id': 'price', 'operation': 'add', 'value': 1}],
    }).json()
    profit = next(row for row in result['nodes'] if row['id'] == 'pnl.result')
    assert profit['delta'] == pytest.approx(2)
    assert profit['path'][0]['rule']['lag_weeks'] == 2

    next_data = {'name': snapshot['name'], 'graph': client.get(f'{BASE}/{snapshot["id"]}').json()['graph']}
    next_data['graph']['nodes'][0]['baseline'] = {'kind': 'scalar', 'value': 10}
    update(client, snapshot | {'version': 2}, next_data)
    restored = client.post(path, json={'version': 3}).json()
    assert restored['version'] == 4
    assert restored['edges'][0]['rule']['multiplier'] == 0.5
    assert restored['edges'][0]['source_type'] == 'default'


def test_goal_ranks_joint_actions_with_propagated_impact(client):
    path = endpoint(save_model(client)) + '/goal-seek'
    assert client.post(path, json=goal_body()).json()['recommendations'] == []
    body = goal_body() | {'profit_pct': 8}
    response = client.post(path, json=body)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result['evaluated'] == 3 and result['baseline_meets_goal'] is False
    best, = result['recommendations']
    assert best['actions'] == {'price': 12, 'terms': 8}
    assert best['kpis']['cash_low'] == 52 and best['kpis']['profit'] == 108
    assert next(row for row in best['impact'] if row['id'] == 'pnl.result')['delta'] == 8
    assert result == client.post(path, json=body).json()
    assert client.post(path, json=body | {'version': 2}).status_code == 409
    for item in ({'node_id': 'pnl.result', 'minimum': 1, 'maximum': 100},
                 {'node_id': 'price', 'minimum': 11, 'maximum': 13}):
        assert client.post(path, json=body | {'controls': [item]}).status_code == 422


def test_monitor_recomputes_from_each_revision_and_retains_thresholds(client):
    data = model_data()
    snapshot = save_model(client, data)
    path = endpoint(snapshot)
    assert client.get(path + '/monitor').json()['alerts'] == []
    response = client.put(path + '/thresholds', json={
        'cash_minimum': 55, 'credit_maximum': 5, 'delays_maximum': 1, 'driver_uncertainty': {'price': 1},
    })
    assert response.status_code == 200, response.text
    report = response.json()
    assert report['samples'] == 200
    assert {item['kpi'] for item in report['alerts']} == {'cash_low', 'credit_breach', 'delays'}
    assert client.get(path + '/monitor').json() == report
    response = client.post(f'{BASE}/{snapshot["id"]}/imports/apply', json={
        'expected_version': 1, 'updates': [{'node_id': 'cash', 'baseline': {'kind': 'scalar', 'value': 60}}],
    })
    assert response.status_code == 200, response.text
    data['graph']['nodes'][3]['baseline']['value'] = 60
    refreshed = client.get(path + '/monitor').json()
    assert refreshed['version'] == 2
    assert {item['kpi'] for item in refreshed['alerts']} == {'credit_breach', 'delays'}
    assert client.put(path + '/thresholds', json={'credit_maximum': 110}).status_code == 422

    data['graph']['edges'] = [item for item in data['graph']['edges'] if item['source'] != 'price']
    data['graph']['nodes'] = [item for item in data['graph']['nodes'] if item['id'] != 'price']
    update(client, snapshot | {'version': 2}, data)
    refreshed = client.get(path + '/monitor').json()
    assert refreshed['warning'] and refreshed['samples'] is None
    assert refreshed['unavailable'] == ['credit_breach']
    assert [item['kpi'] for item in refreshed['alerts']] == ['delays']


def test_decision_identity_snapshot_and_actual_accuracy(client):
    path = endpoint(save_model(client))
    assert client.post(path + '/decisions', json=draft_body() | {'actions': {'price': 12}}).status_code == 422
    decision_id = submit_draft(client, path)
    decision = f'{path}/decisions/{decision_id}'
    observed = {'observed_on': utc_now()[:10], 'metrics': {'profit': 100}}
    assert client.post(decision + '/actuals', json=observed).status_code == 409

    for name, role, companies in (('approver', 'approver', [1]), ('planner', 'planner', [1]),
                                  ('outsider', 'approver', [2])):
        assert client.post('/api/users', json={'username': name, 'password': PASSWORD, 'role': role,
                                              'company_ids': companies}).status_code == 201
    with TestClient(client.app) as reviewer:
        sign_in(reviewer, 'planner', PASSWORD)
        assert reviewer.post(decision + '/decide', json={'decision': 'approved'}).status_code == 403
        sign_in(reviewer, 'outsider', PASSWORD)
        assert reviewer.get(path + '/decisions').status_code == 404
        assert reviewer.post(decision + '/decide', json={'decision': 'approved'}).status_code == 404
        sign_in(reviewer, 'approver', PASSWORD)
        assert reviewer.post(decision + '/decide', headers={'X-CSRF-Token': 'bad'},
                             json={'decision': 'approved'}).status_code == 403
        response = reviewer.post(decision + '/decide', json={'decision': 'approved', 'comment': 'مراجعة'})
        assert response.status_code == 200, response.text
        assert reviewer.post(decision + '/decide', json={'decision': 'rejected'}).status_code == 409
    assert client.post(decision + '/actuals', json=observed).status_code == 201
    assert client.post(decision + '/actuals', json=observed | {'metrics': {'cash_low': 0}}).status_code == 201
    log = client.get(path + '/decisions').json()
    row, = log['decisions']
    assert row['reviewer'] == 'approver' and row['decided_by'] is not None
    assert row['forecast']['kpis']['profit'] == 108
    assert row['actuals']['metrics'] == {'profit': 100, 'cash_low': 0}
    summary = {item['kpi']: item for item in log['accuracy']}
    assert summary['profit'] == {'kpi': 'profit', 'count': 1, 'mae': 8, 'mape': 8}
    assert summary['cash_low']['mae'] == 52 and summary['cash_low']['mape'] is None


def test_stale_draft_is_preserved_and_can_be_rejected(client):
    data = model_data()
    snapshot = save_model(client, data)
    path = endpoint(snapshot)
    decision_id = submit_draft(client, path)
    update(client, snapshot, data)
    decision = f'{path}/decisions/{decision_id}'
    assert client.post(decision + '/decide', json={'decision': 'approved'}).status_code == 409
    assert client.post(decision + '/decide', json={'decision': 'rejected'}).status_code == 200
    assert client.get(path + '/decisions').json()['decisions'][0]['status'] == 'rejected'


def test_existing_sector_approval_log_and_matched_actuals(client, db_path):
    with get_conn(db_path) as connection:
        run_id = connection.execute(
            '''INSERT INTO runs (company_id, dataset_id, model_id, kind, result_json, status, wall_s, created_at)
               VALUES (1, 1, 1, 'optimize', ?, 'OPTIMAL', 0, ?)''',
            (encode({'optimized': {'kpis': {'total': 70}}}), utc_now()),
        ).lastrowid
        plan_id = connection.execute(
            '''INSERT INTO plans (company_id, run_id, variant, status, created_at, decided_at, actor)
               VALUES (1, ?, 'optimized', 'approved', ?, ?, 'reviewer')''', (run_id, utc_now(), utc_now()),
        ).lastrowid
        connection.execute(
            '''INSERT INTO actuals (company_id, plan_id, payload_json, comparison_json, created_at)
               VALUES (1, ?, '[]', ?, ?)''',
            (plan_id, encode({'kpis_plan': {'cost': 50, 'observed_rows': 1},
                              'kpis_actual': {'cost': 55, 'observed_rows': 1}}), utc_now()),
        )
    row, = client.get('/api/companies/1/decision-log').json()
    assert row['predicted'] == {'total': 70}
    assert row['accuracy'][0]['error'] == 5 and row['observed_rows'] == 1
    assert client.get('/api/companies/2/decision-log').json() == []


def test_accuracy_handles_missing_and_zero_actuals():
    assert accuracy({'profit': None}, {'profit': 100}) == []
    row, = accuracy({'profit': 4}, {'profit': 0})
    assert row['absolute_error'] == 4 and row['absolute_percentage_error'] is None


@pytest.mark.parametrize(('message', 'expected'), [
    ('عايز أرفع الربح ١٠٪ بدون ما الكاش ينزل تحت ٥٠٬٠٠٠', {'profit_pct': 10, 'cash_minimum': 50_000}),
    ('أريد رفع صافي الربح بنسبة عشرة بالمية من غير ما السيولة تنخفض عن ١٫٥ مليون ريال سعودي',
     {'profit_pct': 10, 'cash_minimum': 1_500_000}),
    ('خفض الربح 10% من غير ما الكاش ينزل تحت 50', None),
    ('عايز أرفع الربح 10% بدون ما الكاش ينزل تحت X', None),
])
def test_parse_goal_text(client, message, expected):
    response = client.post(endpoint(save_model(client)) + '/goal-input', json={'message': message})
    assert response.status_code == (200 if expected else 422), response.text
    if expected:
        assert response.json() == expected
