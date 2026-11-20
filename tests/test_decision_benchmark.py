from itertools import combinations

import pytest

from app.decision_loop import benchmark, learning, production_case as case, routes


@pytest.fixture(scope='module')
def report():
    return benchmark.run_benchmark()


def test_p0c_dated_inputs_exclude_future_and_freeze_bookings():
    supply, docket, history, parameters, weights = case.demo_case()
    cutoff = case.FIRST_TEST
    inputs = case.dated_inputs(history, docket, cutoff)
    changed = {key: value if key[0] < cutoff else value + 1_000 for key, value in history.items()}
    assert case.dated_inputs(changed, docket, cutoff) == inputs
    assert inputs['last_observed_week'] < inputs['cutoff_exclusive'] == inputs['outcome_window'][0]
    for demand in (*inputs['candidate_totals'].values(), *inputs['draw_totals']):
        problem = case.prepare(supply, docket, parameters, inputs['confirmed_units'], demand)
        for sku, booked, total in zip(case.PRODUCTS, inputs['confirmed_units'], demand):
            assert problem.orders[f'{sku}-confirmed']['qty'] == booked
            assert sum(row['qty'] for row in problem.orders.values() if row['sku_id'] == sku) == total


def test_p0c_replay_pairing_and_reference(report):
    rows, manifest = report['windows'], report['manifest']
    assert len(rows) == 16
    assert report['replay'] == dict(training_matched=20, training_windows=20,
                                    evaluation_matched=16, evaluation_windows=16, coefficients_matched=True)
    for previous, following in zip(rows, rows[1:]):
        assert previous['inputs']['outcome_window'][1] == following['inputs']['outcome_window'][0]
    for row in rows:
        assert row['input_sha256'] == benchmark.fingerprint([manifest, row['inputs']])
        assert row['oracle']['status'] == 'OPTIMAL'
        for policy in row['policies'].values():
            candidate = policy['candidate']
            draws = row['draws'][candidate]
            assert len(draws) == manifest['draws_per_window']
            assert all(all(draw['hard_checks'].values()) for draw in draws)
            assert all(policy['realized']['hard_checks'].values())
            assert policy['realized']['schedule'] == row['planning'][candidate]['schedule']
            assert policy['regret_sar'] == pytest.approx(policy['realized']['cost_sar'] - row['oracle']['cost_sar'])
            assert policy['regret_bounds_sar'] == [policy['regret_sar']] * 2
    pairs = {(pair['reference'], pair['challenger']): pair for pair in report['comparisons']}
    assert set(pairs) == set(combinations((*benchmark.POLICIES, benchmark.EXPERIMENT), 2))
    for (reference, challenger), pair in pairs.items():
        assert pair['paired_windows'] == len(rows)
        assert len(pair['ci95_sar']) == 2
        expected = report['summary'][reference]['mean_regret_sar'] - report['summary'][challenger]['mean_regret_sar']
        assert pair['mean_improvement_sar'] == pytest.approx(expected)
    for summary in report['summary'].values():
        assert summary['feasible_pct'] == 100
        assert summary['credit_breach_sar'] == 0
        assert 0 < summary['latency_p95_s']
    assert case.cvar([10, 20, 30, 40], 0.625) == pytest.approx(110 / 3)


def test_p0c_endpoint_company_role_and_detail(client, monkeypatch, report):
    monkeypatch.setattr(routes, '_demo_report', None)
    calls = []

    def measure():
        calls.append(True)
        return report

    monkeypatch.setattr(routes, 'run_benchmark', measure)
    url = '/api/companies/2/ai/decision-benchmark'
    assert client.post(url, headers={'X-CSRF-Token': 'wrong'}).status_code == 403
    assert client.post('/api/companies/1/ai/decision-benchmark').status_code == 422
    assert client.post('/api/companies/999/ai/decision-benchmark').status_code == 404
    overview = client.post(url)
    assert overview.status_code == 200
    assert 'windows' not in overview.json()
    detail = client.post(url, params={'detail': True})
    assert detail.status_code == 200
    assert detail.json()['replay'] == report['replay']
    assert len(detail.json()['windows']) == 16
    assert calls == [True]


def test_p2b_spo_plus_gate_and_frozen_training(report):
    experiment = report['experiment']
    assert experiment['method'] == 'spo_plus'
    assert experiment['training_examples'] == 20
    assert max(row['week'] for row in experiment['training_replay']) < report['windows'][0]['inputs']['week']
    assert experiment['coefficients'] != list(learning.INITIAL_WEIGHTS)
    summary = report['summary']
    best = min(benchmark.POLICIES, key=lambda name: summary[name]['mean_regret_sar'])
    assert experiment['strongest_baseline'] == best
    expected = 100 * (1 - summary['spo_plus']['mean_regret_sar'] / summary[best]['mean_regret_sar'])
    assert experiment['regret_improvement_pct'] == pytest.approx(expected)
    assert experiment['checks']['minimum_regret_improvement'] == (expected >= 5)
    assert not experiment['enabled_by_default']


@pytest.mark.parametrize(('regret', 'lower_ci', 'expected'), [
    (96, 0.5, False), (95, 0.0, False), (95, 0.5, True),
])
def test_p2b_gate_requires_threshold_and_positive_paired_ci(regret, lower_ci, expected):
    shared = dict(service_pct=90, cvar90_realized_sar=80, eligible_windows=16)
    result = learning.assess(shared | {'mean_regret_sar': regret}, shared | {'mean_regret_sar': 100},
                             {'paired_windows': 16, 'ci95_sar': [lower_ci, 10]},
                             expected_windows=16, replay_ok=True, all_eligible=True)
    assert result['passed'] is expected
