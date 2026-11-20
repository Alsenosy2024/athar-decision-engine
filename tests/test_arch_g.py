from datetime import date, timedelta

import numpy as np
import pytest

from app.db import get_conn
from app.enterprise.schema import Graph
from app.intelligence import analysis, calibration, forecasting, reviews, sparse_pc
from app.process_mining import simulation
from app.simulation import montecarlo
from arch_g_history import acceptance_history, node

SCOPE = '/api/companies/1/ai'


def save(client, nodes, edges=()):
    response = client.post('/api/enterprise/models?company_id=1', json={
        'name': 'سجل قبول أثر',
        'graph': {'nodes': [item.model_dump(mode='json') for item in nodes], 'edges': list(edges)},
    })
    assert response.status_code == 201, response.text
    return response.json()['id']


def edge(source, target, multiplier, lag=0):
    return {'source': source, 'target': target, 'name_ar': source + ' → ' + target,
            'rule': {'multiplier': multiplier, 'lag_weeks': lag}}


class BaselineWorker:
    def __init__(self, bias):
        self.bias = bias
        self.contexts = []

    def forecast(self, contexts, horizon):
        self.contexts = contexts
        return np.array([forecasting.stat_prediction(context, horizon, min(context) >= 0) + self.bias
                         for context in contexts])


def test_forecast_scores_every_candidate_and_gates_on_independent_window(client):
    histories, _ = acceptance_history()
    model_id = save(client, histories)
    bad = BaselineWorker(1_000)
    client.app.state.forecast_worker = bad
    for series in histories:
        if series.id == 'cash':
            client.app.state.forecast_worker = None
        response = client.post(SCOPE + '/forecast', json={'model_id': model_id, 'node_id': series.id, 'horizon': 4})
        assert response.status_code == 201, response.text
        result = response.json()['data']
        assert result['history_count'] == 88
        assert result['evaluation_n'] == 6 and result['calibration_n'] == 14
        stat = result['backtests']['stat']['holdout']
        assert stat['mase'] >= 0 and 0 <= stat['smape'] <= 200
        for report in result['backtests'].values():
            assert report['holdout']['n'] == 24
            assert 0 <= report['holdout_raw']['coverage'] <= 1
            assert len(report['holdout']['coverage_by_horizon']) == 4
        assert result['engine'] != 'timesfm'
        if series.id == 'sales':
            assert 'seasonal_naive' in result['backtests']
            assert 'seasonal_ets' in result['backtests']
            assert result['engine'] != 'stat'
            assert result['backtests'][result['engine']]['holdout']['mae'] < stat['mae']
            values = [point.value for point in series.baseline.points]
            assert all(context == values[:len(context)] for context in bad.contexts)
            assert max(len(context) for context in bad.contexts[:-1]) == len(values) - 4


def test_flat_forecast_scale_and_no_switch_without_holdout():
    result = forecasting.ensemble_forecast(node('sales', [10] * 26), 2, BaselineWorker(100))
    assert result['engine'] == 'stat'
    assert not result['evaluation_n']
    assert result['backtests']['stat']['mase'] is None
    assert result['backtests']['stat']['smape'] == 0


def test_entirely_estimated_series_fails_closed(client, db_path):
    series = node('sales', [100, 110, 120])
    model_id = save(client, [series])
    draft = client.post(SCOPE + '/refresh', json={'model_id': model_id, 'remote': False}).json()
    estimated = [{'node_id': series.id, 'date': point.date.isoformat(), 'value': point.value}
                 for point in series.baseline.points]
    with get_conn(db_path) as connection:
        reviews.update_provenance(connection, model_id, draft['id'], [], estimated)
    catalog = client.get(SCOPE + f'/catalog?model_id={model_id}').json()
    assert catalog['series'] == []
    result = client.post(SCOPE + '/forecast', json={'model_id': model_id, 'node_id': series.id})
    assert result.status_code == 422 and 'ai_forecast_no_observed_history' in result.text


@pytest.mark.parametrize('rho', [-1, .85, 1])
def test_correlated_draws_match_known_shocks_better_than_independent(rho):
    from app.simulation.dependence import Correlation

    centers, scales = {'sales': 100, 'cash': 200}, {'sales': 15, 'cash': 45}
    correlations = [Correlation(left='sales', right='cash', rho=rho)]

    def draw(seed, dependence):
        return np.asarray([list(row.values()) for row in
                           montecarlo.driver_trials(centers, scales, 2000, seed, dependence)])

    for seed in (42, 137, 733):
        independent, joint = draw(seed, []), draw(seed, correlations)
        measured = np.corrcoef(joint.T)[0, 1]
        assert abs(measured - rho) < abs(np.corrcoef(independent.T)[0, 1] - rho)
        assert np.allclose(joint.mean(axis=0), [100, 200], atol=2)
        assert np.array_equal(joint, draw(seed, correlations))


