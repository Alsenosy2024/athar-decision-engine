"""Contract/replay and replanning acceptance through the normal optimization entry points."""
from copy import deepcopy
from datetime import UTC, datetime, timedelta
import json

import pytest

from app.db import encode, get_conn, utc_now
from app.decision_loop.warm_start import PreviousPlan, contracting_hint, production_hint
from app.ingestion.loader import load_demo
from app.optimization import contracting, production
from app.templates_registry import load_templates


def post(client, cid, path, body=None, status=200):
    response = client.post(f'/api/companies/{cid}/{path}', json={} if body is None else body)
    assert response.status_code == status, response.text
    return response.json()


def set_calibration(db_path, cid, key, value):
    with get_conn(db_path) as connection:
        connection.execute(
            '''INSERT INTO calibrations (company_id, key, value_json, updated_at)
               VALUES (?, ?, ?, ?) ON CONFLICT(company_id, key)
               DO UPDATE SET value_json=excluded.value_json, updated_at=excluded.updated_at''',
            (cid, key, encode(value), utc_now()),
        )


@pytest.mark.parametrize('cid,key,value,weights,use_previous', [
    (1, 'dur_ratio.F', {'mu': 0.03, 'sigma': 0.15, 'n': 20}, {'financing': 1.3}, False),
    (1, 'dur_ratio.S', {'mu': 0.12, 'sigma': 0.12, 'n': 20}, {'makespan': 1}, True),
    (2, 'demand_bias.S1', 1.1, {'holding': 0.5}, False),
    (2, 'demand_bias.S2', 1.4, {'unshipped': 1.8}, True),
])
def test_contract_replay_with_changed_live_state(client, db_path, cid, key, value, weights, use_previous):
    set_calibration(db_path, cid, key, value)
    options = {'weights': weights, 'time_limit': 2}
    if use_previous:
        options['previous_run_id'] = post(client, cid, 'optimize', {'time_limit': 2})['run_id']
    forecast_id = post(client, cid, 'forecast')['forecast_id']
    original = post(client, cid, 'optimize', options | {'forecast_id': forecast_id})
    contract = original['decision_contract']
    assert (contract['company_id'], contract['forecast_id']) == (cid, forecast_id)
    units = ('sku', 'units', 'day') if cid == 2 else ('activity', 'weeks', 'week')
    assert (contract['entity'], contract['unit'], contract['horizon']['unit']) == units
    assert contract['calibrations'][0]['value'] == value
    assert contract['weights'].items() >= weights.items()
    assert contract['solver']['seed'] == 7 and contract['solver']['workers'] == 1
    assert set(contract['scenarios']) == {'p10', 'p50', 'p90'}
    assert original['optimized']['plan']
    assert post(client, cid, 'plans', {'run_id': original['run_id'], 'variant': 'optimized'}, 422)['detail']['code'] == 'shadow_run'

    risk_request = {'run_id': original['run_id'], 'n': 25, 'seed': 7}
    risk = post(client, cid, 'montecarlo', risk_request)
    if cid == 2:
        orders = {row['id']: row for row in load_demo('production')['orders']}
        for variant in ('baseline', 'optimized'):
            kpis = original[variant]['kpis']
            targets = kpis['forecast_inventory_targets']
            assert sum(row['confirmed_units'] for row in targets) == sum(row['qty'] for row in orders.values())
            assert all(row['unbooked_units'] == max(0, row['expected_total_units'] - row['confirmed_units'])
                       for row in targets)
            assert sum(orders[row['order']]['qty'] for row in kpis['shipments']) <= sum(row['qty'] for row in orders.values())
            skus = {row['id']: row for row in load_demo('production')['skus']}
            assert kpis['revenue'] == sum(orders[row['order']]['qty'] *
                                          skus[orders[row['order']]['sku_id']]['unit_price']
                                          for row in kpis['shipments'])
        assert risk['forecast_shortfall_units']['p50'] == sum(
            row['shortfall_units'] for row in original['optimized']['kpis']['forecast_inventory_targets']
        )
        assert risk['fill_rate']['p10'] == risk['fill_rate']['p90']
    else:
        p50 = contract['scenarios']['p50']
        assert all(row['finish_week'] - row['start_week'] == max(1, round(p50[row['activity_id']][0]))
                   for row in original['optimized']['plan']['activities'])

    # A later model, dataset and calibration must not leak into a prior run.
    set_calibration(db_path, cid, key, 0.75 if cid == 2 else {'mu': 0.3, 'sigma': 0.2, 'n': 22})
    assert client.put(f'/api/companies/{cid}/model', json={'parameters': {'credit_limit': 1}}).is_success
    assert post(client, cid, 'datasets/demo')['dataset_id'] != contract['dataset_id']
    post(client, cid, 'forecast')
    for _ in range(2):
        replay = post(client, cid, 'optimize', {'replay_run_id': original['run_id']})
        assert replay['decision_contract'] == contract
        assert replay['solution_fingerprint'] == original['solution_fingerprint']
        assert replay['optimized'] == original['optimized'] and replay['baseline'] == original['baseline']
    assert post(client, cid, 'montecarlo', risk_request) == risk
    assert post(client, 3 - cid, 'optimize', {'replay_run_id': original['run_id']}, 404)['detail']['code'] == 'run_not_found'
    assert post(client, cid, 'optimize', {'replay_run_id': original['run_id'], 'time_limit': 2}, 422)['detail']['code'] == 'invalid_replay'


