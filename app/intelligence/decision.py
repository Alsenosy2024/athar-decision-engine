"""Bayesian search ranks decisions using the enterprise propagation engine."""
from math import atan
from typing import Literal

from pydantic import Field, model_validator

from app.decision_loop.eligibility import ineligible_arms
from app.enterprise.impact_graph import Assumption, ImpactError, WhatIfRequest
from app.enterprise.schema import NodeId, Number, StrictFields
from app.intelligence.analysis import display
from app.optimization.budget import SearchExpired, SolveBudget
from app.optimization.common import OptimizationInputError, OptimizationTimeout


class Control(StrictFields):
    node_id: NodeId
    low: Number
    high: Number
    uncertainty: Number = Field(default=0, ge=0, le=1e10)

    @model_validator(mode='after')
    def ordered(self):
        if not -1e10 <= self.low < self.high <= 1e10:
            raise ValueError('Control bounds must be finite and ordered')
        return self


class Goal(StrictFields):
    objective: NodeId = 'pnl.result'
    direction: Literal['maximize', 'minimize'] = 'maximize'
    target: Number | None = None
    target_pct: Number | None = Field(default=None, gt=0, le=1000)
    cash_floor: Number | None = None
    risk_ceiling: Number | None = Field(default=None, ge=0, le=100)
    controls: list[Control] = Field(min_length=1, max_length=5)
    trials: int = Field(default=12, ge=4, le=32)
    samples: int = Field(default=64, ge=16, le=200)
    weeks: int = Field(default=52, ge=1, le=104)

    @model_validator(mode='after')
    def valid_goal(self):
        if len({item.node_id for item in self.controls}) != len(self.controls):
            raise ValueError('Each control must appear once')
        if self.target is not None and self.target_pct is not None:
            raise ValueError('Choose an absolute or relative target')
        return self


def whatif(goal, controls, values, *, stress=False):
    return WhatIfRequest(n=1 if stress else goal.samples, seed=42, weeks=goal.weeks, assumptions=[
        Assumption(node_id=control.node_id, value=value, uncertainty=0 if stress else control.uncertainty)
        for control, value in zip(controls, values)
    ])


def outcome(result, objective):
    rows = {row['id']: row for row in result['nodes']}
    return {name: rows.get(key, {}).get('after') for name, key in (
        ('objective', objective), ('cash', 'cash'), ('risk', 'risk.credit'),
    )} | {'ineligible_arms': ineligible_arms(result)}


def violations(values, goal):
    return [item for active, item in (
        (bool(values.get('ineligible_arms')), 'sector_plan_ineligible'),
        (goal.cash_floor is not None and (values['cash'] is None or values['cash'] < goal.cash_floor), 'cash_floor'),
        (goal.risk_ceiling is not None and (values['risk'] is None or values['risk'] > goal.risk_ceiling), 'risk_ceiling'),
    ) if active]


def meets_target(value, target, direction):
    if target is None:
        return None
    return value >= target if direction == 'maximize' else value <= target


def controllable(model):
    derived = {edge.target for edge, _ in model.causal.executable}
    return [item for item in model.drivers()
            if (item['id'] not in derived or item['id'] in model.causal.drivers)
            and model.causal.nodes[item['id']].type != 'risk_kpi']


