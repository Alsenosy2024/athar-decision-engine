"""Materialize approved source grains and hierarchy totals into the impact graph."""
from collections import defaultdict
from datetime import timedelta
from hashlib import sha256
from math import fsum

from app.company_data import finance
from app.company_data.schema import CompanyData
from app.enterprise.schema import Edge, EdgeRule, Formula, Graph, Node, Observation, Scalar, Series
from app.schemas import api_error

TYPES = {'sales': 'sales', 'units_sold': 'sales', 'stock': 'inventory', 'receivables': 'receivables',
         'amount': 'profit_loss', 'cost': 'profit_loss', 'cash': 'cash', 'payables': 'payables',
         'expenses': 'profit_loss', 'financing': 'financing'}
TOTALS = {'sales': 'pnl.revenue', 'cost': 'pnl.direct_cost', 'cash': 'cash', 'stock': 'inventory.company',
          'expenses': 'pnl.expenses', 'financing': 'financing', 'receivables': 'ar', 'payables': 'ap'}
MAX_GRAPH_POINTS = 80_000
MAX_GRAPH_NODES = 5_000


def empty_graph():
    # The existing Graph contract requires one node. It carries no baseline or rules.
    return Graph(company_data=CompanyData(), nodes=[
        Node(id='company.setup', type='risk_kpi', name_ar='في انتظار اعتماد البيانات', unit='units',
             baseline=Scalar(value=None), definition=Formula(expression='Awaiting approved source records'))
    ])


def project(data):
    measures = {item.key: item for mapping in data.mappings.values() for item in mapping.measures}
    records = defaultdict(list)
    grains = defaultdict(set)
    for item in data.observations:
        records[item.measure, item.entities].append(item)
        grains[item.measure].add(frozenset(data.entities[key].kind for key in item.entities))
    if any(len(kinds) > 1 for kinds in grains.values()):
        raise api_error(422, 'company_mixed_grain',
                        'ثبّت نفس أبعاد التفاصيل لكل مؤشر. بيانات الإجمالي ممكن تكرر أرقام التفاصيل.')

    nodes, edges, membership, node_measures = [], [], {}, {}
    all_points = 0

    for metric in sorted(grains):
        measure = measures[metric]
        series = {grain: rows for (key, grain), rows in records.items() if key == metric}
        days = sorted({row.day - timedelta(days=row.day.weekday()) for rows in series.values() for row in rows})
        total = TOTALS.get(metric, 'company.' + metric)
        values = defaultdict(lambda: defaultdict(list))

        def connect(source, target):
            edges.append(Edge(source=source, target=target, name_ar='تجميع من السجلات المعتمدة',
                              rule=EdgeRule(multiplier=1), evidence=finance.ROLLED_UP))

        def make_node(identity, name, entities, points, *, leaf=False):
            nonlocal all_points
            all_points += len(points)
            if len(nodes) >= MAX_GRAPH_NODES or all_points > MAX_GRAPH_POINTS:
                raise api_error(422, 'company_graph_too_large', 'البيانات أكبر من حد عقد الجراف أو نقاطه.')
            membership[identity] = entities
            node_measures[identity] = metric
            nodes.append(Node(
                id=identity, type=TYPES.get(metric, 'risk_kpi'), name_ar=name[:160], unit=measure.unit,
                baseline=Series(frequency='weekly', points=[
                    Observation(date=day, value=value) for day, value in points.items()
                ]),
                definition=Formula(expression=('Approved source observations' if leaf else 'Sum of approved source scopes')),
            ))

        if len(days) * len(series) > MAX_GRAPH_POINTS:
            raise api_error(422, 'company_graph_too_large', 'عدد الفترات والتقاطعات أكبر من حد الجراف.')
        for grain, rows in series.items():
            by_week = defaultdict(list)
            for row in rows:
                by_week[row.day - timedelta(days=row.day.weekday())].append(row.value)
            points, last = {}, None
            for day in days:
                readings = by_week.get(day)
                if measure.aggregation == 'sum':
                    points[day] = fsum(readings or ())
                elif readings or last is not None:
                    last = readings[-1] if readings else last
                    points[day] = last

            if not grain:
                make_node(total, measure.name, (), points, leaf=True)
                continue
            identity = 'detail.' + metric + '.' + sha256('|'.join(grain).encode()).hexdigest()[:24]
            scope = [data.entities[key] for key in grain]
            make_node(identity, measure.name + ' · ' + ' / '.join(item.name for item in scope), grain, points, leaf=True)
            if not any(item.kind == 'branch' for item in scope):
                connect(identity, total)
            for entity in scope:
                connect(identity, metric + '.' + entity.id)
                while entity:
                    for day, value in points.items():
                        values[entity.id][day].append(value)
                    entity = data.entities.get(entity.parent_id)
            for day, value in points.items():
                values[''][day].append(value)

        for key, observations in sorted(values.items()):
            entity = data.entities.get(key)
            identity = metric + '.' + key if entity else total
            make_node(identity, measure.name + ' · ' + entity.name if entity else measure.name,
                      (key,) if entity else (), {day: fsum(observations[day]) for day in sorted(observations)})
            if entity and entity.parent_id:
                connect(identity, metric + '.' + entity.parent_id)
            elif entity and entity.kind == 'branch':
                connect(identity, total)

    if not nodes:
        return empty_graph().model_copy(update={'company_data': data})
    finance.add_financial_nodes(nodes, edges)
    return Graph(nodes=nodes, edges=edges, company_data=data.model_copy(update={
        'node_entities': membership, 'node_measures': node_measures,
    }))
