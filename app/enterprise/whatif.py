"""Company-wide scenario execution and explanations."""
from dataclasses import dataclass
from datetime import date, timedelta
from math import isfinite

import numpy as np

from app.enterprise.result_context import WEEKLY_CASH_LABEL, calculation_weeks
from app.enterprise.arms import Arms, contracting_ledger, production_ledger, start_date
from app.enterprise.demo import demo_template
from app.enterprise.finance import Ledger
from app.enterprise.impact_graph import CausalGraph, ImpactError, reference
from app.enterprise.schema import Engine, EngineLink
from app.forecasting.methods import quantiles
from app.onboarding import projection
from app.onboarding.presets import INPUTS as ONBOARDING_INPUTS
from app.simulation.montecarlo import driver_trials

FINANCE = {
    'cash', 'financing.draw', 'financing', 'ar.production', 'ar.contracting', 'ap',
    'pnl.revenue', 'pnl.direct_cost', 'pnl.expenses', 'pnl.result', 'procurement', 'risk.credit',
}
INPUTS = {
    'production': {'cash', 'credit', 'storage', 'supplier', 'supplier.payment_lag'},
    'contracting': {'cash', 'credit'},
}


def is_driver(key, sector):
    prefix = ('demand.', 'price.', 'downtime.') if sector == 'production' else ('workforce.',)
    return key in INPUTS[sector] or key.startswith(prefix)


def metric(key, values):
    if values is None:
        return None
    if key == 'cash':
        return float(min(values))
    if key.startswith(('ar.', 'inventory.')) or key in ('financing.draw', 'ap', 'risk.storage'):
        return float(max(values))
    return float(values[-1])


def changed(before, after):
    return (before.shape != after.shape or not np.allclose(before, after, rtol=0, atol=0.00001)
            if before is not None and after is not None else before is not after)


def standard_graph(graph):
    """Support existing snapshots of the Phase 10 demo without rewriting them."""
    template = demo_template().graph
    nodes = {node.id: node for node in graph.nodes}
    expected = {node.id: node for node in template.nodes}
    legacy_ids = {key for key in expected if not key.startswith('downtime.')
                  and key not in ('pnl.expenses', 'supplier.payment_lag')}
    sectors = {arm.sector for arm in graph.arms}
    compatible = sectors == {'production', 'contracting'} and all(
        key in nodes and nodes[key].type == expected[key].type
        and nodes[key].definition == expected[key].definition
        for key in legacy_ids - {'pnl.result'}
    ) and 'pnl.result' in nodes
    if compatible:
        return template
    return None


def edge_methods(graph, template, bound):
    if template is None:
        return {}
    methods = {}
    pairs = {(edge.source, edge.target) for edge in template.edges}
    # Kept for stored Phase 10 graphs.
    pairs.add(('procurement', 'pnl.direct_cost'))
    for edge in graph.edges:
        pair = edge.source, edge.target
        if pair not in pairs:
            continue
        if pair == ('financing.draw', 'cash'):
            method = 'financing_feedback'
        elif edge.source == 'cash' and edge.target.startswith(('line.', 'project.')):
            method = 'opening_cash'
        elif edge.target.startswith(('line.', 'project.')):
            method = 'optimizer'
        elif edge.target in bound:
            method = 'accounting'
        else:
            continue
        methods[pair] = method
    return methods


@dataclass
class Evaluation:
    values: dict
    analytic_effects: dict
    iterations: int
    cash_start: date | None = None
    fill_rate: float | None = None

    def metrics(self):
        return {key: metric(key, values) for key, values in self.values.items()}


