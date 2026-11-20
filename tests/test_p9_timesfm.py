import sys
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from conftest import sign_in

from app.config import Settings
from app.db import encode, get_conn, utc_now
from app.forecasting import foundation
from app.forecasting.methods import HOLDOUT
from app.forecasting.service import run_forecast
from app.ingestion.loader import load_demo
from app.main import create_app


def demand_dataset(*histories):
    return {
        'skus': [{'id': key, 'name_ar': key} for key, _ in histories],
        'demand_history': [
            {'sku_id': key, 'week': week, 'qty': value}
            for key, values in histories for week, value in enumerate(values, start=1)
        ],
    }


def bands(value, horizon):
    return [[value - 2] * horizon, [value] * horizon, [value + 2] * horizon]


def test_auto_batches_rolling_contexts_and_picks_per_sku():
    growing = list(range(20, 48))
    stable = [100] * 28
    dataset = demand_dataset(('growth', growing), ('stable', stable))
    worker = Mock()
    worker.forecast.side_effect = lambda inputs, horizon: [
        bands(context[-1] + 1 if context[0] == 20 else 10, horizon) for context in inputs
    ]

    result = run_forecast('production', dataset, foundation=worker)
    worker.forecast.assert_called_once_with([
        values[:end] for values in (growing, stable) for end in range(20, 29)
    ], 4)
    assert result['method'] == 'mixed'
    chosen, rejected = result['series']
    assert (chosen['engine'], chosen['method']) == ('timesfm', 'timesfm')
    assert (chosen['p10'], chosen['p50'], chosen['p90']) == ([46] * 4, [48] * 4, [50] * 4)
    assert chosen['backtest'] == {'mae': 0, 'mape': 0, 'method': 'timesfm', 'n': HOLDOUT, 'mape_n': HOLDOUT}
    assert chosen['candidate_backtests']['stat']['mae'] > 0
    assert (rejected['engine'], rejected['method']) == ('stat', 'holt_damped')
    assert rejected['backtest']['mae'] == 0
    assert rejected['candidate_backtests']['timesfm']['mae'] == 90
    assert rejected['candidate_backtests']['timesfm']['mape'] == 90


def test_timesfm_preference_calibrates_and_clamps_bands():
    worker = Mock()
    worker.forecast.return_value = [bands(1, 4)] * (HOLDOUT + 1)
    result = run_forecast('production', demand_dataset(('S1', [100] * 24)), {'demand_bias.S1': 1.5},
                          engine='timesfm', foundation=worker)
    assert result['method'] == 'timesfm'
    row = result['series'][0]
    assert (row['p10'], row['p50'], row['p90']) == ([0] * 4, [1.5] * 4, [4.5] * 4)
    assert row['backtest']['mae'] == pytest.approx(98.5)


def test_auto_tie_uses_stat_and_mape_ignores_zero_actuals():
    worker = Mock()
    worker.forecast.return_value = [bands(0, 4)] * (HOLDOUT + 1)
    row = run_forecast('production', demand_dataset(('S1', [0] * 24)), foundation=worker)['series'][0]
    assert row['engine'] == 'stat'
    assert row['candidate_backtests']['timesfm']['mape_n'] == 0
    assert row['candidate_backtests']['timesfm']['mae'] == 0


@pytest.mark.parametrize('bad_output', [None, [[], [], []], bands(float('nan'), 4), [[10] * 4, [5] * 4, [15] * 4]])
def test_invalid_bands_only_fall_back_for_the_affected_sku(bad_output):
    worker = Mock()
    worker.forecast.return_value = [bands(10, 4)] * (2 * (HOLDOUT + 1))
    worker.forecast.return_value[HOLDOUT] = bad_output
    dataset = demand_dataset(('S1', [10] * 24), ('S2', [10] * 24))
    result = run_forecast('production', dataset, engine='timesfm', foundation=worker)
    assert [row['engine'] for row in result['series']] == ['stat', 'timesfm']


def test_short_missing_and_duplicate_weeks_stay_statistical():
    worker = Mock()
    dataset = demand_dataset(('short', [10] * 23), ('gap', [10] * 24), ('duplicate', [10] * 24))
    dataset['demand_history'][23]['week'] = 0
    dataset['demand_history'][-1]['week'] = 23
    result = run_forecast('production', dataset, foundation=worker)
    assert result['method'] == 'holt_croston'
    worker.forecast.assert_not_called()


def test_service_skips_worker_in_stat_mode_and_for_contracting():
    worker = Mock()
    for template, engine in [('production', 'stat'), ('contracting', 'timesfm')]:
        result = run_forecast(template, load_demo(template), engine=engine, foundation=worker)
        assert {row['engine'] for row in result['series']} == {'stat'}
    worker.forecast.assert_not_called()


