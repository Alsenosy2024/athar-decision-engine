"""Measurement periods and risk semantics for enterprise calculations."""
from datetime import timedelta
from math import ceil

from app.enterprise.arms import start_date
from app.enterprise.schema import Engine, EngineLink
from app.onboarding.cash_flow import FLOW_LABELS


def measurement_basis(graph, node_id, weeks):
    if graph.onboarding:
        if weeks > 1:
            return 'total_over_period' if node_id in FLOW_LABELS else 'final_value'
        return 'week_end_snapshot'
    if node_id == 'cash':
        return 'minimum_over_period'
    if node_id.startswith(('ar.', 'inventory.')) or node_id in ('financing.draw', 'ap', 'risk.storage'):
        return 'peak_over_period'
    if graph.arms and node_id in ('pnl.result', 'pnl.revenue', 'pnl.direct_cost', 'pnl.expenses', 'financing', 'risk.delay'):
        return 'total_over_period'
    if node_id == 'risk.fill':
        return 'rate_over_period'
    if node_id == 'risk.credit' and graph.arms:
        return 'breach_over_period'
    return 'final_value'


def dated_period(origin, days):
    return {
        'start_on': origin.isoformat(), 'end_on': (origin + timedelta(days=days - 1)).isoformat(),
        'days': days, 'weeks': ceil(days / 7),
    }


def plan_period(model, sector, params):
    days = params['horizon_days'] if sector == 'production' else params['horizon_weeks'] * 7
    if sector in model.arms.sources:
        days = min(days, model.weeks * 7)
    return dated_period(start_date(sector, params), days)


def indicator_period(model, node, evaluation, plans, ledger_nodes):
    period = {'unit': str(node.unit), 'weeks': model.weeks,
              'basis': measurement_basis(model.graph, node.id, model.weeks)}
    link = node.definition
    if model.template and evaluation.cash_start and node.id in ledger_nodes:
        period |= dated_period(evaluation.cash_start, len(evaluation.values['cash']) - 1)
    elif isinstance(link, EngineLink) and link.output.startswith('ledger.') and evaluation.ledger:
        period |= dated_period(evaluation.cash_start, model.weeks * 7)
        if link.output != 'ledger.cash' and period['basis'] == 'final_value':
            period['basis'] = 'total_over_period'
    elif isinstance(link, EngineLink) and link.engine != Engine.FORECAST and link.sector in plans:
        period |= plan_period(model, link.sector, plans[link.sector].parameters)
    elif model.template and node.id == 'sales.production' and 'production' in plans:
        period |= plan_period(model, 'production', plans['production'].parameters)
    if isinstance(link, EngineLink):
        period['execution_source'] = str(link.engine)
    return period


def scenario_context(model, request, evaluation, plans, ledger_nodes, sampled):
    risk = None
    if model.template and 'risk.credit' in model.causal.nodes:
        risk = {
            'node_id': 'risk.credit', 'kind': 'sampled_frequency' if sampled else 'deterministic_breach',
            'samples': request.n if sampled else None, 'seed': request.seed if sampled else None,
        }
    indicators = {node.id: indicator_period(model, node, evaluation, plans, ledger_nodes)
                  for node in model.graph.nodes}
    if evaluation.fill_rate is not None and 'production' in plans:
        indicators['risk.fill'] = {
            'unit': '%', 'basis': 'rate_over_period', 'execution_source': 'production_optimizer',
            **plan_period(model, 'production', plans['production'].parameters),
        }
    return {
        'assumptions': [{'node_id': item.node_id, 'source': 'scenario_input',
                         'operation': item.operation, 'value': item.value,
                         'unit': '%' if item.operation == 'pct' else str(model.causal.nodes[item.node_id].unit),
                         'uncertainty_unit': str(model.causal.nodes[item.node_id].unit)}
                        for item in request.assumptions],
        'indicators': indicators,
        'risk': risk,
    }