def critique(model, goal, controls, values, result, base, budget):
    primary = outcome(result, goal.objective)
    direction = 1 if goal.direction == 'maximize' else -1
    stresses = []
    # One-at-a-time endpoint stresses show which assumption can damage the
    # result most. These are adversarial checks, separate from sampled risk.
    for index, control in enumerate(controls):
        for bound in (control.low, control.high):
            if not budget.remaining():
                break
            stressed = list(values)
            stressed[index] = bound
            if stressed == values:
                continue
            try:
                check = model.scenario(whatif(goal, controls, stressed, stress=True), base=base)
                tested = outcome(check, goal.objective)
            except (SearchExpired, OptimizationTimeout):
                break
            except (ImpactError, OptimizationInputError):
                stresses.append({'node_id': control.node_id, 'tested_value': bound, 'invalid': True})
                continue
            if tested['objective'] is None:
                continue
            stresses.append({'node_id': control.node_id, 'tested_value': bound, 'invalid': False,
                             **tested, 'objective_loss': direction * (primary['objective'] - tested['objective']),
                             'violations': violations(tested, goal)})
    weakest = max(stresses, key=lambda item: (
        item['invalid'], len(item.get('violations', [])), item.get('objective_loss', 0),
    ), default=None)
    if weakest is None and not budget.remaining():
        text = 'مهلة البحث خلصت قبل اختبار حدود إضافية.'
    elif weakest is None:
        text = 'الحدود المتاحة مفيهاش بديل إضافي لاختبار الفرضية.'
    else:
        nodes = model.causal.nodes
        name = nodes[weakest['node_id']].name_ar
        unit = nodes[weakest['node_id']].unit
        text = f"أضعف فرضية في اختبار الحدود هي {name} عند {display(weakest['tested_value'], unit)}."
        if weakest['invalid']:
            text += ' المحرك مقدرش يحل الحالة عند الحد ده.'
        else:
            text += f" قيمة الهدف في الحالة دي {display(weakest['objective'], nodes[goal.objective].unit)}."
            if 'cash_floor' in weakest['violations']:
                text += ' السيولة نزلت تحت الحد المطلوب.'
            if 'risk_ceiling' in weakest['violations']:
                text += ' تجاوز الائتمان عدى السقف المطلوب.'
    passed = sum(not item['invalid'] and not item['violations'] and item['objective_loss'] <= 0
                 for item in stresses)
    return {'weakest': weakest, 'stress_tests': stresses, 'reply_ar': text,
            'stress_pass_rate': passed / len(stresses) if stresses else None}


def recommend(model, goal):
    budget = SolveBudget(model.arms.time_limit)
    with budget.share():
        return search(model, goal, budget)


def search(model, goal, budget):
    import optuna

    drivers = {item['id']: item for item in controllable(model)}
    controls = sorted(goal.controls, key=lambda item: item.node_id)
    if goal.objective not in model.causal.nodes or any(
        item.node_id not in drivers or item.node_id == goal.objective for item in controls
    ):
        raise ImpactError('راجع المؤشر والمدخلات القابلة للتحكم.')
    if model.graph.arms and (len(controls) > 2 or goal.trials > 12):
        raise ImpactError('النماذج اللي فيها حلّال تشغيلي بتدعم متغيرين و12 تجربة في المرة.')
    base = model.baseline()
    metrics = base[0].metrics()
    reference = metrics.get(goal.objective)
    if reference is None:
        raise ImpactError('الهدف محتاج قيمة مرجعية محسوبة.')
    if goal.cash_floor is not None and metrics.get('cash') is None:
        raise ImpactError('قيد السيولة محتاج عنصر سيولة بقيمة مرجعية.')
    if goal.risk_ceiling is not None and metrics.get('risk.credit') is None:
        raise ImpactError('قيد تجاوز الائتمان محتاج مؤشر مخاطرة محسوب.')
    direction = 1 if goal.direction == 'maximize' else -1
    target = goal.target
    if goal.target_pct is not None:
        if reference == 0:
            raise ImpactError('الهدف النسبي محتاج قيمة مرجعية غير صفرية.')
        target = reference + direction * abs(reference) * goal.target_pct / 100
    sampler = optuna.samplers.TPESampler(seed=42, n_startup_trials=4)
    study = optuna.create_study(direction='maximize', sampler=sampler)
    low = {item.node_id: item.low for item in controls}
    high = {item.node_id: item.high for item in controls}
    midpoint = {item.node_id: (item.low + item.high) / 2 for item in controls}
    for point in (low, high, midpoint):
        study.enqueue_trial(point)
    evaluated = []
    attempted = 0
    for _ in range(goal.trials):
        if not budget.remaining():
            break
        attempted += 1
        trial = study.ask()
        values = [trial.suggest_float(item.node_id, item.low, item.high) for item in controls]
        body = whatif(goal, controls, values)
        try:
            result = model.scenario(body, base=base)
            score = outcome(result, goal.objective)
        except (SearchExpired, OptimizationTimeout):
            study.tell(trial, state=optuna.trial.TrialState.FAIL)
            break
        except (ImpactError, OptimizationInputError):
            study.tell(trial, state=optuna.trial.TrialState.FAIL)
            continue
        if score['objective'] is None:
            study.tell(trial, state=optuna.trial.TrialState.FAIL)
            continue
        bad = violations(score, goal)
        # arctan keeps the objective on a finite scale so any feasible trial
        # ranks ahead of infeasible trials without domain-specific penalties.
        study.tell(trial, float(direction * atan(
            (score['objective'] - reference) / max(abs(reference), 1))) - 4 * len(bad))
        evaluated.append((bad, score, values, body, result))

    ranked = sorted(evaluated, key=lambda row: (
        len(row[0]), -direction * row[1]['objective'],
        row[1]['risk'] if row[1]['risk'] is not None else float('inf'),
        sum(abs(value - drivers[item.node_id]['value']) for value, item in zip(row[2], controls)),
    ))
    recommendations = []
    seen = set()
    for bad, score, values, body, result in ranked:
        signature = tuple(round(value, 9) for value in values)
        if signature in seen:
            continue
        seen.add(signature)
        target_met = meets_target(score['objective'], target, goal.direction)
        recommendations.append({
            **score, 'target_met': target_met, 'violations': bad,
            'disposition': 'infeasible' if bad else 'scenario_only' if target_met is False else 'eligible_decision',
            'constraints': {'cash_floor': goal.cash_floor, 'risk_ceiling': goal.risk_ceiling,
                            'control_bounds': [item.model_dump(mode='json') for item in controls]},
            'outcome_context': result['outcome_context'] | {'assumptions': [
                item | {'source': 'goal_search_controls'} for item in result['outcome_context']['assumptions']
            ]},
            'model_basis': 'cp_sat_and_propagation' if model.graph.arms else 'propagation',
            'assumptions': [item.model_dump(mode='json') for item in body.assumptions],
            'critique': critique(model, goal, controls, values, result, base, budget),
            'impact': result,
        })
        if len(recommendations) == 3:
            break
    timed_out = not budget.remaining()
    state = 'partial' if timed_out and recommendations else 'no_result' if timed_out else 'complete'
    return {'search_status': {'state': state, 'stop_reason': 'time_limit' if timed_out else 'trial_limit',
                              'limit_s': budget.limit, 'attempted_trials': attempted},
            'method': 'optuna_tpe', 'objective': goal.objective, 'reference': reference, 'target': target,
            'direction': goal.direction, 'target_pct': goal.target_pct, 'cash_floor': goal.cash_floor,
            'risk_ceiling': goal.risk_ceiling,
            'trials': goal.trials, 'evaluated_trials': len(evaluated), 'recommendations': recommendations}