@pytest.mark.parametrize('corruption,code,status', [
    ('legacy', 'forecast_incomplete', 422),
    ('missing_entity', 'forecast_incomplete', 422),
    ('missing_band', 'forecast_incomplete', 422),
    ('inverted_band', 'forecast_incomplete', 422),
    ('short_horizon', 'forecast_incomplete', 422),
    ('gap_history', 'forecast_incomplete', 422),
    ('missing_calibration_evidence', 'forecast_incomplete', 422),
    ('expired', 'forecast_stale', 422),
    ('future_forecast', 'forecast_stale', 422),
    ('future_calibration', 'forecast_stale', 422),
    ('different_start', 'forecast_stale', 422),
    ('foreign_company', 'forecast_not_found', 404),
    ('different_dataset', 'forecast_dataset_mismatch', 422),
])
def test_reject_invalid_forecast(client, db_path, corruption, code, status):
    cid = 2
    forecast_id = post(client, 1 if corruption == 'foreign_company' else cid, 'forecast')['forecast_id']
    if corruption == 'different_dataset':
        post(client, cid, 'datasets/demo')
    elif corruption == 'different_start':
        assert client.put(f'/api/companies/{cid}/model',
                          json={'parameters': {'plan_start_date': '2026-03-01'}}).is_success
    elif corruption != 'foreign_company':
        with get_conn(db_path) as connection:
            row = connection.execute('SELECT * FROM forecasts WHERE id = ?', (forecast_id,)).fetchone()
            item = json.loads(row['result_json'])
            stamp = row['created_at']
            evidence = item['decision_evidence']
            if corruption == 'legacy':
                del item['decision_evidence']
            elif corruption == 'missing_entity':
                item['series'].pop()
            elif corruption == 'missing_band':
                del item['series'][0]['p10']
            elif corruption == 'inverted_band':
                item['series'][0]['p90'][0] = 0
            elif corruption == 'short_horizon':
                item['series'][0]['p50'] = item['series'][0]['p50'][:1]
            elif corruption == 'gap_history':
                del item['series'][0]['history_weeks'][2]
            elif corruption == 'missing_calibration_evidence':
                del evidence['calibrations']
            elif corruption == 'expired':
                evidence['cutoff_at'] = (datetime.now(UTC) - timedelta(hours=25)).isoformat()
                connection.execute('UPDATE datasets SET created_at=? WHERE id=?',
                                   (evidence['cutoff_at'], row['dataset_id']))
                stamp = evidence['cutoff_at']
            elif corruption == 'future_forecast':
                stamp = (datetime.now(UTC) + timedelta(days=1)).isoformat()
            elif corruption == 'future_calibration':
                evidence['calibrations'].append({'key': 'demand_bias.S1', 'value': 1.2,
                                                'updated_at': (datetime.now(UTC) + timedelta(days=1)).isoformat()})
            connection.execute('UPDATE forecasts SET result_json=?, created_at=? WHERE id=?',
                               (encode(item), stamp, forecast_id))
    result = post(client, cid, 'optimize', {'forecast_id': forecast_id, 'time_limit': 2}, status)
    assert result['detail']['code'] == code
    assert client.get(f'/api/companies/{cid}/runs/latest?allow_missing=true').json() is None


