"""Cash projection from approved company totals at a common weekly cutoff."""
import numpy as np

from app.enterprise.schema import Edge, EdgeRule, Formula, Node, Observation, Series
from app.onboarding.cash_flow import forecast_ledger
from app.onboarding.records import CashTerms

PROFIT = {'pnl.revenue': 1, 'pnl.direct_cost': -1, 'pnl.expenses': -1, 'financing': -1}
INPUTS = {'cash.opening': 'cash', 'ar': 'ar', 'ap': 'ap', 'sales.volume': 'company.units_sold',
          'pnl.result': 'pnl.result', **{key: key for key in PROFIT}}
ROLLED_UP = 'approved_dimension_rollup'
PROFIT_IDENTITY = 'approved_financial_identity'
CASH_IDENTITY = 'company_cash_ledger'


def aligned_series(nodes, units):
    series = {}
    for key, unit in units.items():
        node = nodes.get(key)
        if not node or node.unit != unit or not isinstance(node.baseline, Series):
            return {}
        series[key] = node.baseline.points
    return series if len({points[-1].date for points in series.values()}) == 1 else {}


def add_financial_nodes(nodes, edges):
    by_id = {node.id: node for node in nodes}
    series = aligned_series(by_id, dict.fromkeys(PROFIT, 'SAR'))
    if not series:
        return
    histories = {key: {point.date: point.value for point in points} for key, points in series.items()}
    shared = set.intersection(*(set(values) for values in histories.values()))
    profit = Node(id='pnl.result', type='profit_loss', name_ar='النتيجة بعد التمويل', unit='SAR',
                  baseline=Series(frequency='weekly', points=[
                      Observation(date=day, value=sum(PROFIT[key] * history[day] for key, history in histories.items()))
                      for day in sorted(shared)]),
                  definition=Formula(expression='Revenue - direct cost - operating expenses - financing'))
    nodes.append(profit)
    edges.extend(Edge(source=key, target=profit.id, name_ar='معادلة الربح بعد التمويل',
                      rule=EdgeRule(multiplier=coefficient), evidence=PROFIT_IDENTITY)
                 for key, coefficient in PROFIT.items())
    by_id[profit.id] = profit
    if ledger_inputs(by_id):
        edges.extend(Edge(source=key, target='cash', name_ar='دفتر التحصيل والسداد', evidence=CASH_IDENTITY)
                     for key in ('pnl.revenue', 'pnl.direct_cost', 'pnl.expenses', 'financing',
                                 'ar', 'ap', 'company.units_sold'))


def ledger_inputs(nodes):
    return aligned_series(nodes, {key: 'units' if name == 'sales.volume' else 'SAR' for name, key in INPUTS.items()})


def ledger_ready(graph):
    return graph.company_data is not None and any(edge.evidence == CASH_IDENTITY for edge in graph.edges)


def cash_lag(edge, data):
    if edge.evidence != CASH_IDENTITY or data is None or data.cash_terms is None:
        return 0
    terms = data.cash_terms
    if edge.source == 'pnl.revenue':
        return terms.collection_lag_weeks
    if edge.source in ('pnl.direct_cost', 'company.units_sold'):
        return terms.payment_lag_weeks
    return 0


def flow_nodes(data):
    measures = {measure.key for mapping in data.mappings.values() for measure in mapping.measures
                if measure.aggregation == 'sum'}
    return {'pnl.result'} | {key for key, measure in data.node_measures.items() if measure in measures}


def project_ledger(values, terms, weeks):
    terms = terms or CashTerms()
    inputs = {name: values[key] for name, key in INPUTS.items()}
    sold, cost = inputs['sales.volume'], inputs['pnl.direct_cost']
    inputs['unit_cost'] = np.divide(cost, sold, out=np.zeros_like(cost), where=sold > 0)
    inputs['inventory'] = values.get('inventory.company')
    return forecast_ledger(inputs, terms, weeks, purchase_amounts=cost)
