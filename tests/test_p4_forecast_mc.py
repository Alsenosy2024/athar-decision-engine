from dataclasses import replace
from time import perf_counter

import numpy as np
import pytest

from app.forecasting.methods import croston_sba, demand_forecast, duration_backtest
from app.forecasting.service import run_forecast
from app.ingestion.loader import load_demo
from app.optimization import contracting, production
from app.optimization.kpis import resource_usage
from app.simulation.montecarlo import contracting_draw, production_draw, run_mc
from app.templates_registry import load_templates


def assert_bands(series, horizon):
    assert len(series['p10']) == len(series['p50']) == len(series['p90']) == horizon
    assert all(0 <= lo <= mid <= hi for lo, mid, hi in zip(series['p10'], series['p50'], series['p90']))
    assert np.isfinite(series['backtest']['mae'])
    assert np.isfinite(series['backtest']['mape'])
    assert series['backtest']['method']


def test_contracting_forecast_has_activity_bands_and_phase_fits():
    dataset = load_demo('contracting')
    forecast = run_forecast('contracting', dataset)
    assert forecast['method'] == 'lognormal'
    assert {series['key'] for series in forecast['series']} == {row['id'] for row in dataset['activities']}
    assert len(forecast['series']) == 15
    for phase, sigma in {'F': 0.15, 'S': 0.20, 'X': 0.12}.items():
        assert forecast['phases'][phase]['sigma'] == pytest.approx(sigma, abs=0.05)
        assert forecast['phases'][phase]['n'] == 20
    for series in forecast['series']:
        assert_bands(series, 1)
        assert series['backtest']['n'] == 20


def test_duration_backtest_uses_leave_one_out():
    planned = np.ones(6)
    actual = np.arange(1, 7, dtype=float)
    predicted = np.array([np.exp(np.log(np.delete(actual, i)).mean()) for i in range(len(actual))])
    result = duration_backtest(planned, actual)
    assert result['mae'] == pytest.approx(np.abs(actual - predicted).mean())
    assert result['method'] == 'leave_one_out'


def test_sparse_contracting_default_and_calibration_override():
    dataset = {**load_demo('contracting'), 'history': []}
    default = run_forecast('contracting', dataset)
    calibrated = run_forecast('contracting', dataset, {'dur_ratio.F': {'mu': 0.4, 'sigma': 0.2, 'n': 9}})
    assert default['phases']['F']['source_ar'] == 'افتراضي'
    assert default['phases']['F']['sigma'] == 0.15
    assert default['series'][0]['backtest']['n'] == 0
    assert calibrated['phases']['F']['source'] == 'calibration'
    assert calibrated['series'][0]['p50'][0] > default['series'][0]['p50'][0]


def test_demo_production_forecast_selects_sku_methods():
    forecast = run_forecast('production', load_demo('production'))
    assert len(forecast['series']) == 6
    for series in forecast['series']:
        expected = 'croston_sba' if series['key'] in {'S5', 'S6'} else 'holt_damped'
        assert series['method'] == expected
        assert len(series['history']) == 52
        assert series['backtest']['n'] == 8
        assert_bands(series, 4)


def test_croston_sba_uses_observed_intervals_and_excludes_zeros_from_mape():
    demand = np.array([0, 20, 0, 0, 30])
    predicted, one_step = croston_sba(demand, 4)
    assert one_step.tolist() == pytest.approx([0, 0, 9.5, 9.5, 9.5])
    assert predicted.tolist() == pytest.approx([9.5] * 4)
    method, _, metrics = demand_forecast([0, 20, 0, 0] * 5 + [0] * 7 + [10], 4, 1.0)
    assert method == 'croston_sba'
    assert metrics['n'] == 8
    assert metrics['mape_n'] == 1


def test_production_bias_scales_bands_for_one_sku():
    dataset = load_demo('production')
    base = run_forecast('production', dataset)['series']
    adjusted = run_forecast('production', dataset, {'demand_bias.S1': 1.2})['series']
    assert base[1:] == adjusted[1:]
    for key in ('p10', 'p50', 'p90'):
        assert adjusted[0][key] == pytest.approx(np.asarray(base[0][key]) * 1.2)


@pytest.mark.parametrize('values', [[], [0] * 12, [10]])
def test_short_demand_histories_have_finite_bands(values):
    _, bands, scores = demand_forecast(values, 4, 1.0)
    assert_bands({**dict(zip(('p10', 'p50', 'p90'), bands)), 'backtest': scores}, 4)


def test_priority_scheduler_honors_precedence_capacity_and_credit():
    dataset = load_demo('contracting')
    params = load_templates()['contracting'].model_defaults()['parameters'] | {
        'horizon_weeks': 100, 'capacity_override': {'R1': {0: 0}},
    }
    problem = contracting.prepare(dataset, params)
    durations = {a.id: a.duration + 1 for a in problem.activities}
    priority = [a.id for chain in reversed(list(problem.chains.values())) for a in reversed(chain)]
    sampled, starts = contracting.schedule_with_priority(problem, priority, durations)
    assert starts is not None
    assert starts['E0'] < starts['A0']
    for project, chain in sampled.chains.items():
        assert starts[chain[0].id] >= problem.projects[project]['nta_week']
        for predecessor, successor in zip(chain, chain[1:]):
            assert starts[successor.id] >= starts[predecessor.id] + durations[predecessor.id]
    usage = resource_usage(sampled.activities, starts, sampled.capacity)
    assert all(used <= cap for r, values in usage.items() for used, cap in zip(values, sampled.capacity[r]))
    assert contracting.evaluate(sampled, starts)['kpis']['credit_ok']
    assert all(sampled.receipts[a.id] * a.duration == problem.receipts[a.id] * durations[a.id]
               for a in problem.activities)


