from dataclasses import dataclass

from ortools.sat.python import cp_model


class OptimizationTimeout(RuntimeError):
    """Search timed out without a usable operating plan."""


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
    solver.parameters.max_time_in_seconds = time_limit
    solver.parameters.num_workers = 1 if deterministic else 8
    if deterministic:
        # Prefer a reproducible search quota. The wall cap also applies under load.
        solver.parameters.max_deterministic_time = 0.05 * time_limit
    return solver
