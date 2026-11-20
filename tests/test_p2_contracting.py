from collections import defaultdict

import pytest

from app.ingestion.loader import load_demo
from app.optimization.contracting import solve, solve_demo


@pytest.fixture(scope='module')
def demo():
    return load_demo('contracting')


@pytest.fixture(scope='module')
def comparison():
    return solve_demo()


def test_reference_baseline(comparison):
    baseline = comparison['baseline']['kpis']
    assert baseline['total'] == 1706.3
    assert baseline['ld'] == 1325
    assert baseline['late'] == {'A': 0, 'B': 0, 'C': 11, 'D': 12, 'E': 15}


def test_reference_optimum(comparison):
    assert comparison['solver']['status'] in ('OPTIMAL', 'FEASIBLE')
    optimized = comparison['optimized']['kpis']
    assert optimized['total'] <= 1100
    assert optimized['total'] <= comparison['baseline']['kpis']['total']
    assert optimized['total'] == pytest.approx(1018.9, abs=0.1)


@pytest.mark.parametrize('variant', ('baseline', 'optimized'))
def test_credit_ceiling(comparison, variant):
    kpis = comparison[variant]['kpis']
    assert kpis['credit_ok']
    assert kpis['breach'] == 0
    assert kpis['peak_od'] == -min(kpis['cash_curve']) <= 4000


@pytest.mark.parametrize('variant', ('baseline', 'optimized'))
def test_release_and_precedence(demo, comparison, variant):
    starts = comparison[variant]['kpis']['starts']
    finishes = comparison[variant]['kpis']['finishes']
    chains = defaultdict(list)
    for activity in demo['activities']:
        chains[activity['project_id']].append(activity)
        assert finishes[activity['id']] - starts[activity['id']] == activity['duration_weeks']
    for project in demo['projects']:
        chain = sorted(chains[project['id']], key=lambda activity: activity['seq'])
        assert starts[chain[0]['id']] >= project['nta_week']
        for previous, activity in zip(chain, chain[1:]):
            assert finishes[previous['id']] <= starts[activity['id']]
    assert max(finishes.values()) <= 44


def assert_weekly_resources(dataset, variant, overrides=None):
    plan = variant['plan']
    usage = {row['id']: [0] * 44 for row in dataset['resources']}
    for activity in dataset['activities']:
        for week in range(variant['kpis']['starts'][activity['id']],
                          variant['kpis']['finishes'][activity['id']]):
            usage[activity['resource']][week] += activity['units']
    assert usage == plan['resource_usage']
    for resource in dataset['resources']:
        resource_id = resource['id']
        for week, used in enumerate(usage[resource_id]):
            cap = (overrides or {}).get(resource_id, {}).get(week, resource['capacity'])
            assert used <= cap == plan['capacity'][resource_id][week]


@pytest.mark.parametrize('variant', ('baseline', 'optimized'))
def test_resource_capacity(demo, comparison, variant):
    assert_weekly_resources(demo, comparison[variant])


def test_wall_time(comparison):
    assert 0 < comparison['solver']['wall_s'] < 20


@pytest.mark.parametrize('limit', (1, 1e-9))
def test_baseline_hint_and_cutoff_fallback(limit):
    result = solve_demo(time_limit=limit)
    assert result['solver']['status'] in ('FEASIBLE', 'OPTIMAL')
    assert result['optimized']['kpis']['total'] <= result['baseline']['kpis']['total']
    assert result['optimized']['kpis']['credit_ok']


def test_financing_weight(comparison):
    result = solve_demo(weights={'ld': 0, 'financing': 1})
    assert result['optimized']['kpis']['interest'] <= comparison['optimized']['kpis']['interest']


def test_time_varying_capacity(demo):
    overrides = {'R2': {week: 2 for week in range(5, 9)}}
    result = solve_demo(parameters={'capacity_override': overrides})
    assert result['solver']['status'] in ('OPTIMAL', 'FEASIBLE')
    for variant in ('baseline', 'optimized'):
        assert_weekly_resources(demo, result[variant], overrides)


def tiny_dataset():
    return {
        'projects': [{'id': 'A', 'nta_week': 0, 'deadline_week': 1, 'ld_per_week': 10}],
        'activities': [{'id': 'work', 'project_id': 'A', 'seq': 1, 'phase': 'F',
                        'duration_weeks': 1, 'resource': 'R', 'units': 1, 'cost_per_week': 100}],
        'resources': [{'id': 'R', 'capacity': 1}],
    }


