"""Project a weekly reference from the data a company has actually entered."""
from collections import defaultdict

import numpy as np

from app.onboarding.cash_flow import forecast_ledger
from app.onboarding.records import CashTerms
from app.onboarding.presets import INPUTS, ITEMS, OUTPUTS, SECTIONS, field, section_specs
from app.enterprise.impact_graph import CausalGraph, reference
from app.enterprise.schema import Observation, Scalar, Series


def record_key(record):
    return record.key, record.date


def latest_records(records):
    latest = {}
    for record in records:
        previous = latest.get(record.key)
        if previous is None or (record.date and (previous.date is None or record.date > previous.date)):
            latest[record.key] = record
    return latest


def projection_edges(graph):
    return {(edge.source, edge.target): 'projection' for edge in graph.edges if edge.rule is None}


def compute_inputs(graph):
    records = defaultdict(list)
    for record in graph.onboarding.records:
        records[record.key].append(record)
    return {
        key: (Series(frequency='weekly', points=[
            Observation(date=record.date, value=record.value)
            for record in sorted((row for row in rows if row.date), key=lambda row: row.date)
        ]) if rows[-1].date else Scalar(value=rows[0].value))
        for key, rows in records.items() if key in INPUTS
    }


def project_output(key, values, weeks):
    deps = [values[dependency] for dependency in OUTPUTS[key][3]]
    if key == 'sales.volume':
        demand, capacity = deps
        if demand is None:
            return None
        return demand.copy() if capacity is None else np.minimum(demand, capacity)
    if any(value is None for value in deps):
        return None
    if key in ('pnl.revenue', 'pnl.direct_cost'):
        return deps[0] * deps[1]
    if key == 'pnl.result':
        return deps[0] - deps[1] - deps[2] - deps[3]
    return np.maximum(-deps[0] - deps[1], np.zeros(weeks))


def evaluate(causal, weeks, changes, terms=None):
    values, effects, ledger = {}, {}, None
    for key in causal.order:
        if key == 'cash':
            stock = values.get('inventory')
            if 'inventory' not in values and causal.nodes.get('inventory') is not None:
                opening_stock = changes.get('inventory', reference(causal.nodes['inventory']))
                if opening_stock is not None:
                    stock = np.full(weeks, opening_stock)
            ledger = forecast_ledger(values | {'inventory': stock}, terms or CashTerms(), weeks)
            value = None if ledger is None else np.array([row['closing_cash'] for row in ledger])
        elif key in OUTPUTS:
            value = project_output(key, values, weeks)
        else:
            base = reference(causal.nodes[key])
            value = None if base is None else np.full(weeks, changes.get(key, base), dtype=float)
        if value is not None:
            for edge in causal.rules[key]:
                ref = reference(causal.nodes[edge.source])
                if ref in (None, 0) or reference(causal.nodes[key]) is None:
                    continue
                effect = causal.edge_effect(edge, values[edge.source], reference(causal.nodes[key]), weeks, ref)
                if effect is not None:
                    value += effect
                    effects[edge.source, key] = effect
            if key in ('sales.volume', 'capacity', 'demand'):
                value = np.maximum(value, 0)
            if key == 'sales.volume':
                value = np.minimum(value, values['demand'])
                if values['capacity'] is not None:
                    value = np.minimum(value, values['capacity'])
        if key == 'cash' and ledger is not None:
            prior_adjustment = 0
            for row, closing in zip(ledger, value):
                adjustment = float(closing) - row['closing_cash']
                movement = adjustment - prior_adjustment
                row['opening_cash'] += prior_adjustment
                row['closing_cash'] = float(closing)
                row['cash_in'] += max(0, movement)
                row['cash_out'] += max(0, -movement)
                row['cash_adjustment'] = movement
                prior_adjustment = adjustment
        values[key] = value
    return values, effects, ledger


def materialize(graph):
    inputs = compute_inputs(graph)
    nodes = [node.model_copy(update={'baseline': inputs.get(node.id, Scalar(value=None))})
             for node in graph.nodes]
    graph = graph.model_copy(update={'nodes': nodes})
    values, _, _ = evaluate(CausalGraph(graph, built_in_edges=projection_edges(graph)), 1, {},
                            graph.onboarding.cash_terms)
    return graph.model_copy(update={'nodes': [
        node.model_copy(update={'baseline': Scalar(value=float(values[node.id][0]))})
        if node.id in OUTPUTS and values[node.id] is not None else node for node in nodes
    ]})


def provenance(graph):
    sources = {key: row.source for key, row in latest_records(graph.onboarding.records).items()}
    return {node.id: ('missing' if reference(node) is None else sources.get(node.id, 'estimated'))
            for node in graph.nodes}


def readiness(graph):
    sector = graph.onboarding.sector
    sources = provenance(graph)
    latest = latest_records(graph.onboarding.records)
    features = {
        'sales.volume': 'توقع المبيعات', 'pnl.revenue': 'توقع الإيراد', 'pnl.result': 'توقع الربح',
        'cash': 'توقع الكاش', 'risk.shortfall': 'تقدير العجز بعد الائتمان',
    }

    def missing_inputs(key):
        if key in INPUTS:
            return {key} if sources[key] == 'missing' else set()
        deps = ('demand',) if key == 'sales.volume' else OUTPUTS[key][3]
        return set().union(*(missing_inputs(dep) for dep in deps))

    blocked = {key: missing_inputs(key) for key in features if sources[key] == 'missing'}
    sections = []
    complete = total = 0
    for section, name in SECTIONS.items():
        required = [key for key in INPUTS if (spec := field(sector, key)).section == section and not spec.optional]
        missing = [{'key': key, 'label': INPUTS[key].label} for key in required if sources[key] == 'missing']
        count = len(required)
        if section in ITEMS:
            prefix, label = ITEMS[section]
            count += 1
            if not any(key.startswith(prefix + '.') for key in latest):
                missing.append({'key': prefix, 'label': label})
        section_keys = {key for key, spec in INPUTS.items() if spec.section == section}
        blocks = [
            {'feature': features[key], 'fields': sorted(deps & section_keys)}
            for key, deps in blocked.items() if deps & section_keys
        ]
        notes = []
        if section == 'operations' and sources['capacity'] == 'missing':
            notes.append('توقع المبيعات المتاح مؤقتًا مبني على الطلب من غير قيد الطاقة التشغيلية.')
        if section == 'risk' and sources['risk.exposure'] == 'missing':
            blocks.append({'feature': 'عرض مؤشر المخاطر التشغيلية', 'fields': ['risk.exposure']})
        done = count - len(missing)
        complete += done
        total += count
        sections.append({'id': section, 'name': name, 'done': done, 'total': count,
                         'score': round(100 * done / count) if count else None,
                         'missing': missing, 'blocks': blocks, 'notes': notes})
    return {'score': round(100 * complete / total), 'sections': sections,
            'sources': sources, 'fields': section_specs(sector)}
