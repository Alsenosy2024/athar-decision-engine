"""Out-of-sample scores for rolling forecast windows."""
import numpy as np

from app.forecasting.methods import backtest


def score(actual, bands, scales, name):
    point = bands[:, 1]
    error = np.abs(actual - point)
    denominator = np.abs(actual) + np.abs(point)
    relative = np.divide(error, denominator, out=np.zeros_like(error), where=denominator > 0)
    scaled = scales > 0
    low, high = bands[:, 0], bands[:, 2]
    covered = (actual >= low) & (actual <= high)
    width = high - low
    interval_loss = width + 10 * (np.maximum(low - actual, 0) + np.maximum(actual - high, 0))
    return backtest(actual.ravel(), point.ravel(), name) | {
        'mase': float(np.mean(error[scaled] / scales[scaled, None])) if scaled.any() else None,
        'mase_n': int(scaled.sum() * actual.shape[1]),
        'smape': float(200 * relative.mean()),
        'coverage': float(covered.mean()),
        'coverage_by_horizon': covered.mean(axis=0).tolist(),
        'mean_width': float(width.mean()),
        'interval_score': float(interval_loss.mean()),
    }


def scale(context, period):
    values = np.asarray(context)
    return float(np.mean(np.abs(values[period:] - values[:-period]))) if len(values) > period else 0.0


def seasonal_period(context, frequency):
    """Require repeated seasonal structure beyond a locally linear trend."""
    values = np.asarray(context)
    for period in ((52, 4) if frequency == 'weekly' else (7,)):
        if len(values) < 3 * period:
            continue
        changes = np.diff(values)
        naive = np.mean(np.abs(changes[period:] - np.median(changes)))
        repeated = np.mean(np.abs(changes[period:] - changes[:-period]))
        if naive > 1e-9 and repeated < 0.65 * naive:
            return period
    return 1


def seasonal_naive(context, horizon, period, nonnegative):
    values = np.asarray(context)
    point = np.resize(values[-period:], horizon)
    error = values[period:] - values[:-period]
    low, high = np.quantile(error, [0.1, 0.9])
    bands = np.array([point + min(low, 0), point, point + max(high, 0)])
    return np.maximum(0, bands) if nonnegative else bands


def improve(challenger, baseline):
    """Require a point improvement and no regression in interval quality."""
    metrics = ('mae', 'smape') + (('mase',) if baseline['mase'] is not None else ())
    return (all(challenger[key] <= baseline[key] + 1e-9 for key in metrics)
            and any(challenger[key] < baseline[key] - 1e-9 for key in metrics)
            and challenger['interval_score'] <= baseline['interval_score'] + 1e-9
            and abs(challenger['coverage'] - 0.8) <= abs(baseline['coverage'] - 0.8) + 1e-9)


def seasonal_ets(context, horizon, period, nonnegative):
    from statsmodels.tsa.holtwinters import ExponentialSmoothing

    values = np.asarray(context)
    fit = ExponentialSmoothing(values, trend='add', damped_trend=True,
                               seasonal='add', seasonal_periods=period).fit()
    point = fit.forecast(horizon)
    low, high = np.quantile(fit.resid[period:], [0.1, 0.9])
    bands = np.array([point + min(low, 0), point, point + max(high, 0)])
    return np.maximum(0, bands) if nonnegative else bands
