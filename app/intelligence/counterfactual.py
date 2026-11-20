"""Historical abduction, intervention and replay for an explicit weekly SCM."""
from datetime import timedelta

import numpy as np
from pydantic import Field, model_validator

from app.enterprise.impact_graph import CausalGraph, ImpactError
from app.enterprise.schema import NodeId, Number, Series, StrictFields


class Counterfactual(StrictFields):
    model_id: int = Field(ge=1, lt=2**63)
    target: NodeId
    interventions: dict[NodeId, Number] = Field(min_length=1, max_length=10)
    weeks: int = Field(default=8, ge=1, le=52)

    @model_validator(mode='after')
    def separate_outcome(self):
        if self.target in self.interventions:
            raise ValueError('The outcome cannot also be intervened on')
        return self


def replay(graph, target, interventions, weeks):
    if graph.arms or graph.onboarding:
        raise ImpactError('التحليل التاريخي متاح للنماذج ذات المعادلات الأسبوعية الصريحة.')
    model = CausalGraph(graph)
    if not {target, *interventions}.issubset(model.nodes):
        raise ImpactError('اختار مؤشرات موجودة في النموذج.')
    relevant = {target, *interventions}
    for key in reversed(model.order):
        if key in relevant:
            relevant.update(edge.source for edge in model.rules[key])
    order = [key for key in model.order if key in relevant]
    equations = {key: model.rules[key] for key in order}
    if any(edge.rule is None for edges in equations.values() for edge in edges):
        raise ImpactError('التحليل السببي محتاج قاعدة صريحة لكل علاقة مستخدمة.')
    for key in order:
        node = model.nodes[key]
        if not isinstance(node.baseline, Series) or node.baseline.frequency != 'weekly':
            raise ImpactError('كل متغير مستخدم محتاج سجل أسبوعي فعلي.')
    observed = {key: {point.date: point.value for point in model.nodes[key].baseline.points} for key in order}
    end = max(observed[target])
    lag = max((edge.rule.lag_weeks for edges in equations.values() for edge in edges), default=0)
    days = [end - timedelta(weeks=index) for index in reversed(range(weeks + lag))]
    if any(day not in observed[key] for key in order for day in days):
        raise ImpactError('الفترة المطلوبة وفترة التهيئة محتاجين سجل فعلي مشترك بلا فجوات.')
    facts = {key: np.array([observed[key][day] for day in days]) for key in order}
    anchors = {key: facts[key][-1] for key in order}
    alternative = {}
    result = {}
    for key in order:
        def parents_value(paths):
            total = np.full(len(days), anchors[key], dtype=float)
            for edge in equations[key]:
                total += model.edge_effect(edge, paths[edge.source], anchors[key], len(days), anchors[edge.source])
            return total

        residual = facts[key] - parents_value(facts)
        if key in interventions:
            alternative[key] = np.concatenate((facts[key][:lag], np.full(weeks, interventions[key])))
        else:
            alternative[key] = parents_value(alternative) + residual
        result[key] = {'parents': [edge.model_dump(mode='json') for edge in equations[key]],
                       'anchor': anchors[key], 'abducted_noise': residual[lag:].tolist(),
                       'incoming_mechanism_replaced': key in interventions}

    return {'method': 'weekly_abduction_action_prediction', 'target': target, 'interventions': interventions,
            'warmup_weeks': lag, 'equations': result,
            'assumptions': ['explicit_invariant_edge_mechanisms', 'fixed_factual_exogenous_residuals',
                            'unchanged_preintervention_history'],
            'periods': [{'date': day.isoformat(), 'factual': float(left), 'counterfactual': float(right),
                         'effect': float(right - left)}
                        for day, left, right in zip(days[lag:], facts[target][lag:], alternative[target][lag:])]}
