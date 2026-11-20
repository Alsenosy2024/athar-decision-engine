"""Local PC over sparse, overlapping neighborhoods with pairwise date coverage."""
from collections import defaultdict
from itertools import combinations

import numpy as np

MIN_POINTS = 24
MAX_NEIGHBORS = 7
MIN_CORRELATION = 0.18
BLOCK_SIZE = 128


def neighborhoods(sources):
    keys = sorted(key for key, series in sources.items() if len(series) >= MIN_POINTS)
    if not keys:
        return {}
    dates = {day: index for index, day in enumerate(sorted({day for key in keys for day in sources[key]}))}
    centered = np.zeros((len(keys), len(dates)))
    present = np.zeros_like(centered)
    for index, key in enumerate(keys):
        series = sources[key]
        days = [dates[day] for day in series]
        values = np.asarray(list(series.values()))
        centered[index, days] = values - values.mean()
        present[index, days] = 1.0
    squares = centered ** 2
    result = {}
    for start in range(0, len(keys), BLOCK_SIZE):
        block = slice(start, start + BLOCK_SIZE)
        n = present[block] @ present.T
        safe_n = np.maximum(1, n)
        left = centered[block] @ present.T
        right = present[block] @ centered.T
        covariance = centered[block] @ centered.T - left * right / safe_n
        variances = (np.maximum(0, squares[block] @ present.T - left ** 2 / safe_n)
                     * np.maximum(0, present[block] @ squares.T - right ** 2 / safe_n))
        valid = (n >= MIN_POINTS) & (variances > 1e-16)
        scores = np.divide(np.abs(covariance), np.sqrt(variances), out=np.zeros_like(n), where=valid)
        for offset, row in enumerate(scores):
            index = start + offset
            row[index] = 0
            peers = np.flatnonzero(row >= MIN_CORRELATION)
            ranked = sorted(peers, key=lambda peer: (-row[peer], keys[peer]))[:MAX_NEIGHBORS]
            result[keys[index]] = [keys[peer] for peer in ranked]
    return result


def discover(sources, fit_pc):
    neighborhoods_by_center = neighborhoods(sources)
    votes = defaultdict(set)
    for center, peers in neighborhoods_by_center.items():
        # Skip a peer that destroys the overlap for the whole local fit.
        group = [center]
        common = set(sources[center])
        for peer in peers:
            overlap = common.intersection(sources[peer])
            if len(overlap) >= MIN_POINTS:
                group.append(peer)
                common = overlap
        if len(group) < 3:
            continue
        local = {key: {day: sources[key][day] for day in common} for key in group}
        directed = fit_pc(local)
        for source, target in combinations(sorted(group), 2):
            if (source, target) in directed:
                votes[source, target].add((source, target))
            if (target, source) in directed:
                votes[source, target].add((target, source))
    return {next(iter(directions)) for directions in votes.values() if len(directions) == 1}
