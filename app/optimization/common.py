from dataclasses import dataclass

from ortools.sat.python import cp_model


class OptimizationInputError(ValueError):
    """A dataset or model configuration that cannot be passed to CP-SAT."""


@dataclass(frozen=True)
class Weights:
    ld: float
    financing: float
    makespan: float

    def scaled(self):
        return tuple(round(weight * 10) for weight in (self.ld, self.financing, self.makespan))


def configured_solver(time_limit: float, *, deterministic: bool = False) -> cp_model.CpSolver:
    solver = cp_model.CpSolver()
    solver.parameters.random_seed = 7
    if deterministic:
        # Repeated enterprise calculations must finish at the same search step,
        # even when the worker is delayed by other requests.
        solver.parameters.num_workers = 1
        solver.parameters.max_deterministic_time = 0.8 * time_limit
    else:
        solver.parameters.max_time_in_seconds = time_limit
        solver.parameters.num_workers = 8
    return solver
