"""Candidate dependencies from PC and held-out, bounded lagged effect estimates."""
from collections import defaultdict
from datetime import timedelta
from graphlib import CycleError, TopologicalSorter

import numpy as np

from app.enterprise.edge_contracts import TIME_IN_DAYS, contract_unit
from app.enterprise.schema import Edge, EdgeRule
from app.intelligence.analysis import history

MIN_POINTS = 24
MAX_NODES = 12
MAX_LAG = 4
MAX_EFFECT = 100


def differences(node):
    points = node.baseline.points
    days = 7 if node.baseline.frequency == 'weekly' else 1
    return {right.date: right.value - left.value for left, right in zip(points, points[1:])
            if (right.date - left.date).days == days}


def pc_links(sources):
    from causallearn.search.ConstraintBased.PC import pc

    keys = sorted(sources)[:MAX_NODES]
    if len(keys) < 2:
        return set()
    common = sorted(set.intersection(*(set(sources[key]) for key in keys)))
    if len(common) < MIN_POINTS:
        return set()
    matrix = np.column_stack([[sources[key][day] for day in common] for key in keys])
    keep = np.std(matrix, axis=0) > 1e-10
    usable = [key for key, include in zip(keys, keep) if include]
    matrix = matrix[:, keep]
    if len(usable) < 2:
        return set()
    # Exact collinearity makes the Fisher-Z covariance singular. Preserve
    # reviewed edges for bounded fitting even when discovery can't run.
    try:
        adjacency = pc(matrix, alpha=0.05, stable=True, show_progress=False).G.graph
    except (ValueError, np.linalg.LinAlgError):
        return set()
    return {(usable[i], usable[j]) for i in range(len(usable)) for j in range(len(usable))
            if adjacency[i, j] == -1 and adjacency[j, i] == 1}


def estimate(source, target, periodicity, lags=range(MAX_LAG + 1)):
    fits = []
    for lag in lags:
        offset = timedelta(days=lag * periodicity)
        days = sorted(day for day in target if day - offset in source)
        if len(days) < MIN_POINTS:
            continue
        x = np.asarray([source[day - offset] for day in days])
        y = np.asarray([target[day] for day in days])
        # Choose lag on a validation window, then report skill on fresh points.
        train_end, validation_end = len(days) * 60 // 100, len(days) * 80 // 100
        xc, yc = x[:train_end] - x[:train_end].mean(), y[:train_end] - y[:train_end].mean()
        variance = xc @ xc
        if variance < 1e-10:
            continue
        slope = float(np.clip(xc @ yc / variance, -MAX_EFFECT, MAX_EFFECT))
        intercept = y[:train_end].mean() - slope * x[:train_end].mean()

        def skill(start, end):
            actual, guess = y[start:end], x[start:end]
            naive = np.mean(np.abs(actual - y[:train_end].mean()))
            return float(1 - np.mean(np.abs(actual - intercept - slope * guess)) / naive) if naive else 0

        residuals = yc - slope * xc
        stderr = float(np.sqrt((residuals @ residuals) / (train_end - 2) / variance))
        fits.append({'multiplier': slope, 'lag_weeks': lag, 'n': len(days),
                     'validation_skill': skill(train_end, validation_end), 'holdout_skill': skill(validation_end, len(days)),
                     'train_interval': [max(-MAX_EFFECT, slope - 1.96 * stderr),
                                        min(MAX_EFFECT, slope + 1.96 * stderr)]})
    if not fits:
        return None
    chosen = max(fits, key=lambda item: (item['validation_skill'], -item['lag_weeks']))
    if min(chosen['validation_skill'], chosen['holdout_skill']) <= 0.05:
        return None
    return chosen


def cyclic(edges, pair):
    dependencies = defaultdict(set)
    for source, target in edges | {pair}:
        dependencies[target].add(source)
    try:
        list(TopologicalSorter(dependencies).static_order())
    except CycleError:
        return True
    return False


def proposals(model):
    nodes = history(model.graph)
    # The existing propagation clock is weekly. Daily history is kept for
    # analyses/forecasting but needs weekly aggregation before fitting edges.
    changes = {key: differences(node) for key, node in nodes.items() if node.baseline.frequency == 'weekly'}
    discovered = pc_links(changes)
    existing = {(edge.source, edge.target): (edge, method) for edge, method in model.causal.executable}
    known = set(existing)
    output = []
    for source, target in sorted(known | discovered):
        pair = source, target
        if source not in changes or target not in changes:
            continue
        old, method = existing.get(pair, (None, 'analytic'))
        if method != 'analytic' or target in model.causal.bound:
            continue
        if old and old.rule and old.rule.mode != 'linear':
            continue
        if not old and (target in model.causal.upstream or (target, source) in known or cyclic(known, pair)):
            continue
        fit = estimate(changes[source], changes[target], 7,
                       lags=(0,) if target in model.causal.upstream else range(MAX_LAG + 1))
        if not fit:
            continue
        left, right = nodes[source].unit, nodes[target].unit
        unit_factor = TIME_IN_DAYS[left] / TIME_IN_DAYS[right] if left in TIME_IN_DAYS and right in TIME_IN_DAYS else 1
        fit['multiplier'] /= unit_factor
        rule = EdgeRule(multiplier=fit['multiplier'], lag_weeks=fit['lag_weeks'],
                        minimum=old.rule.minimum if old and old.rule else None,
                        maximum=old.rule.maximum if old and old.rule else None)
        method = 'pc_fisher_z_bounded_regression' if pair in discovered else 'bounded_lagged_regression'
        link = Edge(source=source, target=target, rule=rule, status='draft',
                    unit=contract_unit(left, right, rule), evidence=method,
                    name_ar=old.name_ar if old else f'{nodes[source].name_ar} → {nodes[target].name_ar}'[:160])
        confidence = min(0.9, fit['holdout_skill'], fit['n'] / 80)
        output.append({'edge': link.model_dump(mode='json'), 'before': old.model_dump(mode='json') if old else None,
                       'method': method,
                       'confidence': round(confidence, 3), **fit})
        known.add(pair)
    return output