def test_engine_setting_defaults_to_auto(monkeypatch):
    monkeypatch.delenv('FORECAST_ENGINE', raising=False)
    assert Settings().forecast_engine == 'auto'
    monkeypatch.setenv('FORECAST_ENGINE', 'timesfm')
    assert Settings().forecast_engine == 'timesfm'
    monkeypatch.setenv('FORECAST_ENGINE', 'other')
    with pytest.raises(ValueError, match='FORECAST_ENGINE'):
        Settings()


@pytest.mark.parametrize('mode', ['auto', 'timesfm'])
def test_api_silent_fallback_including_latest(db_path, mode, caplog):
    application = create_app(Settings(db_path=db_path, forecast_engine=mode))
    worker = Mock()
    worker.forecast.return_value = None
    application.state.forecast_worker = worker
    with TestClient(application) as client:
        sign_in(client)
        response = client.post('/api/companies/2/forecast')
        assert response.status_code == 200
        assert response.json()['method'] == 'holt_croston'
        assert {row['engine'] for row in response.json()['series']} == {'stat'}
        assert client.get('/api/companies/2/forecast/latest').json() == response.json()
    worker.forecast.assert_called_once()
    assert not caplog.records


def test_api_round_trips_old_stat_and_new_timesfm_forecast(db_path):
    application = create_app(Settings(db_path=db_path, forecast_engine='timesfm'))
    worker = Mock()
    worker.forecast.side_effect = lambda inputs, horizon: [bands(100, horizon) for _ in inputs]
    application.state.forecast_worker = worker
    with TestClient(application) as client:
        sign_in(client)
        old = run_forecast('production', load_demo('production'))
        for row in old['series']:
            del row['engine']
        dataset_id = client.get('/api/companies').json()[1]['active_dataset_id']
        with get_conn(db_path) as connection:
            connection.execute(
                'INSERT INTO forecasts (company_id, dataset_id, method, result_json, created_at) VALUES (2, ?, ?, ?, ?)',
                (dataset_id, old['method'], encode(old), utc_now()),
            )
        old_response = client.get('/api/companies/2/forecast/latest')
        assert old_response.status_code == 200
        assert {row['engine'] for row in old_response.json()['series']} == {'stat'}

        response = client.post('/api/companies/2/forecast')
        assert response.status_code == 200
        assert response.json()['method'] == 'timesfm'
        assert {row['engine'] for row in response.json()['series']} == {'timesfm'}
        assert all(row['candidate_backtests']['timesfm']['n'] == HOLDOUT for row in response.json()['series'])
        assert client.get('/api/companies/2/forecast/latest').json() == response.json()


@pytest.fixture
def worker_environment(tmp_path):
    (tmp_path / 'bin').mkdir()
    (tmp_path / 'bin' / 'python').symlink_to(sys.executable)
    (tmp_path / 'checkpoint').mkdir()
    (tmp_path / 'checkpoint' / 'model.safetensors').touch()
    return tmp_path


def test_worker_reuses_process_and_closes_at_app_shutdown(worker_environment, monkeypatch):
    script = worker_environment / 'worker.py'
    script.write_text('''
import json
import os
import socket
import sys

assert os.environ['HF_HUB_OFFLINE'] == '1'
with socket.socket(fileno=int(sys.argv[1])) as channel, channel.makefile('rb') as reader:
    for line in reader:
        request = json.loads(line)
        reply = [[os.getpid(), values] for values in request['inputs']]
        channel.sendall(json.dumps(reply).encode() + b'\\n')
''')
    monkeypatch.setattr(foundation, 'WORKER_SCRIPT', script)
    worker = foundation.TimesFMWorker(worker_environment, timeout=5)
    monkeypatch.setattr('app.main.TimesFMWorker', lambda: worker)
    with TestClient(create_app(Settings(db_path=worker_environment / 'test.sqlite', forecast_engine='auto'))):
        first = worker.forecast([[1, 2]], 4)
        process = worker._process
        second = worker.forecast([list(range(600)), [3]], 4)
        assert first == [[process.pid, [1, 2]]]
        assert second == [[process.pid, list(range(88, 600))], [process.pid, [3]]]
    assert process.poll() == 0


def test_worker_unavailable_or_crashed(worker_environment, monkeypatch):
    worker = foundation.TimesFMWorker(worker_environment / 'missing')
    assert worker.forecast([[1]], 4) is None
    script = worker_environment / 'broken.py'
    script.write_text('raise SystemExit(1)\n')
    monkeypatch.setattr(foundation, 'WORKER_SCRIPT', script)
    worker = foundation.TimesFMWorker(worker_environment, timeout=5)
    assert worker.forecast([[1]], 4) is None
    assert worker._process is None
    assert worker.forecast([[1]], 4) is None