def test_overrun_is_kept_in_sample_and_breach_is_measured():
    dataset = load_demo('contracting')
    activity = dataset['activities'][0]
    project = {**dataset['projects'][0], 'deadline_week': 4}
    dataset = {**dataset, 'projects': [project], 'activities': [activity]}
    problem = contracting.prepare(dataset, load_templates()['contracting'].model_defaults()['parameters'] | {
        'horizon_weeks': 4, 'credit_limit': 1_000,
    })
    resources = {row['id']: row['capacity'] for row in dataset['resources']}
    kpis = contracting_draw(problem, resources, [activity['id']], {activity['id']: 8})
    assert kpis['makespan'] == 8
    assert kpis['late_weeks_total'] == 4
    assert kpis['breach'] == 200


@pytest.fixture(scope='module', params=['contracting', 'production'])
def optimized_demo(request):
    template = request.param
    dataset = load_demo(template)
    solver = {'contracting': contracting, 'production': production}[template]
    result = solver.solve(dataset, time_limit=3)
    return template, dataset, result['optimized']['plan']


def test_seeded_monte_carlo_acceptance(optimized_demo):
    template, dataset, plan = optimized_demo
    forecast = run_forecast(template, dataset)
    start = perf_counter()
    result = run_mc(template, dataset, plan, forecast, n=200, seed=42)
    assert perf_counter() - start < 10
    assert result == run_mc(template, dataset, plan, forecast, n=200, seed=42)
    assert result != run_mc(template, dataset, plan, forecast, n=200, seed=43)
    assert result['n'] == 200
    assert 0 <= result['p_any_late'] <= 1
    assert 0 <= result['p_credit_breach'] <= 1
    metric = 'late_weeks' if template == 'contracting' else 'late_orders'
    assert sum(bin['count'] for bin in result[metric]['hist']) == 200
    if template == 'production':
        assert 0 <= result['fill_rate']['p10'] <= result['fill_rate']['p50'] <= result['fill_rate']['p90'] <= 1
        assert 0 <= result['p_storage_overflow'] <= 1


def test_production_shortfall_can_recover_after_due_day():
    dataset = load_demo('production')
    problem = production.prepare(dataset, load_templates()['production'].model_defaults()['parameters'])
    order = {**problem.orders['O1'], 'due_day': 1}
    problem = replace(problem, orders={'O1': order})
    departures = [[] for _ in problem.days]
    departures[0].append('O1')
    supply = [{} for _ in problem.days]
    supply[2]['S1'] = 2000
    schedule = [{'line': 'L1', 'day': 2, 'sku': 'S1', 'qty': 2000}]
    assert production_draw(problem, schedule, departures, supply, {'O1': 1000})[:2] == (1.0, set())
    assert production_draw(problem, schedule, departures, supply, {'O1': 3000})[:2] == (1.0, {'O1'})
    assert production_draw(problem, schedule, departures, supply, {'O1': 4000})[:2] == (0.0, {'O1'})


@pytest.mark.parametrize('cid,method,hist', [(1, 'lognormal', 'late_weeks'), (2, 'holt_croston', 'late_orders')])
def test_forecast_and_monte_carlo_api(client, cid, method, hist):
    prefix = f'/api/companies/{cid}'
    assert client.get(f'{prefix}/forecast/latest').status_code == 404
    forecast = client.post(f'{prefix}/forecast', json={})
    assert forecast.status_code == 200
    assert forecast.json()['method'] == method
    assert client.get(f'{prefix}/forecast/latest').json() == forecast.json()

    optimized = client.post(f'{prefix}/optimize', json={'time_limit': 2})
    assert optimized.status_code == 200
    run_id = optimized.json()['run_id']
    base = client.get(f'/api/runs/{run_id}').json()
    assert client.put(f'{prefix}/model', json={'parameters': {'credit_limit': 1}}).status_code == 200
    assert client.post(f'{prefix}/datasets/demo').json()['dataset_id'] != base['dataset_id']

    simulation = client.post(f'{prefix}/montecarlo', json={'run_id': run_id})
    assert simulation.status_code == 200
    assert sum(bin['count'] for bin in simulation.json()[hist]['hist']) == 200
    stored = client.get(f'{prefix}/runs/latest?kind=montecarlo').json()
    assert (stored['base_run_id'], stored['dataset_id'], stored['model_id']) == (
        run_id, base['dataset_id'], base['model_id'],
    )
    assert stored['scenario'] == {'seed': 42, 'n': 200}
    assert stored['result'] == simulation.json()
    assert client.post(f'{prefix}/montecarlo', json={'run_id': stored['id']}).status_code == 404
    assert client.post(f'/api/companies/{3 - cid}/montecarlo', json={'run_id': run_id}).status_code == 404


def test_api_validates_input_and_dataset_ownership(client):
    dataset = client.get('/api/companies').json()[1]['active_dataset_id']
    response = client.post('/api/companies/1/forecast', json={'dataset_id': dataset})
    assert response.status_code == 404
    assert response.json()['detail']['code'] == 'dataset_not_found'
    assert client.post('/api/companies/2/forecast', json={'dataset_id': dataset}).status_code == 200
    for invalid in ({'n': 0}, {'seed': -1}, {'run_id': 0}):
        response = client.post('/api/companies/2/montecarlo', json={'run_id': 1, **invalid})
        assert response.status_code == 422
        assert response.json()['detail']['msg_ar']
