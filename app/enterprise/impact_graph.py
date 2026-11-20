"""Compile weekly causal rules and validate the enterprise execution order."""
from collections import defaultdict
from graphlib import CycleError, TopologicalSorter
from typing import Literal

import numpy as np
from pydantic import Field, model_validator

from app.enterprise.edge_contracts import EdgeContractError, conversion, needs_coefficient
from app.enterprise.schema import Graph, NodeId, Number, Scalar, StrictFields


class ImpactError(ValueError):
    """An impact calculation has no valid input or plan."""


class Assumption(StrictFields):
    node_id: NodeId
    operation: Literal['set', 'add', 'pct'] = 'set'
    value: Number
    uncertainty: Number = Field(default=0, ge=0)


class ScenarioAssumptions(StrictFields):
    model_id: int | None = Field(default=None, ge=1, strict=True)
    version: int | None = Field(default=None, ge=1, strict=True)
    assumptions: list[Assumption] = Field(min_length=1, max_length=50)

    @model_validator(mode='after')
    def valid_selection(self):
        if self.version is not None and self.model_id is None:
            raise ValueError('Version requires a saved model ID')
        if len({change.node_id for change in self.assumptions}) != len(self.assumptions):
            raise ValueError('Apply one assumption per driver')
        return self


class WhatIfRequest(ScenarioAssumptions):
    n: int = Field(default=200, ge=1, le=1000, strict=True)
    seed: int = Field(default=42, ge=0, le=2**32 - 1, strict=True)
    weeks: int = Field(default=52, ge=1, le=104, strict=True)


def reference(node):
    if isinstance(node.baseline, Scalar):
        return node.baseline.value
    return node.baseline.points[-1].value


class CausalGraph:
    def __init__(self, graph: Graph, *, built_in_edges=(), bound=(), drivers=()):
        self.nodes = {node.id: node for node in graph.nodes}
        self.bound = set(bound)
        self.rules = defaultdict(list)
        self.executable = []
        self.conversions = {}
        self.drivers = set(drivers)
        dependencies = {node_id: set() for node_id in self.nodes}
        for edge in graph.edges:
            pair = edge.source, edge.target
            method = built_in_edges.get(pair) if built_in_edges else None
            if edge.status == 'draft' or (edge.status is None and edge.unit is None
                                          and needs_coefficient(edge, self.nodes, method)):
                continue
            try:
                self.conversions[pair] = conversion(edge, self.nodes, method)
            except EdgeContractError as error:
                raise ImpactError(str(error)) from None
            if edge.rule is not None and method is not None:
                raise ImpactError(f'العلاقة {edge.source} ← {edge.target} مرتبطة بحساب جاهز.')
            if method is None:
                if edge.target in self.bound:
                    raise ImpactError(f'العلاقة إلى {edge.target} محتاجة ربط معروف للحساب.')
                self.rules[edge.target].append(edge)
            # The opening balance is an independent input. The draw loop is solved
            # in the ledger, using closing cash for each period.
            if method not in ('opening_cash', 'financing_feedback'):
                dependencies[edge.target].add(edge.source)
            self.executable.append((edge, method or 'analytic'))

        try:
            self.order = list(TopologicalSorter(dependencies).static_order())
        except CycleError as exc:
            cycle = ' ← '.join(exc.args[1])
            raise ImpactError(f'فيه دورة في علاقات الأثر ({cycle}).') from None

        # Resolve all analytic ancestors of model inputs before planning.
        pending = sorted(self.drivers)
        self.upstream = set(pending)
        while pending:
            for edge in self.rules[pending.pop()]:
                if edge.source in self.bound:
                    raise ImpactError(f'علاقة {edge.source} بترجع ناتج المحرك إلى مدخلات التخطيط.')
                if edge.rule is not None and edge.rule.lag_weeks:
                    raise ImpactError(f'مدخل المحرك {edge.target} محتاج علاقة من غير تأخير.')
                if edge.source not in self.upstream:
                    self.upstream.add(edge.source)
                    pending.append(edge.source)

    def adjustable(self, key):
        return (key in self.nodes and (key not in self.bound or key in self.drivers)
                and reference(self.nodes[key]) is not None)

    def propagate(self, weeks, assumptions, *, computed=None, upstream=False, anchors=None):
        values = dict(computed or {})
        effects = {}
        for key in self.order:
            if key in values or (upstream and key not in self.upstream):
                continue
            base = reference(self.nodes[key])
            if base is None:
                values[key] = None
                continue
            value = np.full(weeks, assumptions.get(key, base), dtype=float)
            for edge in self.rules[key]:
                anchor = (anchors or {}).get(edge.source)
                effect = self.edge_effect(edge, values[edge.source], base, weeks, anchor)
                if effect is not None:
                    value += effect
                    effects[edge.source, key] = effect
            values[key] = value
        return values, effects

    def edge_effect(self, edge, source, target_reference, weeks, anchor):
        source_reference = reference(self.nodes[edge.source]) if anchor is None else anchor
        if source is None or source_reference is None:
            return None
        rule = edge.rule
        source = np.asarray(source)
        if source.size != weeks:
            source = np.full(weeks, source[-1])
        delta = (source - source_reference) * self.conversions[edge.source, edge.target]
        if rule is None:
            return delta
        if rule.mode == 'elasticity':
            if source_reference == 0:
                raise ImpactError(f'معامل المرونة محتاج قيمة مرجعية غير صفرية ({edge.source}).')
            delta = delta / source_reference * target_reference
        delta = delta * rule.multiplier
        # Bounds constrain the edge's implied target value. Subtract the
        # bounded reference so a capacity cannot generate a phantom shock.
        if rule.minimum is not None or rule.maximum is not None:
            delta = (np.clip(target_reference + delta, rule.minimum, rule.maximum)
                     - np.clip(target_reference, rule.minimum, rule.maximum))
        lag = min(rule.lag_weeks, weeks)
        return np.concatenate((np.zeros(lag), delta[:weeks - lag]))