def test_correlated_whatif_preserves_default_and_rejects_invalid_matrix(client):
    model_id = save(client, [node(key, [100] * 4) for key in ('a', 'b', 'c', 'result')], [
        edge('a', 'result', 1), edge('b', 'result', 1), edge('c', 'result', 1),
    ])
    body = {'model_id': model_id, 'n': 1000, 'weeks': 1, 'seed': 42,
            'assumptions': [{'node_id': key, 'value': 100, 'uncertainty': 10} for key in 'abc']}
    path = '/api/enterprise/whatif'
    old = client.post(path, json=body)
    assert old.status_code == 200, old.text
    assert old.json() == client.post(path, json=body | {'correlations': []}).json()
    pairs = [{'left': key, 'right': other, 'rho': 1} for key, other in ('ab', 'ac', 'bc')]
    response = client.post(path, json=body | {'correlations': pairs})
    assert response.status_code == 200, response.text
    old_band = next(row['quantiles'] for row in old.json()['nodes'] if row['id'] == 'result')
    new_band = next(row['quantiles'] for row in response.json()['nodes'] if row['id'] == 'result')
    # A common shock yields a triangular total on [-30, 30] around 100.
    true_p90 = 100 + 30 * (1 - np.sqrt(.2))
    assert abs(new_band['p90'] - true_p90) < abs(old_band['p90'] - true_p90)
    assert response.json() == client.post(path, json=body | {'correlations': pairs}).json()
    pairs[0]['rho'] = -1
    assert client.post(path, json=body | {'correlations': pairs}).status_code == 422


def test_joint_allocation_improves_shared_plan_with_holdout(client):
    def option(key, credit, crews, profit, exposure):
        return {'id': key, 'resources': {'credit': credit, 'crews': crews}, 'value': profit,
                'exposures': {'demand': exposure, 'cost': -exposure}}

    payload = {
        'limits': {'credit': 10, 'crews': 5},
        'correlations': [{'left': 'demand', 'right': 'cost', 'rho': .6}],
        'units': [
            {'id': 'production', 'current': 'low', 'options': [option('low', 3, 2, 4, 0), option('high', 5, 2, 10, 1)]},
            {'id': 'contracting', 'current': 'low', 'options': [option('low', 3, 1, 4, 0), option('high', 6, 3, 15, 2)]},
        ],
    }
    response = client.post(SCOPE + '/allocate', json=payload)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result['optimal'] and result['preferred'] == 'joint'
    assert result['joint']['selection'] == {'production': 'low', 'contracting': 'high'}
    assert result['joint']['resource_usage'] == {'credit': 9, 'crews': 5}
    assert result['current']['score'] == 8
    assert all(row['joint_score'] > row['current_score'] for row in result['validation'])
    assert result == client.post(SCOPE + '/allocate', json=payload).json()
    payload['units'][1]['current'] = 'high'
    assert client.post(SCOPE + '/allocate', json=payload).json()['preferred'] == 'current'
    payload['units'][0]['current'] = 'high'
    repaired = client.post(SCOPE + '/allocate', json=payload).json()
    assert not repaired['current']['feasible'] and repaired['preferred'] == 'joint'
    assert repaired['joint']['feasible']


def test_work_shifts_and_replications_improve_operational_replay(monkeypatch):
    profile = {'departments': [{'id': 'packing', 'capacity': 1, 'service_seconds': 60 * 90}]}

    def controlled_arrivals(_profile, _weeks, _scenario, seed, _budget):
        return [(30 * 60 + seed % 2, ['packing'])]

    monkeypatch.setattr(simulation, 'arrivals', controlled_arrivals)
    base = {'company_id': 1, 'weeks': 1, 'seed': 42}
    full, _ = simulation.repeat(profile, simulation.SimulationRequest.model_validate(base))
    shifted, bands = simulation.repeat(profile, simulation.SimulationRequest.model_validate(base | {
        'replications': 3, 'scenario': {'shifts': {'packing': {'windows': [{'start': 60, 'end': 120},
                                                                         {'start': 180, 'end': 240}]}}},
    }))
    # 30 min before opening + 60 min work + 60 min closure + 30 min work.
    observed_lead = 180 / (24 * 60)
    actual = shifted[0]['lead_time_days']
    assert abs(actual - observed_lead) < abs(full[0]['lead_time_days'] - observed_lead)
    assert shifted[0]['departments'][0]['utilization_pct'] == 75
    assert bands['replication_seeds'] == [42, 43, 44]
    assert bands['replication_bands'][0]['metrics']['completed'] == {'n': 3, 'p10': 1, 'p50': 1, 'p90': 1}