def test_unavoidable_credit_breach_and_payment_after_horizon():
    result = solve(tiny_dataset(), {'horizon_weeks': 1, 'payment_lag_weeks': 2, 'credit_limit': 65})
    assert result['solver']['status'] == 'OPTIMAL'
    for variant in ('baseline', 'optimized'):
        kpis = result[variant]['kpis']
        assert not kpis['credit_ok']
        assert kpis['breach'] == 35
        assert kpis['cash_curve'] == [-100, -100, -100, 15]
        assert kpis['interest'] == 1.2


def test_solver_can_find_plan_when_serial_baseline_cannot():
    dataset = tiny_dataset()
    dataset['projects'].append({'id': 'B', 'nta_week': 0, 'deadline_week': 2, 'ld_per_week': 10})
    dataset['activities'].append({**dataset['activities'][0], 'id': 'work-B', 'project_id': 'B',
                                  'duration_weeks': 2})
    result = solve(dataset, {'horizon_weeks': 4, 'capacity_override': {'R': {2: 0}}})
    assert result['baseline'] == {'kpis': {}, 'plan': {}}
    assert result['solver']['status'] == 'OPTIMAL'
    assert result['optimized']['kpis']['starts'] == {'work': 3, 'work-B': 0}


def test_hard_infeasibility_returns_status_without_schedule():
    result = solve(tiny_dataset(), {'horizon_weeks': 1, 'capacity_override': {'R': {0: 0}}})
    assert result['solver']['status'] == 'INFEASIBLE'
    assert result['optimized'] == {'kpis': {}, 'plan': {}}
    assert result['solver']['objective'] is None


def test_optimize_and_read_run(client):
    model = client.get('/api/companies/1/model').json()
    dataset = client.get('/api/companies/1/datasets').json()[0]
    response = client.post('/api/companies/1/optimize', json={'time_limit': 1})
    assert response.status_code == 200
    result = response.json()
    assert result['explanation_ar'].strip()
    assert result['baseline']['kpis']['total'] == 1706.3
    assert result['optimized']['kpis']['total'] <= 1706.3
    run_id = result.pop('run_id')
    direct = client.get(f'/api/runs/{run_id}')
    latest = client.get('/api/companies/1/runs/latest?kind=optimize')
    assert direct.status_code == latest.status_code == 200
    run = direct.json()
    assert run == latest.json()
    assert run['id'] == run_id
    assert run['model_id'] == model['id']
    assert run['dataset_id'] == dataset['id']
    assert run['result'] == result
    assert run['status'] == result['solver']['status']
    assert run['wall_s'] == result['solver']['wall_s']


def test_explicit_dataset_and_model_version(client):
    original = client.get('/api/companies/1/datasets').json()[0]['id']
    assert client.post('/api/companies/1/datasets/demo').json()['dataset_id'] != original
    model = client.put('/api/companies/1/model', json={'weights': {'ld': 0}}).json()
    response = client.post('/api/companies/1/optimize', json={
        'dataset_id': original, 'weights': {'financing': 0, 'makespan': 0}, 'time_limit': 1,
    })
    assert response.status_code == 200
    result = response.json()
    assert result['solver']['objective'] == 0
    run = client.get(f"/api/runs/{result['run_id']}").json()
    assert run['model_id'] == model['id']
    assert run['dataset_id'] == original


@pytest.mark.parametrize(('method', 'path', 'body', 'status', 'code'), [
    ('POST', '/api/companies/999/optimize', {}, 404, 'company_not_found'),
    ('POST', '/api/companies/1/optimize', {'dataset_id': 2}, 404, 'dataset_not_found'),
    ('POST', '/api/companies/1/optimize', {'weights': {'ld': 8}}, 422, 'invalid_weight'),
    ('POST', '/api/companies/1/optimize', {'time_limit': 0}, 422, 'invalid_request'),
    ('GET', '/api/runs/999', None, 404, 'run_not_found'),
    ('GET', '/api/companies/1/runs/latest?kind=whatif', None, 404, 'run_not_found'),
])
def test_api_errors(client, method, path, body, status, code):
    response = client.request(method, path, json=body)
    assert response.status_code == status
    assert response.json()['detail']['code'] == code
    assert response.json()['detail']['msg_ar']
