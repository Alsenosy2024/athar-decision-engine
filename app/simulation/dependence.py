"""Seeded Gaussian copula for bounded triangular driver shocks."""
import numpy as np
from pydantic import Field, model_validator
from scipy.special import ndtr

from app.enterprise.schema import NodeId, Number, StrictFields


class Correlation(StrictFields):
    left: NodeId
    right: NodeId
    rho: Number = Field(ge=-1, le=1)

    @model_validator(mode='after')
    def distinct(self):
        if self.left == self.right:
            raise ValueError('Correlation requires two different drivers')
        return self


def correlation_factor(keys, correlations):
    order = {key: index for index, key in enumerate(keys)}
    matrix = np.eye(len(keys))
    seen = set()
    for item in correlations:
        if item.left not in order or item.right not in order:
            raise ValueError('Correlated drivers must both have positive uncertainty')
        i, j = sorted((order[item.left], order[item.right]))
        if (i, j) in seen:
            raise ValueError('Each correlation pair may appear once')
        seen.add((i, j))
        matrix[i, j] = matrix[j, i] = item.rho
    eigenvalues, vectors = np.linalg.eigh(matrix)
    if eigenvalues[0] < -1e-8:
        raise ValueError('The correlation matrix must be positive semidefinite')
    return vectors * np.sqrt(np.maximum(eigenvalues, 0))


def correlated_triangular(keys, correlations, n, seed):
    factor = correlation_factor(keys, correlations) if correlations else np.eye(len(keys))
    normal = np.random.default_rng(seed).standard_normal((n, len(keys)))
    uniform = ndtr(normal @ factor.T)
    # Inverse CDF of the symmetric triangular distribution on [-1, 1].
    return np.where(uniform < 0.5, np.sqrt(2 * uniform) - 1, 1 - np.sqrt(2 - 2 * uniform))
