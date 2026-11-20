"""Project a stored enterprise graph into departments and directed roads."""
from collections import defaultdict
from math import log1p

from app.company_data import finance as company_finance
from app.enterprise.edge_contracts import contract_unit, needs_coefficient
from app.enterprise.impact_graph import ImpactError, reference
from app.enterprise.scenarios import KPI_NODES, kpi_specs
from app.enterprise.schema import NodeType, Series, Unit
from app.enterprise.whatif import ImpactModel
from app.intelligence.decision import controllable
from app.onboarding import projection

DEPARTMENTS = {
    'sales': ('المبيعات والطلب', (NodeType.SALES, NodeType.DEMAND, NodeType.PRICING)),
    'procurement': ('المشتريات والموردين', (NodeType.PROCUREMENT, NodeType.SUPPLIER)),
    'operations': ('الإنتاج والتشغيل', (NodeType.LINE,)),
    'projects': ('المشروعات', (NodeType.PROJECT,)),
    'inventory': ('المخزون والتخزين', (NodeType.INVENTORY, NodeType.STORAGE)),
    'workforce': ('فرق العمل', (NodeType.WORKFORCE,)),
    'accounts': ('الذمم والحسابات', (NodeType.AR, NodeType.AP)),
    'finance': ('السيولة والتمويل', (NodeType.CASH, NodeType.FINANCING, NodeType.CREDIT)),
    'results': ('الأرباح والخسائر', (NodeType.PROFIT_LOSS,)),
    'risk': ('المخاطر', (NodeType.RISK,)),
}
GROUP = {kind: department for department, (_, kinds) in DEPARTMENTS.items() for kind in kinds}
VOLUME_UNITS = {Unit.SAR, Unit.UNITS, Unit.STOCK_SPACE, Unit.TEAMS}
# Prefer a meaningful group headline when the graph contains its standard measure.
HEADLINES = ('sales.volume', 'sales.production', 'demand', 'supplier', 'supplier.delay',
             'capacity', 'inventory', 'cash', 'pnl.result', 'risk.credit', 'risk.shortfall')
HEADLINE_PRIORITY = {key: index for index, key in enumerate(HEADLINES)}


def map_payload(snapshot, company, models, *, demo=False):
    graph = snapshot['graph']
    sources = projection.provenance(graph) if graph.onboarding else {}
    company_data = graph.company_data
    departments_by_key = {key: name for key, (name, _) in DEPARTMENTS.items()}
    if company_data is not None:
        departments_by_key = {'company': 'إجماليات الشركة'}
        departments_by_key.update({key: entity.name for key, entity in company_data.entities.items()})
    grouped = defaultdict(list)
    nodes = {}
    for node in graph.nodes:
        baseline = reference(node)
        source = sources.get(node.id, 'missing' if baseline is None else 'estimated')
        group = GROUP[node.type]
        if company_data is not None:
            entity_ids = company_data.node_entities.get(node.id, ())
            group = next((key for key in entity_ids if company_data.entities[key].kind == 'department'),
                         entity_ids[0] if entity_ids else 'company')
            source = 'actual' if baseline is not None else 'missing'
        nodes[node.id] = {
            'id': node.id, 'name_ar': node.name_ar, 'type': node.type, 'unit': node.unit,
            'department': group, 'baseline': baseline, 'source': source,
            'history': [point.value for point in node.baseline.points[-16:]]
            if isinstance(node.baseline, Series) else [],
        }
        grouped[group].append(nodes[node.id])

    departments = []
    for key, name in departments_by_key.items():
        if key not in grouped:
            continue
        rows = grouped[key]
        # A missing standard headline can still be inspected in departmental detail.
        headline = min(rows, key=lambda row: (
            row['baseline'] is None,
            not row['id'].startswith(('pnl.revenue', 'sales.')) if company_data is not None else False,
            HEADLINE_PRIORITY.get(row['id'], len(HEADLINES)),
        ))
        departments.append({'id': key, 'name_ar': name, 'node_ids': [row['id'] for row in rows],
                            'headline_id': headline['id']})

    drivers, goal_drivers, legacy_edges, analysis_error = [], [], [], None
    try:
        model = ImpactModel(graph, weeks=1)
        drivers, goal_drivers = model.drivers(), controllable(model)
        legacy_edges = [edge for edge, _ in model.legacy_paths]
    except ImpactError as error:
        analysis_error = str(error)

    # Width uses a unit-normalized reference proxy. Dependencies with no
    # throughput-like source (or with missing data) get a thin default road.
    maxima = defaultdict(float)
    for node in nodes.values():
        if node['unit'] in VOLUME_UNITS and node['baseline'] is not None:
            maxima[node['unit']] = max(maxima[node['unit']], abs(node['baseline']))

    edges = []
    nodes_by_id = {node.id: node for node in graph.nodes}
    for edge in [*graph.edges, *legacy_edges]:
        left, right = nodes[edge.source], nodes[edge.target]
        unit = left['unit']
        volume = abs(left['baseline']) if unit in VOLUME_UNITS and left['baseline'] is not None else None
        maximum = maxima[unit]
        edges.append({
            'source': edge.source, 'target': edge.target, 'name_ar': edge.name_ar,
            'from_department': left['department'], 'to_department': right['department'],
            'lag_weeks': edge.rule.lag_weeks if edge.rule else company_finance.cash_lag(edge, company_data),
            'unit': edge.unit or contract_unit(unit, right['unit'], edge.rule, 'built_in'),
            'status': edge.status or 'approved',
            'provenance': edge.provenance.model_dump(mode='json') if edge.provenance else None,
            'can_approve': edge.status == 'draft' and not needs_coefficient(edge, nodes_by_id, None),
            **({'kind': 'aggregation' if edge.evidence == company_finance.ROLLED_UP else 'accounting',
                'source_type': edge.evidence} if company_data is not None and edge.evidence else {}),
            'baseline_volume': volume, 'volume_unit': unit if volume is not None else None,
            'weight': log1p(volume) / log1p(maximum) if volume and maximum else 0,
        })

    kpis, labels = [], kpi_specs(graph)
    for key, node_id in KPI_NODES.items():
        node = nodes.get(node_id, {})
        # The operational template's cash input is its opening balance.
        baseline = None if key == 'cash_low' and graph.arms else node.get('baseline')
        kpis.append({'id': key, 'node_id': node_id, **labels[key], 'baseline': baseline,
                     'history': node.get('history', []),
                     'source': 'missing' if baseline is None else node['source']})
    readiness = projection.readiness(graph)['score'] if graph.onboarding else None
    return {
        'company': company, 'models': models, 'is_demo': demo,
        'model': {key: snapshot[key] for key in ('id', 'version', 'name')},
        'departments': departments, 'nodes': list(nodes.values()), 'edges': edges,
        'kpis': kpis, 'drivers': drivers, 'goal_drivers': goal_drivers,
        'readiness': readiness, 'analysis_error': analysis_error, 'has_engines': bool(graph.arms),
        'has_cash_ledger': company_finance.ledger_ready(graph),
    }