def test_tampering_and_legacy_replay_are_rejected(client, db_path):
    legacy = post(client, 2, 'optimize', {'time_limit': 2})
    assert 'decision_contract' not in legacy and 'hint_source' not in legacy['solver']
    assert post(client, 2, 'optimize', {'replay_run_id': legacy['run_id']}, 409)['detail']['code'] == 'replay_unavailable'
    forecast = post(client, 2, 'forecast')
    run = post(client, 2, 'optimize', {'forecast_id': forecast['forecast_id'], 'time_limit': 2})
    with get_conn(db_path) as connection:
        contract = run['decision_contract']
        connection.execute('UPDATE datasets SET payload_json=? WHERE id=?',
                           (encode({}), contract['dataset_id']))
    assert post(client, 2, 'optimize', {'replay_run_id': run['run_id']}, 409)['detail']['code'] == 'replay_inputs_changed'


def test_changed_shadow_scenario_stays_ineligible(client):
    forecast = post(client, 2, 'forecast')
    base = post(client, 2, 'optimize', {'forecast_id': forecast['forecast_id'], 'time_limit': 2})
    scenario = post(client, 2, 'whatif', {'base_run_id': base['run_id'],
                                         'changes': [{'type': 'line_down', 'line': 'L1', 'from_day': 1, 'days': 1}]})
    assert scenario['source_input_fingerprint'] == base['decision_contract']['input_fingerprint']
    assert post(client, 2, 'plans', {'run_id': scenario['run_id'], 'variant': 'scenario'}, 422)['detail']['code'] == 'shadow_run'


@pytest.mark.parametrize('cid', [1, 2])
def test_warm_start_from_owned_plan_and_fallback(client, cid):
    previous = post(client, cid, 'optimize', {'time_limit': 2})
    warm = post(client, cid, 'optimize', {'time_limit': 5, 'previous_run_id': previous['run_id']})
    assert warm['solver']['hint_source'] == 'previous_plan'
    assert 0 < warm['solver']['first_qualified_s'] <= 5
    assert warm['optimized']['kpis']['credit_ok']
    assert post(client, 3 - cid, 'optimize', {'previous_run_id': previous['run_id']}, 404)['detail']['code'] == 'run_not_found'

    field = 'project_start_date' if cid == 1 else 'plan_start_date'
    assert client.put(f'/api/companies/{cid}/model', json={'parameters': {field: '2027-04-08'}}).is_success
    fallback = post(client, cid, 'optimize', {'time_limit': 2, 'previous_run_id': previous['run_id']})
    assert fallback['solver']['hint_source'] == 'heuristic'
    assert fallback['optimized']['plan']


def test_production_rebind_filters_slots_and_changed_orders():
    dataset = load_demo('production')
    params = load_templates()['production'].model_defaults()['parameters']
    prior_plan = production.solve(dataset, time_limit=2)['optimized']['plan']
    current = deepcopy(dataset)
    order = current['orders'][0]
    order['qty'] += 1
    blocked = prior_plan['schedule'][-1]
    params_now = params | {'line_down': {blocked['line']: [blocked['day']]}}
    problem = production.prepare(current, params_now)
    warm = production_hint(problem, PreviousPlan(prior_plan, dataset, params), params_now, None)
    assert warm is None or all(row['order'] != order['id'] for row in warm['shipments'])
    assert warm is None or all((row['line'], row['day']) != (blocked['line'], blocked['day']) for row in warm['schedule'])

    spare = {'schedule': [blocked, prior_plan['schedule'][0]], 'shipments': []}
    params_now |= {'cold_capacity': 1_000_000, 'opening_cash': 1_000_000}
    problem = production.prepare(current, params_now)
    warm = production_hint(problem, PreviousPlan(spare, dataset, params), params_now, None)
    assert warm['schedule'] == [prior_plan['schedule'][0]]


def test_contracting_rebind_partial_prefix_and_precedence():
    dataset = load_demo('contracting')
    params = load_templates()['contracting'].model_defaults()['parameters']
    problem = contracting.prepare(dataset, params)
    plan = contracting.evaluate(problem, contracting.baseline(problem))['plan']
    plan['activities'] = [row for row in plan['activities'] if row['activity_id'] != 'A1']
    warm = contracting_hint(problem, PreviousPlan(plan, dataset, params), params)
    assert 'A0' in warm and 'A1' not in warm and 'A2' not in warm and 'B0' in warm
    solved = contracting.solve(dataset, time_limit=2, previous=PreviousPlan(plan, dataset, params))
    assert solved['solver']['hint_source'] == 'previous_plan'
    assert solved['optimized']['kpis']['starts'].keys() == {row['id'] for row in dataset['activities']}
