"""Fit bounded edge responses from aligned historical changes."""
from datetime import timedelta

import numpy as np

from app.enterprise.impact_graph import reference
from app.enterprise.schema import EdgeRule, Series

MIN_PAIRS = 24
MAX_LAG = 8
MIN_SCORE = 0.20


def regression(x, y, limit):
    centered = x - x.mean()
    scale = np.linalg.norm(centered)
    if scale <= 1e-10:
        return None
    slope = float(np.clip(np.dot(centered / scale, y - y.mean()) / scale, -limit, limit))
    return slope, y.mean() - slope * x.mean()


def skill(x, y, fit, benchmark):
    error = np.linalg.norm(y - (fit[0] * x + fit[1]))
    null_error = np.linalg.norm(y - benchmark)
    return float(1 - (error / null_error) ** 2) if null_error > 1e-10 else None


def fit_rule(source, target, default, max_lag):
    if not isinstance(source.baseline, Series) or not isinstance(target.baseline, Series):
        return None, 'missing_history'
    if source.baseline.frequency != target.baseline.frequency:
        return None, 'mixed_frequency'
    x_ref, y_ref = abs(reference(source)), abs(reference(target))
    if default.mode == 'elasticity' and (not x_ref or not y_ref):
        return None, 'zero_reference'

    limit = 10 if default.mode == 'elasticity' else min(1e12, 10 * max(1, abs(default.multiplier)))
    # Compare lags on the same target dates. Fall back to shorter lag ranges
    # when long lags or gaps would leave too few observations.
    offsets = {lag: aligned_by_date(source.baseline, target.baseline, lag) for lag in range(max_lag + 1)}
    dates = None
    admissible = []
    for lag, rows in offsets.items():
        common = set(rows) if dates is None else dates & rows.keys()
        if len(common) >= MIN_PAIRS:
            dates = common
            admissible.append(lag)
    dates = sorted(dates or ())
    training_end, selection_end = len(dates) * 3 // 5, len(dates) * 4 // 5
    results = []
    for lag in admissible:
        x, y = np.asarray([offsets[lag][day] for day in dates]).T
        if default.mode == 'elasticity':
            x, y = x / reference(source), y / reference(target)
        fit = regression(x[:training_end], y[:training_end], limit)
        if fit is None:
            continue
        selection = skill(x[training_end:selection_end], y[training_end:selection_end], fit, y[:training_end].mean())
        if selection is not None:
            results.append((selection, lag, x, y))
    if not results:
        return None, 'insufficient_variation' if admissible else 'insufficient_history'

    _, lag, x, y = max(results, key=lambda item: (item[0], -item[1]))
    fit = regression(x[:selection_end], y[:selection_end], limit)
    score = skill(x[selection_end:], y[selection_end:], fit, y[:selection_end].mean())
    if score is None or score < MIN_SCORE:
        return None, 'weak_holdout'
    return {
        'rule': default.model_copy(update={'multiplier': fit[0], 'lag_weeks': lag}),
        'holdout_skill': score, 'observations': len(dates), 'holdout_observations': len(dates) - selection_end,
    }, None


def aligned_by_date(source, target, lag):
    step = timedelta(days=7 if source.frequency == 'weekly' else 1)
    offset = timedelta(weeks=lag)
    values = {point.date: point.value for point in source.points}
    rows = {}
    for previous, point in zip(target.points, target.points[1:]):
        day = point.date - offset
        if point.date - previous.date == step and day in values and day - step in values:
            rows[point.date] = values[day] - values[day - step], point.value - previous.value
    return rows


def calibrate(model, defaults):
    incoming = {target: len(edges) for target, edges in model.causal.rules.items()}
    rows, edges = [], []
    for edge, method in model.causal.executable:
        default = defaults.get((edge.source, edge.target), edge.rule)
        effective = default or EdgeRule()
        row = {'source': edge.source, 'target': edge.target, 'name_ar': edge.name_ar,
               'default_rule': default, 'rule': effective if method == 'analytic' else None,
               'source_type': 'default', 'reason': None, 'holdout_skill': None}
        fitted = None
        if method != 'analytic':
            row.update(source_type='engine', reason='engine')
        elif incoming[edge.target] > 1:
            row['reason'] = 'multiple_inputs'
        else:
            # Arm optimizers consume scalar inputs at week zero.
            max_lag = 0 if edge.target in model.causal.upstream else MAX_LAG
            fitted, row['reason'] = fit_rule(model.causal.nodes[edge.source], model.causal.nodes[edge.target],
                                             effective, max_lag)
        if fitted is not None:
            row.update(fitted, source_type='calibrated')
        rule = row['rule'] if fitted else default
        edges.append(edge.model_copy(update={'rule': rule}))
        rows.append(row)
    return model.graph.model_copy(update={'edges': edges}), rows