def test_seasonal_anomaly_reduces_false_alarms_and_finds_known_event(client):
    histories, event_labels = acceptance_history()
    sales = histories[0]
    # Isolated high season produces rolling-MAD false positives on weekly demand.
    values = [100 if index % 4 != 3 else 120 for index in range(88)]
    event_index = next(iter(event_labels['sales']))
    values[event_index] += 31
    sales = node('sales', values)
    event_date = sales.baseline.points[event_index].date.isoformat()
    model_id = save(client, [sales])
    legacy = client.post(SCOPE + '/refresh', json={'model_id': model_id, 'remote': False}).json()['data']
    candidate = client.post(SCOPE + '/refresh', json={
        'model_id': model_id, 'remote': False, 'anomaly_method': 'seasonal',
    }).json()['data']
    old_days = {item['date'] for item in legacy['anomalies']}
    new_days = {item['date'] for item in candidate['anomalies']}
    assert event_date in new_days
    assert len(new_days - {event_date}) < len(old_days - {event_date})
    assert all(item['method'] == 'stl_robust_change' for item in candidate['anomalies'])


def test_seasonal_anomaly_confirms_persistent_shift():
    graph = Graph(nodes=[node('sales', [100] * 35 + [125] * 10)])
    shifts = [item for item in analysis.anomalies(graph, 'seasonal') if item['signal'] == 'persistent_level_shift']
    assert [item['date'] for item in shifts] == [graph.nodes[0].baseline.points[37].date.isoformat()]


def test_sparse_pc_recovers_late_nodes_without_global_overlap():
    rng = np.random.default_rng(42)
    anchor = date(2020, 1, 6)

    def rows(values, offset=0):
        return {anchor + timedelta(weeks=i + offset): v for i, v in enumerate(values)}

    n = 240
    sources = {f'a{i:02}': rows(rng.normal(size=n)) for i in range(36)}
    x, y, z = rng.normal(size=(3, n))
    sources.update(x=rows(x, 300), y=rows(y, 300), z=rows(2*x + 2*y + .15*z, 300))
    expected = {('x', 'z'), ('y', 'z')}
    legacy = calibration.pc_links(sources)
    candidate = sparse_pc.discover(sources, calibration.pc_links)
    assert len(candidate ^ expected) < len(legacy ^ expected)
    assert candidate == expected


def test_calibration_drops_a_spurious_edge_after_controlling_common_cause():
    rng = np.random.default_rng(80)
    days = [date(2020, 1, 6) + timedelta(weeks=i) for i in range(120)]
    common, noise, error = rng.normal(size=(3, len(days)))
    source = dict(zip(days, 2*common + .1*noise))
    target = dict(zip(days, 3*common + .1*error))
    assert calibration.estimate(source, target, 7, lags=(0,))['holdout_skill'] > .8
    assert calibration.estimate(source, target, 7, lags=(0,), adjust={'common': dict(zip(days, common))}) is None


def test_scm_abducts_noise_and_cuts_incoming_edges(client):
    source = node('source', [1, 2, 3, 4, 5])
    middle = node('middle', [9, 12, 6, 14, 5])
    output = node('result', [20, 25, 42, 19, 31])
    model_id = save(client, [source, middle, output], [edge('source', 'middle', 2), edge('middle', 'result', 3, 1)])
    post = {'model_id': model_id, 'target': 'result', 'weeks': 3, 'interventions': {'source': 100, 'middle': 15}}
    response = client.post(SCOPE + '/counterfactual', json=post)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result['warmup_weeks'] == 1
    assert result['equations']['middle']['incoming_mechanism_replaced']
    assert [row['effect'] for row in result['periods']] == pytest.approx([0, 3*(15-6), 3*(15-14)])
    # Legacy propagation treats the middle change as additive and also applies source.
    old = client.post('/api/enterprise/whatif', json={'model_id': model_id, 'weeks': 3, 'assumptions': [
        {'node_id': key, 'value': value} for key, value in post['interventions'].items()
    ]}).json()
    legacy_effect = next(row['delta'] for row in old['nodes'] if row['id'] == 'result')
    assert abs(result['periods'][-1]['effect'] - 3) < abs(legacy_effect - 3)
    assert client.post(SCOPE + '/counterfactual', json=post | {'weeks': 5}).status_code == 422
