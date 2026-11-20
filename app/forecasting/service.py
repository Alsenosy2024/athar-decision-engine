from collections import defaultdict

import numpy as np

from app.forecasting.foundation import MAX_CONTEXT
from app.forecasting.methods import HOLDOUT, Z90, backtest, demand_forecast, duration_backtest, lognormal_fit

PHASE_NAMES = {'F': 'أساسات', 'S': 'هيكل', 'X': 'تشطيبات'}
DEMAND_HORIZON = 4
MIN_TIMESFM_TRAINING_WEEKS = 16
BAND_KEYS = ('p10', 'p50', 'p90')


def contracting_forecast(dataset, calibrations):
    history = defaultdict(list)
    for row in dataset.get('history', []):
        history[row['phase']].append(row)
    phases = {}
    scores = {}
    series = []
    for activity in dataset['activities']:
        phase = activity['phase']
        rows = history[phase]
        if phase not in phases:
            planned = np.array([row['planned_weeks'] for row in rows])
            actual = np.array([row['actual_weeks'] for row in rows])
            mu, sigma = lognormal_fit(planned, actual)
            calibration = calibrations.get(f'dur_ratio.{phase}')
            source = 'history' if len(rows) >= 5 else 'default'
            if calibration is not None:
                mu, sigma = calibration['mu'], calibration['sigma']
                source = 'calibration'
            phases[phase] = {
                'mu': mu, 'sigma': sigma, 'n': len(rows) if calibration is None else calibration.get('n', len(rows)),
                'source': source,
                'source_ar': {'history': 'السجل التاريخي', 'default': 'افتراضي',
                              'calibration': 'المعايرة'}[source],
            }
            scores[phase] = duration_backtest(planned, actual)
        mu, sigma = phases[phase]['mu'], phases[phase]['sigma']
        bands = activity['duration_weeks'] * np.exp(mu + Z90 * sigma * np.array([-1, 0, 1]))
        key = activity['id']
        series.append({
            'key': key,
            'name_ar': f"{PHASE_NAMES.get(phase, phase)} — {activity['project_id']}",
            'engine': 'stat',
            'phase': phase,
            'history': [row['actual_weeks'] for row in rows],
            **dict(zip(('p10', 'p50', 'p90'), ([value] for value in bands.round(1).tolist()))),
            'backtest': scores[phase],
        })
    return {
        'method': 'lognormal',
        'series': series,
        'phases': phases,
        'summary_ar': f'المدد المتوقعة لـ {len(series)} نشاطًا بالأسابيع اعتمادًا على بيانات كل مرحلة.',
    }


def compare_timesfm(series, calibrations, engine, foundation):
    eligible = []
    contexts = []
    for row in series:
        values = row['history']
        weeks = row['history_weeks']
        # TimesFM expects regularly spaced inputs. Missing rows are not zero demand.
        if len(values) < MIN_TIMESFM_TRAINING_WEEKS + HOLDOUT:
            continue
        if any(stop - start != 1 for start, stop in zip(weeks, weeks[1:])):
            continue
        eligible.append(row)
        for end in range(len(values) - HOLDOUT, len(values) + 1):
            contexts.append(values[max(0, end - MAX_CONTEXT):end])

    if not contexts:
        return
    results = foundation.forecast(contexts, DEMAND_HORIZON)
    if results is None:
        return

    for index, row in enumerate(eligible):
        start = index * (HOLDOUT + 1)
        try:
            bands = np.asarray(results[start:start + HOLDOUT + 1], dtype=float)
            if bands.shape != (HOLDOUT + 1, len(BAND_KEYS), DEMAND_HORIZON) or not np.isfinite(bands).all():
                continue
            bands = np.maximum(0, bands * calibrations.get(f"demand_bias.{row['key']}", 1.0))
            if np.any(bands[:, :-1] > bands[:, 1:]):
                continue
            # Each holdout prediction sees only data available before that week.
            scores = backtest(np.asarray(row['history'][-HOLDOUT:]), bands[:-1, 1, 0], 'timesfm')
            if not all(np.isfinite(scores[key]) for key in ('mae', 'mape')):
                continue
        except (ValueError, TypeError, FloatingPointError, OverflowError):
            continue

        stat_scores = row['backtest']
        row['candidate_backtests'] = {'stat': stat_scores, 'timesfm': scores}
        timesfm_error = (scores['mae'], scores['mape'])
        stat_error = (stat_scores['mae'], stat_scores['mape'])
        if engine == 'timesfm' or timesfm_error < stat_error:
            row.update(zip(BAND_KEYS, bands[-1].tolist()))
            row.update(engine='timesfm', method='timesfm', backtest=scores)


def production_forecast(dataset, calibrations, engine='auto', foundation=None):
    history = defaultdict(list)
    for row in dataset.get('demand_history', []):
        history[row['sku_id']].append(row)
    series = []
    for sku in dataset['skus']:
        key = sku['id']
        weekly = sorted(history[key], key=lambda row: row['week'])
        values = [row['qty'] for row in weekly]
        method, bands, scores = demand_forecast(values, DEMAND_HORIZON, calibrations.get(f'demand_bias.{key}', 1.0))
        series.append({
            'key': key,
            'name_ar': sku['name_ar'],
            'history': values,
            'history_weeks': [row['week'] for row in weekly],
            'engine': 'stat',
            'method': method,
            **dict(zip(BAND_KEYS, bands)),
            'backtest': scores,
        })
    if engine != 'stat' and foundation is not None:
        compare_timesfm(series, calibrations, engine, foundation)

    summary = f'الطلب الأسبوعي المتوقع لـ {len(series)} منتجات'
    if series:
        widest = max(series, key=lambda item: sum(hi - lo for lo, hi in zip(item['p10'], item['p90'])))
        summary += f"؛ أعلى عدم يقين في {widest['name_ar']}"
    engines = {row['engine'] for row in series}
    method = 'holt_croston'
    if 'timesfm' in engines:
        method = 'mixed' if 'stat' in engines else 'timesfm'
    return {'method': method, 'series': series, 'summary_ar': summary + '.'}


def run_forecast(template, dataset, calibrations=None, *, engine='auto', foundation=None):
    if template == 'contracting':
        return contracting_forecast(dataset, calibrations or {})
    if template == 'production':
        return production_forecast(dataset, calibrations or {}, engine, foundation)
    raise KeyError(template)
