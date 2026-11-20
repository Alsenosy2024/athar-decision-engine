"""Rolling-origin model comparison and horizon-specific split conformal intervals."""
from datetime import timedelta
from pathlib import Path
from warnings import catch_warnings, simplefilter

import numpy as np
from statsmodels.tools.sm_exceptions import ConvergenceWarning

from app.config import ROOT
from app.forecasting.foundation import MAX_CONTEXT, TimesFMWorker
from app.forecasting.methods import croston_sba, holt_alpha, is_intermittent, ses_holt_damped
from app.intelligence import forecast_metrics

SELECTION = 8
CALIBRATION = 14
EVALUATION = 6
MIN_TRAIN = 16
COVERAGE = 0.8


class ChronosWorker(TimesFMWorker):
    worker_script = Path(__file__).with_name('chronos_worker.py').resolve()

    def __init__(self, environment=None, timeout=60):
        super().__init__(environment or ROOT / '.venv-chronos', timeout)


def stat_prediction(context, horizon, nonnegative):
    values = np.asarray(context, dtype=float)
    if nonnegative:
        if is_intermittent(values):
            point, _ = croston_sba(values, horizon)
        else:
            point, _ = ses_holt_damped(values, horizon, holt_alpha(values))
    else:
        change = np.median(np.diff(values[-9:])) if len(values) > 1 else 0
        point = values[-1] + change * np.cumsum(0.85 ** np.arange(1, horizon + 1))
    residuals = np.diff(values)
    spread = float(np.quantile(np.abs(residuals), COVERAGE)) if residuals.size else 0
    spread = spread * np.sqrt(np.arange(1, horizon + 1))
    bands = np.array([point - spread, point, point + spread])
    return np.maximum(0, bands) if nonnegative else bands


def worker_predictions(worker, contexts, horizon, nonnegative):
    if worker is None:
        return None
    try:
        bands = np.asarray(worker.forecast(contexts, horizon), dtype=float)
    except (OSError, TypeError, ValueError, RuntimeError):
        return None
    if bands.shape != (len(contexts), 3, horizon) or not np.isfinite(bands).all():
        return None
    if np.any(bands[:, :-1] > bands[:, 1:]):
        return None
    if isinstance(nonnegative, (list, np.ndarray)):
        return np.where(np.asarray(nonnegative)[:, None, None], np.maximum(0, bands), bands)
    return np.maximum(0, bands) if nonnegative else bands


def conformal_radius(residuals):
    from mapie.regression import SplitConformalRegressor
    from sklearn.dummy import DummyRegressor

    columns = np.zeros((len(residuals), 1))
    estimator = DummyRegressor(strategy='constant', constant=0).fit([[0]], [0])
    conformal = SplitConformalRegressor(estimator, confidence_level=COVERAGE)
    conformal.conformalize(columns, residuals)
    _, interval = conformal.predict_interval([[0]])
    return float(interval[0, 1, 0])


def ready_history(points, frequency):
    days = 7 if frequency == 'weekly' else 1
    step = timedelta(days=days)
    tail = []
    for point in reversed(points):
        if tail and tail[-1].date - point.date != step:
            break
        tail.append(point)
    return list(reversed(tail)), step