class ImpactModel:
    def __init__(self, graph, *, weeks, time_limit=15, forecast_engine='stat', foundation=None):
        self.weeks = calculation_weeks(graph, weeks)
        self.graph = graph
        self.template = standard_graph(graph) if graph.arms else None
        self.arms = Arms((arm.sector for arm in graph.arms), time_limit=time_limit,
                         forecast_engine=forecast_engine, foundation=foundation)
        links = {node.id: node.definition for node in graph.nodes if isinstance(node.definition, EngineLink)}
        keys = {node.id for node in graph.nodes}
        bound = set(links)
        drivers = set()
        if self.template:
            bound |= FINANCE | {'sales.production', 'risk.delay', 'risk.storage'}
            drivers = {key for key in keys if any(is_driver(key, sector) for sector in self.arms.datasets)}
            bound -= drivers - {'cash'}
        built_in = (projection.projection_edges(graph) if graph.onboarding
                    else edge_methods(graph, self.template, bound))
        self.causal = CausalGraph(graph, built_in_edges=built_in,
                                  bound=bound & keys, drivers=drivers)
        self.links = links
        self.legacy_paths = []
        if self.template and not ({'pnl.expenses', 'supplier.payment_lag'} & keys):
            existing = {(edge.source, edge.target) for edge in graph.edges}
            # Legacy snapshots predate these accounting links. They can
            # participate in explanations when both modeled endpoints exist.
            missing = [edge for edge in self.template.edges if edge.source in keys and edge.target in keys
                       and (edge.source, edge.target) not in existing]
            methods = edge_methods(graph.model_copy(update={'edges': missing}), self.template, bound)
            self.legacy_paths = [(edge, methods[edge.source, edge.target]) for edge in missing]

    def adjustable(self, key):
        return self.causal.adjustable(key) and (not self.graph.onboarding or key in ONBOARDING_INPUTS)

    def drivers(self):
        return [
            dict(id=node.id, name_ar='السيولة الافتتاحية' if self.template and node.id == 'cash' else node.name_ar,
                 value=reference(node), unit=node.unit)
            for node in self.graph.nodes if self.adjustable(node.id)
        ]

    def input_values(self, changes):
        if self.graph.onboarding:
            for key, value in changes.items():
                lower = -1e12 if key == 'cash.opening' else 0
                upper = 100 if key == 'risk.exposure' else 1e12
                if not lower <= value <= upper:
                    raise ImpactError(f'قيمة {key} خارج النطاق المتاح.')
            return None, {}, {}, {}
        upstream, effects = self.causal.propagate(self.weeks, changes, upstream=True)

        def get(key, default):
            if key.startswith('demand_factor.'):
                key = key.replace('demand_factor.', 'demand.', 1)
                values = upstream.get(key)
                if values is None:
                    return default
                base = reference(self.causal.nodes[key])
                return values[0] / base if base else 1.0
            values = upstream.get(key)
            return float(values[0]) if values is not None else default

        for key, values in upstream.items():
            if key in self.causal.drivers and values is not None and (
                    not np.isfinite(values).all() or min(values) < 0 or max(values) > 1e10):
                raise ImpactError(f'قيمة {key} لازم تكون موجبة أو صفر وفي النطاق المدعوم.')
        if get('supplier.payment_lag', 0) > 365:
            raise ImpactError('مهلة سداد الخامات حدها 365 يوم.')
        arms = {sector: self.arms.inputs(sector, get) for sector in self.arms.datasets}
        return get, arms, upstream, effects

    def plans(self, inputs, phase, previous=None):
        required = set(inputs) if self.template else {
            link.sector for link in self.links.values() if link.engine != Engine.FORECAST
        }
        return {sector: self.arms.plan(sector, *args, phase, (previous or {}).get(sector))
                for sector, args in inputs.items() if sector in required}

    def computed(self, get, inputs, plans, *, trial=False):
        if not inputs:
            return {}, 0, None, None
        starts = {sector: start_date(sector, params) for sector, (_, params) in inputs.items()}
        origin = min(starts.values())
        offsets = {sector: (start - origin).days for sector, start in starts.items()}
        payable_lag = round(get('supplier.payment_lag', 0))
        ledger = Ledger(days=1)
        projections = {}
        iterations = 0
        for sector, plan in plans.items():
            offset, (_, params) = offsets[sector], inputs[sector]
            if sector == 'production':
                projections[sector] = production_ledger(inputs[sector], plan, offset, payable_lag, ledger, trial=trial)
                ledger.days = max(ledger.days, offset + params['horizon_days'] + max(params['payment_lag_days'], payable_lag))
            else:
                projections[sector] = contracting_ledger(inputs[sector], plan, offset, ledger, trial=trial)
                ledger.days = max(ledger.days, offset + (params['horizon_weeks'] + params['payment_lag_weeks']) * 7 + 1)

        values = {}
        if self.template:
            ledger.days = max(ledger.days, 1 + max((max(book, default=0) for book in ledger.books.values()), default=0))
            values, iterations = ledger.close(get('cash', 0), get('credit', 0),
                                              inputs['contracting'][1]['weekly_interest'])
            values['sales.production'] = [projections['production']['booked']]

        for key, link in self.links.items():
            entity = link.entity_id
            if link.engine == Engine.FORECAST:
                if self.template and key.startswith('demand.'):
                    continue  # The driver is the reference weekly demand; forecasts stay separate.
                series = self.arms.forecast(link.sector)[entity][link.output.split('.')[1]]
                value = np.array(series)
            else:
                data = projections[link.sector]
                value = {
                    'plan.schedule': lambda: data['lines'][entity],
                    'plan.inventory': lambda: data['inventory'][entity],
                    'kpis.peak_storage_pct': lambda: data['storage'],
                    'plan.activities': lambda: [data['projects'][entity]],
                    'kpis.late_weeks_total': lambda: [data['delay']],
                }[link.output]()
            values[key] = value
        return {key: np.asarray(value, dtype=float) for key, value in values.items()
                if key in self.causal.nodes}, iterations, origin, projections.get('production', {}).get('fill_rate')

    def evaluate(self, changes, inputs, plans, baseline=None, *, trial=False):
        get, arm_inputs, upstream, upstream_effects = inputs
        if self.graph.onboarding:
            values, effects = projection.evaluate(self.causal, self.weeks, changes)
            return Evaluation(values, effects, 0)
        computed, iterations, cash_start, fill_rate = self.computed(get, arm_inputs, plans, trial=trial)
        anchors = {
            key: metric(key, baseline.values.get(key) if baseline else value)
            for key, value in computed.items()
        }
        # A computed daily/plan output feeds analytic edges as its summary.
        raw = upstream | {key: np.full(self.weeks, metric(key, value)) for key, value in computed.items()}
        values, effects = self.causal.propagate(self.weeks, changes, computed=raw, anchors=anchors)
        values.update(computed)
        return Evaluation(values, effects | upstream_effects, iterations, cash_start, fill_rate)

    def assumption_values(self, assumptions):
        centers, spreads, reference_values = {}, {}, {}
        for item in assumptions:
            key = item.node_id
            if not self.adjustable(key):
                raise ImpactError(f'العنصر {key} مش مدخل قابل للتعديل.')
            base = reference(self.causal.nodes[key])
            center = {'set': lambda: item.value, 'add': lambda: base + item.value,
                      'pct': lambda: base * (1 + item.value / 100)}[item.operation]()
            if not all(isfinite(value) for value in (center, center - item.uncertainty, center + item.uncertainty)):
                raise ImpactError('نطاق الفرضية أكبر من النطاق الرقمي المدعوم.')
            if item.uncertainty and not center - item.uncertainty < center < center + item.uncertainty:
                raise ImpactError('مدى عدم اليقين أصغر من دقة القيمة المدخلة.')
            if self.template and key.startswith('demand.') and base == 0 and (center or item.uncertainty):
                raise ImpactError('تغيير طلب الديمو محتاج طلب مرجعي غير صفري.')
            centers[key], spreads[key], reference_values[key] = center, item.uncertainty, base

        central_inputs = self.input_values(centers)
        for key, spread in spreads.items():
            if spread:
                for bound in (centers[key] - spread, centers[key] + spread):
                    self.input_values(centers | {key: bound})
        return centers, spreads, reference_values, central_inputs

    def baseline(self):
        base_inputs = self.input_values({})
        base_plans = self.plans(base_inputs[1], 'baseline')
        return self.evaluate({}, base_inputs, base_plans), base_plans

    def scenario(self, request, *, base=None):
        centers, spreads, reference_values, central_inputs = self.assumption_values(request.assumptions)
        baseline, base_plans = self.baseline() if base is None else base
        central_plans = self.plans(central_inputs[1], 'scenario', base_plans)
        central = self.evaluate(centers, central_inputs, central_plans, baseline)
        before, after = baseline.metrics(), central.metrics()

        samples = {key: [] for key, value in after.items() if value is not None}
        fill_rates = []
        active_edges = set()
        root_changes = {key for key in centers if centers[key] != reference_values[key] or spreads[key]}
        max_iterations = max(baseline.iterations, central.iterations)
        if any(spreads.values()):
            for sample in driver_trials(centers, spreads, request.n, request.seed):
                evaluation = self.evaluate(sample, self.input_values(sample), central_plans, baseline, trial=True)
                max_iterations = max(max_iterations, evaluation.iterations)
                for key in samples:
                    samples[key].append(metric(key, evaluation.values[key]))
                if evaluation.fill_rate is not None:
                    fill_rates.append(evaluation.fill_rate)
                active_edges.update(self.carried_edges(baseline, evaluation, sample, reference_values))
        else:
            samples = {key: [after[key]] * request.n for key in samples}
            if central.fill_rate is not None:
                fill_rates = [central.fill_rate] * request.n
        active_edges.update(self.carried_edges(baseline, central, centers, reference_values))
        bands = {key: quantiles(values) for key, values in samples.items()}
        if 'risk.credit' in samples:
            after['risk.credit'] = float(np.mean(samples['risk.credit']))

        paths = self.path_explanation(root_changes, active_edges)
        rows = []
        sources = projection.provenance(self.graph) if self.graph.onboarding else {}
        for node in self.graph.nodes:
            key = node.id
            left, right = before[key], after[key]
            delta = right - left if left is not None and right is not None else None
            pct = 100 * delta / abs(left) if left and delta is not None else None
            if any(value is not None and not isfinite(value) for value in (left, right, delta, pct)):
                raise ImpactError('الأثر أكبر من النطاق الرقمي المدعوم.')
            rows.append({
                'id': key, 'name_ar': WEEKLY_CASH_LABEL if self.graph.onboarding and key == 'cash' else node.name_ar,
                'type': node.type, 'unit': node.unit,
                'before': left, 'after': right, 'delta': delta, 'delta_pct': pct,
                'quantiles': bands.get(key), 'path': paths.get(key, []),
                **({'source': sources[key],
                    'scenario_source': 'estimated' if key in centers or key in paths else sources[key]}
                   if sources else {}),
                'affected': (key in root_changes or key in paths or changed(baseline.values[key], central.values[key])
                             or any(changed(np.array([left]), np.array([value]))
                                    for value in bands.get(key, {}).values())),
            })
        return {
            'nodes': rows, 'assumptions': [dict(node_id=key, before=reference_values[key], after=value,
                                               uncertainty=spreads[key]) for key, value in centers.items()],
            'paths': [edge for incoming in paths.values() for edge in incoming],
            'charts': self.charts(baseline, central),
            'fill_rate': central.fill_rate,
            'fill_rate_baseline': baseline.fill_rate,
            'fill_rate_quantiles': quantiles(fill_rates) if fill_rates else None,
            'simulation': {'n': request.n, 'seed': request.seed, 'max_financing_iterations': max_iterations},
            'optimizers_run': self.arms.optimizers_run,
            'solver_status': {sector: plan.status for sector, plan in central_plans.items()},
            **({'onboarding': projection.readiness(self.graph)} if sources else {}),
        }

    def carried_edges(self, base, scenario, assumptions, references):
        differences = {key for key, value in base.values.items() if changed(value, scenario.values[key])}
        result = set()
        for edge, method in self.causal.executable + self.legacy_paths:
            source, target = edge.source, edge.target
            if target not in differences:
                continue
            if method == 'analytic':
                left = base.analytic_effects.get((source, target))
                right = scenario.analytic_effects.get((source, target))
                if changed(left, right):
                    result.add((source, target))
            elif method == 'opening_cash':
                if assumptions.get('cash', references.get('cash')) != references.get('cash'):
                    result.add((source, target))
            elif source in differences:
                result.add((source, target))
        return result

    def path_explanation(self, roots, active_edges):
        reachable = set(roots)
        edges = self.causal.executable + self.legacy_paths
        result = {}
        while True:
            count = len(reachable)
            for edge, method in edges:
                if (edge.source, edge.target) in active_edges and edge.source in reachable:
                    reachable.add(edge.target)
                    result.setdefault(edge.target, {})[edge.source] = {
                        **edge.model_dump(mode='json'), 'method': method,
                    }
            if len(reachable) == count:
                return {key: list(incoming.values()) for key, incoming in result.items()}

    def charts(self, baseline, central):
        fields = [key for key in ('pnl.revenue', 'pnl.direct_cost', 'pnl.expenses', 'financing', 'pnl.result')
                  if key in baseline.values and baseline.values[key] is not None]
        charts = {'pnl': [{'id': key, 'before': metric(key, baseline.values[key]),
                           'after': metric(key, central.values[key])} for key in fields],
                  'cash': None}
        if self.template:
            before, after = (evaluation.values['cash'].tolist() for evaluation in (baseline, central))
            dates = [(baseline.cash_start + timedelta(days=index - 1)).isoformat()
                     for index in range(max(len(before), len(after)))]
            charts['cash'] = {'dates': dates, 'before': before, 'after': after}
        elif self.graph.onboarding and baseline.values['cash'] is not None:
            charts['cash'] = {'period_ar': 'نهاية الأسبوع',
                              'before': [float(baseline.values['cash'][-1])],
                              'after': [float(central.values['cash'][-1])]}
        return charts
