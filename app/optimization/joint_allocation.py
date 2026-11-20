"""Multiple-choice allocation across business units with shared resource limits."""
from collections import Counter

import numpy as np
from ortools.linear_solver import pywraplp
from pydantic import Field, model_validator

from app.enterprise.schema import NodeId, Number, StrictFields
from app.simulation.dependence import Correlation, correlated_triangular, correlation_factor

TRAIN_SAMPLES = 96
VALIDATION_SAMPLES = 512
TAIL_FRACTION = 0.2
SEEDS = (42, 137, 733, 971)
MAX_VALUE = 1e9


class Option(StrictFields):
    id: NodeId
    resources: dict[NodeId, Number] = Field(max_length=12)
    value: Number = Field(ge=-MAX_VALUE, le=MAX_VALUE)
    exposures: dict[NodeId, Number] = Field(default_factory=dict, max_length=12)


class BusinessUnit(StrictFields):
    id: NodeId
    current: NodeId
    options: list[Option] = Field(min_length=1, max_length=20)


class Allocation(StrictFields):
    units: list[BusinessUnit] = Field(min_length=2, max_length=12)
    limits: dict[NodeId, Number] = Field(min_length=1, max_length=12)
    correlations: list[Correlation] = Field(default_factory=list, max_length=66)
    tail_weight: Number = Field(default=0.25, ge=0, le=1)

    @model_validator(mode='after')
    def validate_portfolio(self):
        if len({unit.id for unit in self.units}) != len(self.units):
            raise ValueError('Business unit IDs must be unique')
        if any(not 0 <= value <= MAX_VALUE for value in self.limits.values()):
            raise ValueError('Resource limits must be nonnegative and in range')
        for unit in self.units:
            options = {option.id for option in unit.options}
            if len(options) != len(unit.options) or unit.current not in options:
                raise ValueError('Each unit needs distinct options and a current option')
            for option in unit.options:
                if option.resources.keys() - self.limits.keys():
                    raise ValueError('Each resource needs a shared limit')
                if any(not 0 <= amount <= MAX_VALUE for amount in option.resources.values()):
                    raise ValueError('Resource usage must be nonnegative and in range')
                if any(abs(amount) > MAX_VALUE for amount in option.exposures.values()):
                    raise ValueError('Driver exposures exceed the supported range')
        if len(self.drivers()) > 12:
            raise ValueError('The allocation supports up to 12 uncertain drivers')
        if self.correlations:
            correlation_factor(self.drivers(), self.correlations)
        return self

    def drivers(self):
        return sorted({key for unit in self.units for option in unit.options for key in option.exposures})


def payoff(options, shocks, drivers):
    exposures = np.asarray([[option.exposures.get(key, 0) for key in drivers] for option in options])
    return np.asarray([option.value for option in options]) + shocks @ exposures.T


def solve(problem, options, groups, outcomes):
    solver = pywraplp.Solver.CreateSolver('SCIP')
    if solver is None:
        raise RuntimeError('Joint allocation solver is unavailable')
    solver.SetTimeLimit(2500)
    variables = [solver.BoolVar(str(index)) for index in range(len(options))]
    for group in groups:
        solver.Add(solver.Sum(variables[index] for index in group) == 1)
    for key, limit in problem.limits.items():
        solver.Add(solver.Sum(var * option.resources.get(key, 0)
                              for var, option in zip(variables, options)) <= limit)
    objective = (1 - problem.tail_weight) * solver.Sum(var * float(value)
                                                       for var, value in zip(variables, outcomes.mean(axis=0)))
    if problem.tail_weight:
        cutoff = solver.NumVar(-solver.infinity(), solver.infinity(), 'cutoff')
        shortfalls = []
        for index, outcome in enumerate(outcomes):
            shortfall = solver.NumVar(0, solver.infinity(), f'shortfall_{index}')
            shortfalls.append(shortfall)
            solver.Add(shortfall >= cutoff - solver.Sum(var * float(value)
                                                        for var, value in zip(variables, outcome)))
        objective += problem.tail_weight * (cutoff - solver.Sum(shortfalls) / (len(outcomes) * TAIL_FRACTION))
    solver.Maximize(objective)
    status = solver.Solve()
    if status in (solver.OPTIMAL, solver.FEASIBLE):
        return status == solver.OPTIMAL, [index for index, var in enumerate(variables) if var.solution_value() > 0.5]
    return None, []


def assess(problem, options, indices, outcomes):
    chosen = [options[index] for index in indices]
    usage = Counter()
    for option in chosen:
        usage.update(option.resources)
    for key in problem.limits:
        usage.setdefault(key, 0)
    gains = outcomes[:, indices].sum(axis=1)
    # Fractional last observation gives the same lower-tail CVaR objective as the solver.
    ordered = np.sort(gains)
    count = len(gains) * TAIL_FRACTION
    whole = int(count)
    tail = (ordered[:whole].sum() + (count - whole) * ordered[whole]) / count
    mean = float(np.mean(gains))
    return {'selection': {unit.id: option.id for unit, option in zip(problem.units, chosen)},
            'resource_usage': dict(usage), 'feasible': all(amount <= problem.limits[key] + 1e-8
                                                           for key, amount in usage.items()),
            'mean': mean, 'lower_tail_mean': float(tail),
            'score': float((1 - problem.tail_weight) * mean + problem.tail_weight * tail)}


def allocate(problem):
    options, groups, current = [], [], []
    for unit in problem.units:
        group = list(range(len(options), len(options) + len(unit.options)))
        current.append(next(index for index, option in zip(group, unit.options) if option.id == unit.current))
        groups.append(group)
        options.extend(unit.options)
    drivers = problem.drivers()
    draws = [payoff(options, correlated_triangular(drivers, problem.correlations, count, seed), drivers)
             for count, seed in zip((TRAIN_SAMPLES,) + (VALIDATION_SAMPLES,) * 3, SEEDS)]
    optimal, selected = solve(problem, options, groups, draws[0])
    baseline = assess(problem, options, current, np.vstack(draws[1:]))
    candidate = assess(problem, options, selected, np.vstack(draws[1:])) if selected else None
    comparisons = [{'seed': seed, 'current_score': assess(problem, options, current, data)['score'],
                    'joint_score': assess(problem, options, selected, data)['score'] if selected else None}
                   for seed, data in zip(SEEDS[1:], draws[1:])]
    improved = candidate and candidate['feasible'] and (
        not baseline['feasible'] or all(row['joint_score'] > row['current_score'] + 1e-8 for row in comparisons))
    return {'method': 'joint_credit_and_resource_allocation', 'training_seed': SEEDS[0],
            'training_samples': TRAIN_SAMPLES, 'samples_per_validation_seed': VALIDATION_SAMPLES,
            'optimal': optimal, 'current': baseline, 'joint': candidate, 'validation': comparisons,
            'preferred': 'joint' if improved else 'current' if baseline['feasible'] else None}