def ensemble_forecast(node, horizon, timesfm=None, chronos=None):
    points, step = ready_history(node.baseline.points, node.baseline.frequency)
    values = np.asarray([point.value for point in points], dtype=float)
    size = len(values)
    calibration_start = size - EVALUATION - CALIBRATION - 2 * (horizon - 1)
    selection_end = calibration_start - (horizon - 1)
    selection_start = max(MIN_TRAIN, selection_end - SELECTION)
    full_history = selection_end - selection_start >= 4
    if full_history:
        selection = list(range(selection_start, selection_end))
        calibration = list(range(calibration_start, calibration_start + CALIBRATION))
        evaluation = list(range(size - EVALUATION - horizon + 1, size - horizon + 1))
    else:
        selection = list(range(max(MIN_TRAIN, size - horizon + 1 - SELECTION), size - horizon + 1))
        calibration = evaluation = []
    origins = [*selection, *calibration, *evaluation, size]
    contexts = [values[max(0, index - MAX_CONTEXT):index].tolist() for index in origins]
    positive = [node.type not in {'cash', 'profit_loss'} and min(context) >= 0 for context in contexts]
    predictions = {'stat': np.asarray([stat_prediction(context, horizon, nonnegative)
                                       for context, nonnegative in zip(contexts, positive)])}
    period = forecast_metrics.seasonal_period(contexts[0], node.baseline.frequency)
    unavailable = []
    if period > 1:
        for name, method in (('seasonal_naive', forecast_metrics.seasonal_naive),
                             ('seasonal_ets', forecast_metrics.seasonal_ets)):
            try:
                with catch_warnings():
                    simplefilter('error', ConvergenceWarning)
                    guessed = np.asarray([method(context, horizon, period, nonnegative)
                                          for context, nonnegative in zip(contexts, positive)])
                if not np.isfinite(guessed).all():
                    raise ValueError('Nonfinite seasonal forecast')
            except (ValueError, np.linalg.LinAlgError, ConvergenceWarning):
                unavailable.append(name)
            else:
                predictions[name] = guessed
    if selection:
        for name, worker in (('timesfm', timesfm), ('chronos_bolt', chronos)):
            result = worker_predictions(worker, contexts, horizon, positive)
            if result is not None:
                predictions[name] = result
            else:
                unavailable.append(name)
    if 'timesfm' in predictions and 'chronos_bolt' in predictions:
        predictions['ensemble'] = (predictions['timesfm'] + predictions['chronos_bolt']) / 2

    def measure(indices, guessed, name):
        actual = np.asarray([values[index:index + horizon] for index in indices])
        scales = np.asarray([forecast_metrics.scale(values[:index], period) for index in indices])
        return forecast_metrics.score(actual, guessed, scales, name)

    first = len(selection)
    end = first + len(calibration)
    scores = {}
    final = {}
    for name, guessed in predictions.items():
        final[name] = guessed[-1].copy()
        if not selection:
            continue
        scores[name] = measure(selection, guessed[:first], name)
        if not evaluation:
            continue
        actual = np.asarray([values[index:index + horizon] for index in calibration])
        residuals = actual - guessed[first:end, 1]
        radius = np.asarray([conformal_radius(column) for column in residuals.T])
        held_out = guessed[end:-1].copy()
        calibrated = held_out.copy()
        calibrated[:, 0] = calibrated[:, 1] - radius
        calibrated[:, 2] = calibrated[:, 1] + radius
        calibrated = np.where(np.asarray(positive[end:-1])[:, None, None], np.maximum(calibrated, 0), calibrated)
        final[name][0] = final[name][1] - radius
        final[name][2] = final[name][1] + radius
        scores[name]['holdout_raw'] = measure(evaluation, held_out, name)
        scores[name]['holdout'] = measure(evaluation, calibrated, name)
    if evaluation:
        candidates = [name for name in scores if name != 'stat'
                      and forecast_metrics.improve(scores[name], scores['stat'])]
        candidate = min(candidates, key=lambda name: (scores[name]['mae'], scores[name]['smape']), default='stat')
        engine = (candidate if forecast_metrics.improve(scores[candidate]['holdout'], scores['stat']['holdout'])
                  else 'stat')
    else:
        engine = 'stat'
    bands = np.maximum(0, final[engine]) if positive[-1] else final[engine]
    empirical = scores[engine]['holdout']['coverage_by_horizon'] if evaluation else []
    return {
        'node_id': node.id, 'name_ar': node.name_ar, 'unit': node.unit, 'engine': engine,
        'history_count': size, 'backtests': scores,
        'model_calibration': {'selection_origins': len(selection), 'scale_period': period,
                              'unavailable': unavailable, 'holdout_start': points[evaluation[0]].date.isoformat()
                              if evaluation else None},
        'confidence': COVERAGE if calibration else None,
        'interval_method': 'mapie_split_conformal' if calibration else 'uncalibrated',
        'calibration_n': len(calibration), 'evaluation_n': len(evaluation), 'evaluation_coverage': empirical,
        'points': [{'date': (points[-1].date + step * (index + 1)).isoformat(),
                    **dict(zip(('p10', 'p50', 'p90'), bands[:, index].tolist()))}
                   for index in range(horizon)],
    }
