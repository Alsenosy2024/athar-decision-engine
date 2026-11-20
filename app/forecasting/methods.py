import numpy as np

Z90 = 1.2816
HOLDOUT = 8


def lognormal_fit(planned, actual):
    logs = np.log(actual) - np.log(planned)
    if len(logs) < 5:
        return 0.0, 0.15
    return float(logs.mean()), float(logs.std(ddof=1))


def backtest(actual, predicted, method):
    errors = np.abs(actual - predicted)
    positive = actual > 0
    return {
        'mae': float(errors.mean()) if len(errors) else 0.0,
        'mape': float(100 * np.mean(errors[positive] / actual[positive])) if positive.any() else 0.0,
        'method': method,
        'n': len(actual),
        'mape_n': int(positive.sum()),
    }


def duration_backtest(planned, actual):
    logs = np.log(actual) - np.log(planned)
    # Each fit sees one fewer record than the full phase fit.
    mu = (logs.sum() - logs) / (len(logs) - 1) if len(logs) > 5 else 0.0
    return backtest(actual, planned * np.exp(mu), 'leave_one_out')


def ses_holt_damped(values, horizon, alpha=0.3, beta=0.1, phi=0.9):
    level, trend = (float(values[0]) if len(values) else 0.0), 0.0
    predictions = [0.0] if len(values) else []
    for value in values[1:]:
        next_level = level + phi * trend
        predictions.append(max(0.0, next_level))
        next_level += alpha * (value - next_level)
        trend = beta * (next_level - level) + (1 - beta) * phi * trend
        level = next_level
    damping = np.cumsum(phi ** np.arange(1, horizon + 1))
    return np.maximum(0, level + trend * damping), np.asarray(predictions)


def croston_sba(values, horizon, alpha=0.1):
    size, interval, elapsed = 0.0, 0.0, 1
    forecast = 0.0
    predictions = []
    for value in values:
        predictions.append(forecast)
        if value > 0:
            if interval:
                size += alpha * (value - size)
                interval += alpha * (elapsed - interval)
            else:
                size, interval = value, elapsed
            forecast = (1 - alpha / 2) * size / interval
            elapsed = 0
        elapsed += 1
    return np.full(horizon, forecast), np.asarray(predictions)


def holt_alpha(values):
    split = max(1, len(values) - HOLDOUT)
    if len(values) <= split:
        return 0.3
    # The rolling forecasts at each index only see observations before it.
    candidates = (0.3, 0.1, 0.2, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9)
    return min(candidates, key=lambda alpha: np.mean(np.abs(
        values[split:] - ses_holt_damped(values, 0, alpha)[1][split:]
    )))


def is_intermittent(values):
    return len(values) > 0 and np.mean(values == 0) >= 0.30


def demand_forecast(values, horizon, bias):
    values = np.asarray(values, dtype=float)
    split = max(1, len(values) - HOLDOUT)
    train = values[:split]

    # Tune and select on the training prefix for scoring. Refit the published
    # forecast using all observations, including the most recent eight weeks.
    if is_intermittent(train):
        holdout_method = 'croston_sba'
        _, holdout_predictions = croston_sba(values, 0)
    else:
        holdout_method = 'holt_damped'
        _, holdout_predictions = ses_holt_damped(values, 0, holt_alpha(train))
    scores = backtest(values[split:], holdout_predictions[split:] * bias, holdout_method)

    if is_intermittent(values):
        method = 'croston_sba'
        median, fitted = croston_sba(values, horizon)
    else:
        method = 'holt_damped'
        median, fitted = ses_holt_damped(values, horizon, holt_alpha(values))
    residuals = values[1:] - fitted[1:]
    if len(residuals) >= 20:
        low_error, high_error = np.quantile(residuals, [0.1, 0.9])
    else:
        sigma = residuals.std(ddof=1) if len(residuals) >= 2 else 0.0
        high_error = Z90 * sigma * np.sqrt(np.arange(1, horizon + 1))
        low_error = -high_error
    bands = (
        np.clip(median + low_error, 0, median),
        median,
        np.maximum(median, median + high_error),
    )
    return method, [(band * bias).tolist() for band in bands], scores


def quantiles(values):
    return dict(zip(('p10', 'p50', 'p90'), np.quantile(values, [0.1, 0.5, 0.9]).tolist()))
