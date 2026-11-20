"""Immutable expected measurements and post-execution closure requirements."""
from app.decision_loop.eligibility import ineligible_arms
from app.enterprise.outcome_context import measurement_basis
from app.enterprise.scenarios import KPI_NODES, kpi_specs


def from_forecast(graph, forecast, weeks, required, *, source='planner_controls', objective=None, constraints=None):
    nodes = {node.id: node for node in graph.nodes}
    specs = kpi_specs(graph)
    expected = {}
    context = forecast.get('outcome_context') or {}
    for key, value in forecast['kpis'].items():
        if value is None:
            continue
        node_id = objective if objective is not None else KPI_NODES[key]
        node = nodes.get(node_id)
        expected[key] = {
            'node_id': node_id, 'label': node.name_ar if objective is not None else specs[key]['label'],
            'unit': str(node.unit) if node else specs[key]['unit'], 'expected': value,
            'weeks': weeks, 'basis': measurement_basis(graph, node_id, weeks),
            **context.get('indicators', {}).get(node_id, {}),
            'required': key in required,
        }
    return {'indicators': expected, 'assumption_source': source, 'constraints': constraints or {},
            'risk': context.get('risk')}


def legacy_contract(snapshot, forecast):
    return from_forecast(snapshot['graph'], forecast, 1 if snapshot['graph'].onboarding else forecast.get('weeks', 52),
                         {'profit', 'cash_low'}, constraints={
                             key: forecast[key] for key in ('profit_minimum', 'cash_minimum', 'control_bounds') if key in forecast
                         })


def recommendation_forecast(snapshot, review_id, payload, option):
    objective = payload['objective']
    kpi = next((key for key, node in KPI_NODES.items() if node == objective), objective)
    impact = option['impact']
    weeks = impact.get('outcome_context', {}).get('indicators', {}).get(objective, {}).get(
        'weeks', 1 if snapshot['graph'].onboarding else 52)
    forecast = {
        'kpis': {kpi: option['objective']}, 'baseline': {kpi: payload['reference']},
        'assumptions': impact['assumptions'], 'solver_status': impact['solver_status'],
        'weeks': weeks, 'review_id': review_id, 'ineligible_arms': ineligible_arms(impact), 'outcome_context': impact.get('outcome_context'),
    }
    forecast['outcome_contract'] = from_forecast(
        snapshot['graph'], forecast, weeks, {kpi}, source='lab_search_controls', objective=objective,
        constraints={key: payload.get(key) for key in ('target', 'direction', 'cash_floor', 'risk_ceiling')}
        | {'control_bounds': option.get('constraints', {}).get('control_bounds', [])},
    )
    return forecast
