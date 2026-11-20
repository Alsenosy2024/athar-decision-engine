"""Exercise the forecast-based production decision over Uvicorn and TCP."""
import math

import pytest

from test_arch_e2e import http_client, payload
from test_hybrid_h2 import assert_evidence


def test_hybrid_decision_lifecycle(http_client):
    client = http_client
    company = '/api/companies/2'
    forecast = payload(client.post(f'{company}/forecast'))
    assert any(low < high for row in forecast['series'] for low, high in zip(row['p10'], row['p90']))
    assert all(low <= center <= high for row in forecast['series']
               for low, center, high in zip(row['p10'], row['p50'], row['p90']))

    request = {'forecast_id': forecast['forecast_id'], 'time_limit': 2,
               'risk_policy': {'alpha': .9, 'tail_weight': .5}}
    run = payload(client.post(f'{company}/optimize', json=request))
    rid = run['run_id']
    run_url = f'/api/runs/{rid}'
    saved = payload(client.get(run_url))['result']
    contract, risk = run['decision_contract'], run['risk_assessment']
    assert saved['decision_contract'] == contract and saved['risk_assessment'] == risk
    assert contract['forecast_id'] == forecast['forecast_id']
    assert contract['approval_policy'] == 'shadow_only'
    for row in forecast['series']:
        for band in ('p10', 'p50', 'p90'):
            frozen = contract['scenarios'][band][row['key']]
            assert frozen == row[band][:math.ceil(contract['horizon']['periods'] / 7)]

    scenarios = risk['scenario_set']
    assert scenarios['input_fingerprint'] == contract['input_fingerprint']
    assert scenarios['quantile_fingerprint'] == contract['scenario_set_fingerprint']
    assert scenarios['source'] == 'pinned_forecast_quantiles'
    assert (scenarios['full_count'], scenarios['reduced_count']) == (64, 16)
    data = payload(client.get(f"/api/datasets/{contract['dataset_id']}"))['payload']
    bookings = {}
    for order in data['orders']:
        key = (order['sku_id'], order['due_day'] // 7)
        bookings[key] = bookings.get(key, 0) + order['qty']
    horizon = contract['horizon']['periods']
    expected = {
        (sku, week): max(0, round(total * min(7, horizon - week * 7) / 7) - bookings.get((sku, week), 0))
        for sku, totals in contract['scenarios']['p50'].items() for week, total in enumerate(totals)
    }
    assert {(row['sku_id'], row['due_day'] // 7): row['qty'] for row in scenarios['forecast_orders']} == expected
    assert sum(row['probability'] for row in scenarios['representatives']) == pytest.approx(1)
    assert len({row['scenario_fingerprint'] for row in risk['attempts']}) >= 2
    assert len(risk['candidates']) >= 2
    eligible = [row for row in risk['candidates'] if row['assessment']['eligible']]
    selected = min(eligible, key=lambda row: (row['assessment']['score'], row['id']))
    assert risk['selected_id'] == selected['id']
    assessment = selected['assessment']
    assert assessment['cvar']['cost_sar'] >= assessment['mean']['cost_sar']
    scales = risk['scales']
    assert assessment['score'] == pytest.approx(
        assessment['mean']['cost_sar'] / scales['cost_sar']
        + .5 * sum(assessment['cvar'][key] / scale for key, scale in scales.items())
    )
    assert assessment['breaches']['physical']['max_excess'] == 0

    replay = payload(client.post(f'{company}/optimize', json={'replay_run_id': rid}))
    assert replay['solution_fingerprint'] == run['solution_fingerprint']
    assert replay['decision_contract'] == contract
    assert 'shadow_only' in saved['solution_record']['optimized']['reasons']
    blocked = payload(client.post(f'{company}/plans', json={'run_id': rid, 'variant': 'optimized'}), 422)
    assert blocked['detail']['code'] == 'shadow_run'

    ask = f'{company}/chat'
    explanation = payload(client.post(ask, json={'run_id': rid, 'remote': False, 'message': 'اشرح النتيجة'}))
    assert explanation['action'] == 'explain' and explanation['run_id'] == rid
    assert_evidence(client, explanation)
    tail = next(item for item in explanation['evidence'] if item['metric'] == 'cost_cvar')
    assert tail['value'] == assessment['cvar']['cost_sar']

    question = 'لو زودنا حد الائتمان ٢٠٪؟'
    answer = payload(client.post(ask, json={'run_id': rid, 'remote': False, 'message': question}))
    assert answer['action'] == 'whatif' and answer['run_id'] != rid
    assert_evidence(client, answer)
    rerun = payload(client.get(f"/api/runs/{answer['run_id']}"))
    assert rerun['base_run_id'] == rid and rerun['dataset_id'] == saved['solution_record']['snapshot']['dataset_id']
    rerun_result = rerun['result']
    assert rerun_result['risk_assessment']['scenario_set']['fingerprint'] == scenarios['fingerprint']
    assert rerun_result['source_input_fingerprint'] == contract['input_fingerprint']
    assert rerun_result['solution_record']['limits']['credit_limit'] == pytest.approx(
        saved['solution_record']['limits']['credit_limit'] * 1.2,
    )
    assert rerun_result['risk_assessment']['scales']['peak_credit_sar'] != scales['peak_credit_sar']
    assert payload(client.get(f'{company}/chat/history'))[-1]['evidence'] == answer['evidence']
    rejected = payload(client.post(f'{company}/optimize', json=request | {
        'risk_policy': {'min_service_pct': 100},
    }))['risk_assessment']
    assert rejected['selected_id'] is None
    assert rejected['reason'] == 'no_eligible_candidate'
    assert rejected['candidates']
    assert all('service' in row['assessment']['failed_rules'] for row in rejected['candidates'])

    actuals = f'{company}/decision-loop/runs/{rid}/actuals'
    assert scenarios['forecast_orders']
    quantities = {row['id']: row['qty'] + 7 for row in scenarios['forecast_orders']}
    assert client.get(actuals).status_code == 404
    assert client.post(actuals, json={'quantities': {}}).status_code == 422
    outcome = payload(client.post(actuals, json={'quantities': quantities}))
    assert payload(client.get(actuals)) == outcome
    assert payload(client.post(actuals, json={'quantities': quantities})) == outcome
    changed = quantities | {next(iter(quantities)): 0}
    assert client.post(actuals, json={'quantities': changed}).status_code == 409
    assert client.get(f'/api/companies/1/decision-loop/runs/{rid}/actuals').status_code == 404
    assert client.post(f'/api/companies/1/decision-loop/runs/{rid}/actuals',
                       json={'quantities': quantities}).status_code == 404
    assert outcome['solution_fingerprint'] == run['solution_fingerprint']
    assert outcome['input_fingerprint'] == contract['input_fingerprint'] and outcome['quantities'] == quantities
    benchmark = outcome['benchmark']
    measured = benchmark['evaluated'][selected['id']]
    assert measured['candidate_version'] == selected['version']
    assert measured['realized_feasible']
    assert measured['cost_gap_sar'] == pytest.approx(
        measured['metrics']['cost_sar'] - benchmark['reference']['cost_sar'],
    )
    print(f"Hybrid HTTP run {rid}: {len(risk['candidates'])} candidates, selected {selected['id']}, "
          f"cost CVaR {tail['value']:.2f} SAR, realized cost {measured['metrics']['cost_sar']:.2f} SAR, "
          f"reference {benchmark['reference']['cost_sar']:.2f} SAR, "
          f"gap {measured['cost_gap_sar']:.2f} SAR, hindsight {benchmark['reference']['solver']['status']}")

    historical = payload(client.post(f'{company}/ai/decision-benchmark?detail=true'))
    window = historical['windows'][0]
    comparison = window['policies']['scenario']
    assert comparison['regret_sar'] == pytest.approx(comparison['realized']['cost_sar'] - window['oracle']['cost_sar'])
    assert historical['replay']['evaluation_matched'] == historical['replay']['evaluation_windows'] == 16
