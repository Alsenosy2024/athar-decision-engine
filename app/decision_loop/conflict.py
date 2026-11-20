"""CP-SAT feasibility diagnosis with named hard-policy assumptions."""
from ortools.sat.python import cp_model

from app.decision_loop.eligibility import SOLVERS
from app.optimization.budget import SolveBudget
from app.optimization.common import SOLVER_SEED


class Assumptions:
    def __init__(self, policy):
        self.keys = [key for key, spec in policy.items() if spec['kind'] == 'hard']

    def attach(self, model):
        self.literals = {key: model.new_bool_var(f'assume:{key}') for key in self.keys}
        model.add_assumptions(self.literals.values())

    def guard(self, key, constraint):
        constraint.only_enforce_if(self.literals[key])


def diagnose(template, dataset, model, policy, time_limit):
    config = {'seed': SOLVER_SEED, 'workers': 1, 'time_limit_s': time_limit}
    budget = SolveBudget(time_limit)
    assumptions = Assumptions(policy)
    solver_module = SOLVERS[template]
    try:
        problem = solver_module.prepare(dataset, model['parameters'])
        cp, *_ = solver_module.build_model(problem, None, None, diagnosis=assumptions)
    except (TypeError, ValueError, ArithmeticError):
        return {'status': 'MODEL_INVALID', 'constraint_ids': [], 'config': config}

    solver = cp_model.CpSolver()
    solver.parameters.random_seed = SOLVER_SEED
    solver.parameters.num_workers = 1
    solver.parameters.max_time_in_seconds = budget.remaining()
    solver.parameters.max_deterministic_time = time_limit * 0.05
    status = solver.solve(cp) if solver.parameters.max_time_in_seconds > 0 else cp_model.UNKNOWN
    core = set(solver.sufficient_assumptions_for_infeasibility()) if status == cp_model.INFEASIBLE else set()
    return {
        'status': solver.status_name(status), 'config': config,
        'constraint_ids': [key for key, literal in assumptions.literals.items() if literal.index in core],
    }