def interpret_goal(text, graph):
    """Prefill only explicit targets. Editable controls stay with the planner."""
    import re

    from app.intelligence.analysis import target_ids
    from app.intelligence.extraction import NUMBER, decimal_value, norm, normalized_value
    from app.enterprise.schema import Unit

    text = norm(text)
    cash_floor = None
    cash = re.search(
        r'(?:السيوله|الكاش|النقديه).{0,36}?(?:ما تنزلش عن|مينزلش عن|اقل من|حد ادني|علي الاقل|ما يقلش عن)\s*'
        + f'({NUMBER.pattern})' + r'\s*([^،؛,]*)', text,
    )
    if cash:
        try:
            cash_floor = normalized_value(cash[1], cash[2], Unit.SAR, {})
        except ValueError:
            pass
        text = text[:cash.start()]
    target = re.search(f'({NUMBER.pattern})' + r'\s*%|(?:بنسبه\s*)' + f'({NUMBER.pattern})'
                       + r'\s*(?:بالم(?:يه|ئه)|في الم(?:يه|ئه))', text)
    target_pct = float(decimal_value(target[1] or target[2])) if target else None
    if target_pct is not None and not 0 < target_pct <= 1000:
        target_pct = None
    direction = None
    if re.search(r'(?<!\w)(?:اخفض|اقل[لل]|خفض|تقليل|نقلل|انزل|خفضه)(?!\w)', text):
        direction = 'minimize'
    elif re.search(r'(?<!\w)(?:ارفع|رفع|ازود|نزود|زياده|نرفع|احسن)(?!\w)', text):
        direction = 'maximize'
    ids = target_ids(graph, text)
    return {'objective': ids[0] if ids else None, 'direction': direction,
            'target_pct': target_pct, 'cash_floor': cash_floor}
