"""Independent checks on candidate schedules using the effective input snapshot."""
from app.optimization import contracting, production
from app.optimization.kpis import cash_curve, resource_usage
from app.templates_registry import load_templates

SOLVERS = {'contracting': contracting, 'production': production}
SOLUTION_STATUSES = {'FEASIBLE', 'OPTIMAL'}


def constraint_policy(template, model):
    configured = model.get('constraints', {})
    return {
        spec.id: {
            'label_ar': spec.name_ar,
            'kind': 'soft' if spec.kind == 'soft' or (
                spec.kind == 'hard_soft' and configured.get(spec.id) == 'soft'
            ) else 'hard',
        }
        for spec in load_templates()[template].constraints
    }


def check_constraint(rows, policy, key, slacks, unit):
    if slacks:
        minimum = min(slacks)
        rows[key] = policy[key] | {
            'unit': unit, 'minimum_slack': float(minimum), 'overrun': float(max(0, -minimum)),
            'binding_count': slacks.count(0),
        }


def contracting_checks(problem, plan, policy, rows):
    starts = {row['activity_id']: row['start_week'] for row in plan['activities']}
    if len(starts) != len(plan['activities']) or set(starts) != {a.id for a in problem.activities}:
        raise ValueError('Activity set does not match snapshot')
    precedence = []
    for project, chain in problem.chains.items():
        earliest = problem.projects[project]['nta_week']
        for activity in chain:
            start = starts[activity.id]
            finish = start + activity.duration
            if type(start) is not int or start < 0 or finish > problem.horizon:
                raise ValueError('Activity is outside the planning horizon')
            precedence.append(start - earliest)
            earliest = finish
    usage = resource_usage(problem.activities, starts, problem.capacity)
    resource_slack = [cap - load for key, capacity in problem.capacity.items()
                      for cap, load in zip(capacity, usage[key], strict=True)]
    cash = cash_curve(problem.activities, starts, problem.receipts, problem.lag, problem.cash_periods)
    check_constraint(rows, policy, 'precedence', precedence, 'أسبوع')
    check_constraint(rows, policy, 'resource_capacity', resource_slack, 'فريق')
    check_constraint(rows, policy, 'credit_limit', [balance + problem.credit for balance in cash], 'ألف ريال')


def production_checks(problem, plan, policy, rows):
    kpis = production.evaluate(problem, plan)['kpis']
    switches = {(row['line'], row['day']) for row in kpis['changeover_days']}
    slots = set()
    line_slack = []
    for row in kpis['schedule']:
        line, day, sku, quantity = (row[key] for key in ('line', 'day', 'sku', 'qty'))
        slot = (line, day)
        if type(day) is not int or type(quantity) is not int or quantity <= 0 or slot in slots:
            raise ValueError('Duplicate or invalid production slot')
        capacity = problem.rates.get((line, sku), 0)
        if day in problem.down[line]:
            capacity = 0
        elif slot in switches:
            capacity -= problem.lines[line]['changeover_loss_units']
        line_slack.append(capacity - quantity)
        slots.add(slot)
    shipped = {row['order'] for row in kpis['shipments']}
    if len(shipped) != len(kpis['shipments']) or any(type(row['day']) is not int for row in kpis['shipments']):
        raise ValueError('Duplicate or invalid shipment')
    check_constraint(rows, policy, 'line_capacity', line_slack, 'وحدة')
    check_constraint(rows, policy, 'inventory_balance',
                     [qty for stock in kpis['inventory'].values() for qty in stock], 'وحدة')
    check_constraint(rows, policy, 'cold_capacity', [problem.storage - qty for qty in kpis['storage_curve']], 'وحدة تخزين')
    check_constraint(rows, policy, 'credit_limit', [balance + problem.credit for balance in kpis['cash_curve']], 'ريال')


def assess(template, dataset, parameters, policy, plan, solver, *, inputs_approved, heuristic=False,
           approval_policy=None):
    rows = {}
    reasons = []
    has_candidate = bool(plan) and (heuristic or solver.get('status') in SOLUTION_STATUSES)
    if not inputs_approved:
        reasons.append('inputs_unapproved')
    if approval_policy == 'shadow_only':
        reasons.append('shadow_only')
    if not has_candidate:
        reasons.append('no_solution')
    elif not heuristic and (solver.get('search_status') or solver.get('status')) not in SOLUTION_STATUSES:
        reasons.append('solver_no_solution')
    if has_candidate:
        try:
            defaults = load_templates()[template].model_defaults()['parameters']
            problem = SOLVERS[template].prepare(dataset, defaults | parameters)
            check = contracting_checks if template == 'contracting' else production_checks
            check(problem, plan, policy, rows)
        except (KeyError, TypeError, ValueError, IndexError, ArithmeticError):
            reasons.append('invalid_schedule')
    hard_overruns = [key for key, row in rows.items() if row['kind'] == 'hard' and row['overrun'] > 0]
    if hard_overruns:
        reasons.append('mandatory_violation')
    return {
        'eligible': not reasons, 'has_candidate': has_candidate, 'reasons': reasons,
        'constraints': rows, 'hard_overruns': hard_overruns,
    }


def approved_inputs(validation):
    # Sector loaders persist only validated datasets. Explicit review flags, when
    # present, take precedence. An archived but validated version is replayable.
    return validation.get('ok') is True and validation.get('approved', True) is True


def ineligible_arms(impact):
    return [
        sector for sector, summary in (impact.get('solver_details') or {}).get('scenario', {}).items()
        if not (summary or {}).get('approval_eligibility', {}).get('eligible', False)
    ]
