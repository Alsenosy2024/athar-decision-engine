"""Enumerate a small bounded grid using the enterprise impact engine."""
import re
from itertools import product
from math import isfinite
from typing import Annotated

from pydantic import Field, model_validator

from app.enterprise.impact_graph import Assumption, ImpactError, WhatIfRequest
from app.enterprise.scenarios import KPI_NODES, kpis
from app.enterprise.schema import Label, NodeId, Number, StrictFields
from app.llm.rules_ar import NUMBER, normalize, number_words
from app.optimization.common import OptimizationInputError

Value = Annotated[float, Field(strict=True, ge=-1e12, le=1e12, allow_inf_nan=False)]
WEEKS = 52


class Control(StrictFields):
    node_id: NodeId
    minimum: Value
    maximum: Value

    @model_validator(mode='after')
    def valid_bounds(self):
        if self.minimum >= self.maximum:
            raise ValueError('Maximum must exceed minimum')
        return self


class GoalTarget(StrictFields):
    profit_pct: Number = Field(ge=0, le=1000)
    cash_minimum: Value


GOAL_TEXT = re.compile(
    rf'(?:(?:انا )?(?:عايز|عاوز|اريد) )?(?:ارفع|ازود|رفع|زياده) (?:صافي )?الربح '
    rf'(?:بنسبه )?({NUMBER})\s*%\s+(?:بدون|من غير) (?:ما )?(?:الكاش|السيوله) '
    rf'(?:ينزل|تنزل|يقل|تقل|ينخفض|تنخفض) (?:تحت|عن) ({NUMBER})(?: (الف|مليون))?(?: ريال(?: سعودي)?)?[؟?]?',
)


def parse_target(message):
    match = GOAL_TEXT.fullmatch(number_words(normalize(message)))
    if match is None:
        raise ImpactError('اكتب الهدف مثل «عايز أرفع الربح 10% من غير ما السيولة تنزل تحت 50 ألف ريال».')
    pct, cash, magnitude = match.groups()
    multiplier = {None: 1, 'الف': 1_000, 'مليون': 1_000_000}[magnitude]
    return GoalTarget(profit_pct=float(pct), cash_minimum=float(cash) * multiplier)


class Goal(GoalTarget):
    version: int = Field(ge=1, strict=True)
    controls: list[Control] = Field(min_length=1, max_length=3)

    @model_validator(mode='after')
    def unique_controls(self):
        if len({item.node_id for item in self.controls}) != len(self.controls):
            raise ValueError('Choose distinct controls')
        return self


class Draft(StrictFields):
    name: Label
    goal: Goal
    actions: dict[NodeId, Value] = Field(min_length=1, max_length=3)


def controllable(model):
    # A known arm control or an exogenous analytical root may be changed.
    # Demand and supplier delays are observed shocks in the built-in template.
    derived = {edge.target for edge, method in model.causal.executable if method == 'analytic'}
    sources = {edge.source for edge, _ in model.causal.executable}
    blocked = set(KPI_NODES.values()) - {'cash'} if model.template else set(KPI_NODES.values())
    return [driver for driver in model.drivers()
            if driver['id'] not in derived | blocked
            and (driver['id'] in sources or driver['id'] in model.causal.drivers)
            and (not model.template or not driver['id'].startswith(('demand.', 'downtime.'))
                 and driver['id'] != 'supplier')]


def prepare(model, body):
    drivers = {row['id']: row for row in controllable(model)}
    references = {}
    for control in body.controls:
        if control.node_id not in drivers:
            raise ImpactError(f'المدخل {control.node_id} مش قابل للتحكم في البحث.')
        value = drivers[control.node_id]['value']
        if not control.minimum <= value <= control.maximum:
            raise ImpactError(f'نطاق {control.node_id} لازم يشمل قيمة خط الأساس.')
        references[control.node_id] = value
    base = model.baseline()
    metrics = kpis(base[0].metrics(), base[0].fill_rate)
    if metrics['profit'] is None or metrics['cash_low'] is None:
        raise ImpactError('البحث محتاج مؤشري الربح وأقل رصيد سيولة في النموذج.')
    # A loss improves when it moves toward zero. A percentage from zero has no scale.
    if metrics['profit'] == 0 and body.profit_pct:
        raise ImpactError('خط أساس الربح صفر. نسبة التحسن محتاجة قيمة مرجعية غير صفرية.')
    profit_minimum = metrics['profit'] + abs(metrics['profit']) * body.profit_pct / 100
    if not isfinite(profit_minimum):
        raise ImpactError('هدف الربح أكبر من النطاق المدعوم.')
    return base, metrics, references, profit_minimum


def evaluate(model, base, actions):
    assumptions = [Assumption(node_id=key, value=value) for key, value in actions.items()]
    result = model.scenario(WhatIfRequest(assumptions=assumptions, weeks=WEEKS, n=1), base=base)
    metrics = kpis({node['id']: node['after'] for node in result['nodes']}, result['fill_rate'])
    return {
        'actions': actions, 'kpis': metrics, 'assumptions': result['assumptions'],
        'impact': [node for node in result['nodes'] if node['affected']],
        'solver_status': result['solver_status'],
    }


def assess(candidate, target, cash_minimum):
    return candidate['kpis']['profit'] >= target and candidate['kpis']['cash_low'] >= cash_minimum


def search(model, body):
    base, metrics, references, target = prepare(model, body)
    controls = body.controls
    points = []
    for control in controls:
        reference = references[control.node_id]
        # Sample the range on each side of the starting point and include the status quo.
        levels = [control.minimum, reference, control.maximum]
        if len(controls) == 1:
            levels += [(control.minimum + reference) / 2, (control.maximum + reference) / 2]
        points.append(sorted(set(levels)))

    candidates, evaluated, invalid = [], 0, 0
    for values in product(*points):
        actions = {control.node_id: value for control, value in zip(controls, values)
                   if value != references[control.node_id]}
        if not actions:
            continue
        try:
            row = evaluate(model, base, actions)
        except (ImpactError, OptimizationInputError, FloatingPointError, OverflowError):
            invalid += 1
            continue
        evaluated += 1
        if assess(row, target, body.cash_minimum):
            row['effort'] = sum(abs(actions.get(item.node_id, references[item.node_id]) - references[item.node_id])
                                / (item.maximum - item.minimum) for item in controls)
            candidates.append(row)

    candidates.sort(key=lambda row: (row['effort'], -row['kpis']['profit'], -row['kpis']['cash_low']))
    return {'baseline': metrics, 'profit_minimum': target, 'cash_minimum': body.cash_minimum,
            'baseline_meets_goal': metrics['profit'] >= target and metrics['cash_low'] >= body.cash_minimum,
            'evaluated': evaluated, 'invalid': invalid, 'recommendations': candidates[:5]}


def validate_draft(model, draft):
    base, metrics, references, target = prepare(model, draft.goal)
    controls = {control.node_id: control for control in draft.goal.controls}
    if any(key not in controls or value == references[key] or not controls[key].minimum <= value <= controls[key].maximum
           for key, value in draft.actions.items()):
        raise ImpactError('الإجراءات لازم تغيّر القيم المرجعية داخل النطاق اللي حددته.')
    result = evaluate(model, base, draft.actions)
    if not assess(result, target, draft.goal.cash_minimum):
        raise ImpactError('الإجراءات دي ما حققتش هدف الربح والسيولة في الحساب الحالي.')
    return {**result, 'baseline': metrics, 'profit_minimum': target,
            'cash_minimum': draft.goal.cash_minimum, 'weeks': WEEKS}
