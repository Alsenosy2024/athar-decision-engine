"""Turn source-backed mappings into a versioned ontology diff."""
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import re

import numpy as np
from pydantic import Field

from app.enterprise.impact_graph import CausalGraph, ImpactError
from app.enterprise.schema import Edge, EdgeRule, Graph, ModelInput, Node, NodeType, Scalar, Series, StrictFields, Unit
from app.intelligence.documents import Evidence
from app.intelligence.grounding import select
from app.llm.rules_ar import normalize

# Canonical IDs match the enterprise model where the concept already exists.
CATALOG = {
    'cash': (NodeType.CASH, Unit.SAR, 'السيولة', ('cash', 'cash balance', 'الكاش', 'النقدية', 'رصيد النقدية', 'سيولة')),
    'credit': (NodeType.CREDIT, Unit.SAR, 'حد الائتمان', ('credit limit', 'credit facility', 'التسهيل الائتماني')),
    'ar': (NodeType.AR, Unit.SAR, 'الذمم المدينة', ('accounts receivable', 'receivables', 'AR', 'العملاء')),
    'ap': (NodeType.AP, Unit.SAR, 'الذمم الدائنة', ('accounts payable', 'payables', 'AP', 'الدائنون')),
    'pnl.revenue': (NodeType.SALES, Unit.SAR, 'الإيرادات', ('revenue', 'total revenue', 'sales', 'المبيعات', 'إجمالي الإيرادات')),
    'pnl.result': (NodeType.PROFIT_LOSS, Unit.SAR, 'صافي الربح', ('net profit', 'net income', 'profit', 'الربح')),
    'pnl.expenses': (NodeType.PROFIT_LOSS, Unit.SAR, 'مصروفات التشغيل', ('expenses', 'operating expenses', 'المصروفات')),
    'pnl.direct_cost': (NodeType.PROFIT_LOSS, Unit.SAR, 'التكلفة المباشرة', ('direct cost', 'cogs', 'cost of sales', 'تكلفة المبيعات')),
    'demand': (NodeType.DEMAND, Unit.UNITS, 'الطلب', ('demand', 'orders quantity', 'حجم الطلب')),
    'price': (NodeType.PRICING, Unit.SAR_PER_UNIT, 'سعر الوحدة', ('unit price', 'price', 'السعر', 'التسعير')),
    'storage': (NodeType.STORAGE, Unit.STOCK_SPACE, 'سعة المخزن', ('warehouse', 'storage capacity', 'المخزن', 'مخزن')),
    'inventory': (NodeType.INVENTORY, Unit.UNITS, 'المخزون', ('inventory', 'stock', 'رصيد المخزون')),
    'workforce': (NodeType.WORKFORCE, Unit.TEAMS, 'عدد الفرق', ('workforce teams', 'crew count', 'teams', 'فرق العمل')),
    'production': (NodeType.LINE, Unit.UNITS, 'الإنتاج', ('production', 'production volume', 'كمية الإنتاج')),
    'procurement': (NodeType.PROCUREMENT, Unit.SAR, 'المشتريات', ('procurement', 'purchases', 'تكلفة المشتريات')),
    'financing': (NodeType.FINANCING, Unit.SAR, 'تكلفة التمويل', ('financing cost', 'interest expense', 'فوائد التمويل')),
    'project.cost': (NodeType.PROJECT, Unit.SAR, 'تكلفة المشروع', ('project cost', 'planned cost', 'تكلفة العقد')),
    'supplier.count': (NodeType.SUPPLIER, Unit.UNITS, 'عدد الموردين', ('supplier', 'suppliers', 'الموردين', 'موردين')),
    'supplier.lead_time': (NodeType.SUPPLIER, Unit.DAYS, 'مهلة المورد', ('supplier lead time', 'lead time', 'مدة التوريد')),
    'risk.credit': (NodeType.RISK, Unit.PERCENT, 'احتمال تجاوز الائتمان', ('credit breach probability', 'credit risk')),
}
NUMBER = re.compile(r'(?<![\w.])(?:[-+−]?\d[\d.,٬٫]*|\(\d[\d.,٬٫]*\))(?![\w])')
DATES = re.compile(r'(?<!\d)\d{4}[-/]\d{2}[-/]\d{2}(?!\d)')
TYPED_VALUE = re.compile(r'\s*(?:[:=：]|\bis\b|(?:هو|هي|يساوي|بقيمة|بلغت?|عند)\s+)\s*', re.I)
HEADER_DATE = {'date', 'period', 'التاريخ', 'الفتره', 'تاريخ'}
HEADER_UNIT = {'unit', 'currency', 'وحده', 'الوحده', 'العمله', 'عمله'}
HEADER_LABEL = {'metric', 'field', 'item', 'entity', 'node', 'node id', 'label', 'المؤشر', 'البند', 'العنصر'}
HEADER_VALUE = {'value', 'amount', 'balance', 'القيمه', 'قيمه', 'المبلغ', 'الرصيد'}
UNIT_MARKER = {
    'SAR': r'\bsar\b|ريال(?!\s+قطري)|ر\.\s*س',
    'USD': r'\busd\b|\bus dollars?\b|\$|دولار',
    'EUR': r'\beur\b|\beuros?\b|€|يورو',
    'AED': r'\baed\b|درهم',
    'GBP': r'\bgbp\b|£|جنيه استرليني',
    'EGP': r'\begp\b|جنيه مصري',
    'KWD': r'\bkwd\b|دينار كويتي',
    'QAR': r'\bqar\b|ريال قطري',
}


def norm(text):
    return ' '.join(normalize(text).casefold().strip(' :؛،').split())


def matches(text, alias):
    return re.search(r'(?<!\w)' + re.escape(alias) + r'(?!\w)', text) is not None


def numeric_literals(text):
    return [m.group().rstrip(',.٬٫') for m in NUMBER.finditer(DATES.sub(' ', text))]


def decimal_value(literal):
    raw = normalize(literal).replace('−', '-').replace('٬', ',').replace('٫', '.')
    if raw.startswith('(') and raw.endswith(')'):
        raw = '-' + raw[1:-1]
    if not re.fullmatch(r'[+-]?(?:\d+|\d{1,3}(?:,\d{3})+)(?:\.\d+)?', raw):
        raise ValueError('راجع الفاصل العشري وفاصل الآلاف.')
    return Decimal(raw.replace(',', ''))


def observation_date(text):
    found = DATES.search(normalize(text))
    if not found:
        return None
    try:
        return date.fromisoformat(found.group().replace('/', '-')).isoformat()
    except ValueError:
        return None


def normalized_value(literal, context, unit, fx):
    value = decimal_value(literal)
    text = norm(context)
    if unit in (Unit.SAR, Unit.SAR_PER_UNIT):
        currencies = [key for key, pattern in UNIT_MARKER.items() if re.search(pattern, text)]
        if len(currencies) > 1:
            raise ValueError('فيه أكتر من عملة في المصدر لنفس القيمة.')
        currency = currencies[0] if currencies else None
        if not currency and re.search(r'\b[A-Z]{3}\b', context) and any(
            word in context for word in ('currency:', 'العملة:')
        ):
            raise ValueError('عملة المصدر محتاجة مراجعة.')
        if currency and currency != 'SAR':
            if currency not in fx:
                raise ValueError('محتاج سعر تحويل من المصدر للريال السعودي.')
            value *= Decimal(str(fx[currency]))
        if re.search(r'\b(?:millions?|mn)\b|مليون|ملايين', text):
            value *= 1_000_000
        elif re.search(r'\b(?:thousands?|000s|k)\b|الف|الاف', text):
            value *= 1_000
    if unit == Unit.DAYS and re.search(r'\bweeks?\b|اسبوع|اسابيع', text):
        value *= 7
    elif unit == Unit.WEEKS and re.search(r'\bdays?\b|ايام|\bيوم\b', text):
        value /= 7
    if abs(value) > Decimal('1e10'):
        raise ValueError('القيمة بعد التوحيد أكبر من حد المعاينة.')
    return float(value)


@dataclass(frozen=True)
class Candidate:
    source: str
    text: str
    context: str
    evidence: str
    literals: tuple[str, ...]
    date: str | None
    confidence: float


def candidate(row, text, values, context='', dated=None):
    evidence = ' | '.join(row.cells)
    return Candidate(row.source, text[:250], context[:1500], evidence[:1800],
                     tuple(numeric_literals(values)), dated, row.confidence)


def table_headers(cells):
    clean = [norm(cell) for cell in cells]

    def col(words):
        return next((i for i, text in enumerate(clean) if text in words), None)

    dated, label, value, unit = (col(words) for words in (HEADER_DATE, HEADER_LABEL, HEADER_VALUE, HEADER_UNIT))
    if label is not None and value is not None:
        return 'long', dated, label, value, unit
    if dated is not None and sum(bool(cell) for cell in cells) >= 2:
        return 'wide', dated, *cells
    if all(not numeric_literals(cell) for cell in cells) and sum(
        bool(alias_match(cell, catalog(None))) for cell in cells if cell
    ) >= 2:
        return 'wide', None, *cells
    column_dates = [observation_date(cell) for cell in cells]
    if sum(bool(item) for item in column_dates) >= 2 and not any(
            numeric_literals(cell) for cell in cells if cell):
        return 'transpose', *column_dates
    return None


def candidates(rows):
    headers = {}
    for row in rows:
        group = row.source.rsplit(':', 1)[0]
        cells = row.cells
        if header := table_headers(cells):
            headers[group] = header
            continue
        if selected := headers.get(group):
            style, dated, *items = selected

            def cell(index):
                return cells[index] if index is not None and index < len(cells) else ''

            if style == 'long':
                label, value, unit = items
                if cell(label):
                    yield candidate(row, cell(label), cell(value), f'العملة: {cell(unit)}',
                                    observation_date(cell(dated)))
            elif style == 'wide':
                parsed_date = observation_date(cell(dated))
                if parsed_date or dated is None:
                    for index, label in enumerate(items):
                        if index != dated and label:
                            yield candidate(row, label, cell(index), label, parsed_date)
            else:
                dates = [dated, *items]
                label = cells[0]
                for index, date_value in enumerate(dates):
                    if date_value:
                        yield candidate(row, label, cell(index), label, date_value)
            continue
        if len(cells) > 1:
            # PDF layout mode sometimes emits label and its amount as separate cells.
            numeric = [cell for cell in cells[1:] if numeric_literals(cell)]
            if len(numeric) == 1 and cells[0] and not numeric_literals(cells[0]):
                yield candidate(row, cells[0], numeric[0], ' '.join(cells), observation_date(' '.join(cells)))
                continue
        for clause in re.split(r'[؛;؟\n]|،|,(?!\d)', ' '.join(cells)):
            if not clause.strip() or '->' in clause or '→' in clause:
                continue
            parts = TYPED_VALUE.split(clause, maxsplit=1)
            label = parts[0] if len(parts) == 2 else NUMBER.split(clause, maxsplit=1)[0]
            remainder = parts[1] if len(parts) == 2 else clause
            yield candidate(row, label, remainder, clause, observation_date(clause))


def catalog(graph):
    definitions = {key: (kind, unit, name, (key, name, *aliases))
                   for key, (kind, unit, name, aliases) in CATALOG.items()}
    if graph:
        for node in graph.nodes:
            aliases = definitions.get(node.id, (None, None, None, ()))[3]
            definitions[node.id] = (node.type, node.unit, node.name_ar, (node.id, node.name_ar, *aliases))
    return definitions


def alias_match(label, definitions):
    text = norm(label)
    ranks = {}
    for key, (_, _, _, aliases) in definitions.items():
        for alias in map(norm, aliases):
            if text == alias:
                ranks[key] = max(ranks.get(key, (0, 0)), (2, len(alias)))
            elif len(alias) >= 3 and matches(text, alias):
                ranks[key] = max(ranks.get(key, (0, 0)), (1, len(alias)))
    if not ranks:
        return None
    best = max(ranks.values())
    winners = [key for key, rank in ranks.items() if rank == best]
    if len(winners) == 1:
        return winners[0], (0.99 if best[0] == 2 else 0.90)
    return None


def entity_key(kind, label):
    slug = sha256(norm(label).encode()).hexdigest()[:16]
    return f'{kind}.{slug}'


COMPATIBLE_UNITS = {
    NodeType.PROJECT: {Unit.SAR, Unit.DAYS, Unit.WEEKS},
    NodeType.RISK: {Unit.PERCENT, Unit.DAYS, Unit.WEEKS, Unit.SAR},
    NodeType.SUPPLIER: {Unit.DAYS, Unit.WEEKS, Unit.SAR},
}
for kind, unit, _, _ in CATALOG.values():
    COMPATIBLE_UNITS.setdefault(kind, set()).add(unit)
COMPATIBLE_UNITS[NodeType.STORAGE] = {Unit.STOCK_SPACE}
COMPATIBLE_UNITS[NodeType.LINE].add(Unit.STOCK_SPACE)


class Mapping(StrictFields):
    row: int
    node_id: str
    literal: str
    confidence: float = Field(ge=0, le=1)
    new_label: str | None = Field(default=None, min_length=1, max_length=160)
    new_type: NodeType | None = None
    new_unit: Unit | None = None


class MappingBatch(StrictFields):
    mappings: list[Mapping] = Field(max_length=60)


def provider_mappings(settings, entries, definitions):
    if not entries or settings.llm_provider != 'codex':
        return {}
    choices = {}
    # Bound provider latency. Remaining ambiguous rows stay visibly unmapped.
    nodes = [{'id': key, 'label': item[2], 'unit': item[1]} for key, item in list(definitions.items())[:180]]
    for start in range(0, min(len(entries), 120), 40):
        chunk = dict(entries[start:start + 40])
        response = select(settings, MappingBatch,
                          'Map candidate rows to node ids. Select literal exactly from the provided row numbers. '
                          'Use node_id="" only for a new entity with an exact new_label from the candidate label, '
                          'and an evidenced new_type and new_unit. For known nodes set the three new fields to null. '
                          'Do not map dates, identifiers, ambiguous quantities or subtotals. '
                          'Use input labels or surrounding text for mapping, never infer a missing number.',
                          {'nodes': nodes, 'rows': [
                              {'row': index, 'label': item.text, 'text': item.evidence[:750],
                               'numbers': item.literals} for index, item in chunk.items()
                          ]})
        if response is None:
            continue
        seen = set()
        for mapping in response.mappings:
            row = chunk.get(mapping.row)
            if mapping.row in seen:
                choices.pop(mapping.row, None)
            elif row and mapping.literal in row.literals and mapping.confidence >= 0.5:
                if mapping.node_id in definitions:
                    choices[mapping.row] = mapping
                elif (not mapping.node_id and mapping.new_label and mapping.new_label in row.text
                      and mapping.new_unit in COMPATIBLE_UNITS.get(mapping.new_type, ())):
                    label = mapping.new_label.strip()
                    if label and alias_match(label, definitions) is None:
                        key = entity_key(mapping.new_type, label)
                        definitions[key] = (mapping.new_type, mapping.new_unit, label, (label,))
                        choices[mapping.row] = mapping.model_copy(update={'node_id': key})
                    elif label:
                        existing = alias_match(label, definitions)
                        if existing and definitions[existing[0]][:2] == (mapping.new_type, mapping.new_unit):
                            choices[mapping.row] = mapping.model_copy(update={'node_id': existing[0]})
            seen.add(mapping.row)
    return choices


class OntologyHint(StrictFields):
    row: int
    node_id: str
    quote: str = Field(min_length=2, max_length=160)
    confidence: float = Field(ge=0, le=1)


class OntologyHints(StrictFields):
    entities: list[OntologyHint] = Field(max_length=30)


def onboarding(rows, settings, definitions, existing):
    free_text = {}
    for index, row in enumerate(rows):
        text = ' '.join(row.cells)
        if (row.source.startswith('وصف الشركة:') and '->' not in text and '→' not in text
                and not numeric_literals(text)):
            free_text[index] = row
    lines = {index: ' '.join(row.cells)[:1500] for index, row in list(free_text.items())[:35]}
    available = {key: item[2] for key, item in definitions.items() if key not in existing}
    if not lines or not available or settings.llm_provider != 'codex':
        return []
    response = select(
        settings, OntologyHints,
        'Build an ontology draft from the description. Select existing ontology ids for departments, assets, '
        'or indicators clearly present in the text. Each quote must be the exact phrase as evidence. '
        'Only select described entities. Leave out hypothetical or explicitly absent entities. '
        'Do not extract or infer any number.',
        {'possible_entities': available, 'description': [{'row': index, 'text': text} for index, text in lines.items()]},
    )
    if response is None:
        return []
    seen = set(existing)
    selected = []
    for item in response.entities:
        if (item.node_id in available and item.node_id not in seen and item.confidence >= 0.5
                and item.row in lines and item.quote in lines[item.row]):
            selected.append((item.node_id, min(item.confidence, 0.95), free_text[item.row], item.quote))
            seen.add(item.node_id)
    return selected


def preview(rows, graph, name, settings, fx):
    definitions = catalog(graph)
    current = {node.id: node for node in graph.nodes} if graph else {}
    items = list(candidates(rows))
    fixed = {}
    unresolved = []
    gaps = {}
    issues = []
    for index, item in enumerate(items):
        known = alias_match(item.text, definitions)
        if known and len(item.literals) == 1:
            key, confidence = known
            fixed[index] = Mapping(row=index, node_id=key, literal=item.literals[0], confidence=confidence)
        elif item.literals:
            unresolved.append((index, item))
        elif known:
            gaps[known[0], item.date] = item

    predicted = provider_mappings(settings, unresolved, definitions)
    grouped = defaultdict(dict)
    blocked = set()
    duplicates = 0
    for index, mapping in sorted((fixed | predicted).items()):
        item = items[index]
        key, day = mapping.node_id, item.date
        kind, unit, label, _ = definitions[key]
        group_key = key, day
        if group_key in blocked:
            continue
        try:
            value = normalized_value(mapping.literal, item.context, unit, fx)
        except (ValueError, InvalidOperation) as error:
            issues.append({'source': item.source, 'message_ar': str(error)})
            continue
        previous = grouped[key].get(day)
        if previous:
            if previous['value'] == value:
                duplicates += 1
            else:
                issues.append({'source': item.source, 'message_ar': f'قيم متعارضة لنفس البند والتاريخ ({label}).'})
                blocked.add(group_key)
                del grouped[key][day]
            continue
        if group_key in gaps:
            del gaps[group_key]
        grouped[key][day] = {
            'node_id': key, 'name_ar': label, 'value': value, 'unit': unit, 'date': day,
            'evidence': item.evidence, 'source': item.source, 'literal': mapping.literal,
            'confidence': min(mapping.confidence, item.confidence, 0.95 if index in predicted else 1.0),
            'provider': 'codex' if index in predicted else 'rules', 'estimated': False,
        }

    diff = []
    for key, updates in grouped.items():
        if not updates:
            continue
        previous = current.get(key)
        baseline = previous.baseline if previous else None
        if None in updates and (len(updates) > 1 or isinstance(baseline, Series)):
            issues.append({'source': updates[None]['source'], 'message_ar': 'السلسلة الزمنية محتاجة تاريخ للقيمة.'})
            del updates[None]
        if not updates:
            continue
        kind, unit, label, _ = definitions[key]
        if None in updates:
            value = updates[None]['value']
            updates[None]['before'] = baseline.value if isinstance(baseline, Scalar) else None
            replacement = {'kind': 'scalar', 'value': value}
        else:
            old_points = ({point.date.isoformat(): point.value for point in baseline.points}
                          if isinstance(baseline, Series) else {})
            for day, update in updates.items():
                update['before'] = old_points.get(day)
            points = old_points | {day: update['value'] for day, update in updates.items()}
            days = sorted(points)
            weekly = len(days) > 1 and all(
                (date.fromisoformat(right) - date.fromisoformat(left)).days % 7 == 0
                for left, right in zip(days, days[1:])
            )
            replacement = {'kind': 'series',
                           'frequency': baseline.frequency if isinstance(baseline, Series) else 'weekly' if weekly else 'daily',
                           'points': [{'date': day, 'value': points[day]} for day in days]}
        node = (previous.model_dump(mode='json') if previous else
                {'id': key, 'type': kind, 'unit': unit, 'name_ar': label,
                 'definition': {'kind': 'formula', 'expression': 'reviewed source observation'}})
        current[key] = Node.model_validate(node | {'baseline': replacement})
        diff.extend(updates.values())

    estimates = []
    for (key, day), item in gaps.items():
        node = current.get(key)
        point = None
        if day and node and isinstance(node.baseline, Series):
            if any(p.date.isoformat() == day for p in node.baseline.points):
                continue
            preceding = [p for p in node.baseline.points if p.date.isoformat() < day][-8:]
            if len(preceding) >= 3:
                point = float(np.median([p.value for p in preceding]))
        if node and isinstance(node.baseline, Scalar) and node.baseline.value is not None and day is None:
            continue
        estimates.append({'node_id': key, 'name_ar': definitions[key][2], 'date': day, 'value': point,
                          'unit': definitions[key][1], 'confidence': 0.5 if point is not None else 0,
                          'source': item.source, 'evidence': item.evidence, 'estimated': True,
                          'provider': 'median' if point is not None else 'rules'})

    if not graph:
        structural = [
            (key, 0.9, Evidence(item.source, (item.evidence,)), None) for (key, day), item in gaps.items()
            if key not in current and not day and item.source.startswith('وصف الشركة:')
            and not any(entry['source'] == item.source for entry in issues)
            and not re.search(r'\b(?:مفيش|مافيش|بدون|من غير|مش عندنا|no|without)\b', norm(item.text))
        ]
        hinted = onboarding(rows, settings, definitions, {*current, *(item[0] for item in structural)})
        for key, confidence, item, quote in structural + hinted:
            kind, unit, label, _ = definitions[key]
            current[key] = Node(
                id=key, type=kind, name_ar=label, unit=unit, baseline=Scalar(value=None),
                definition={'kind': 'formula', 'expression': 'missing source observation'},
            )
            diff.append({'node_id': key, 'name_ar': label, 'value': None, 'before': None, 'unit': unit,
                         'date': None, 'evidence': ' | '.join(item.cells)[:1800], 'source': item.source,
                         'literal': quote, 'confidence': confidence, 'estimated': False,
                         'provider': 'codex' if quote else 'rules'})
    model = None
    relations = []
    if diff or graph:
        updated = Graph(nodes=list(current.values()), edges=graph.edges if graph else [], arms=graph.arms if graph else [])
        if not updated.arms:
            updated, relations = explicit_relationships(rows, updated, definitions, issues)
        if diff or relations or any(item['value'] is not None for item in estimates):
            model = ModelInput(name=name, graph=updated).model_dump(mode='json')
    return {'model': model, 'diff': diff, 'relations': relations, 'estimates': estimates, 'duplicates': duplicates,
            'issues': issues, 'unmapped': [
                {'source': item.source, 'evidence': item.evidence} for index, item in unresolved if index not in predicted
            ], 'provider': 'codex' if any(item['provider'] == 'codex' for item in diff) else 'rules'}


def explicit_relationships(rows, graph, definitions, issues):
    edges = list(graph.edges)
    keys = {node.id for node in graph.nodes}
    reviewed = []
    for row in rows:
        if not row.source.startswith('وصف الشركة:'):
            continue
        text = ' '.join(row.cells)
        match = re.fullmatch(r'\s*(.+?)\s*(?:->|→)\s*(.+?)\s*=\s*(.+?)\s*', text)
        if not match:
            continue
        source = alias_match(match[1], definitions)
        target = alias_match(match[2], definitions)
        if not source or not target or any(item[0] not in keys for item in (source, target)):
            issues.append({'source': row.source, 'message_ar': 'طرف العلاقة محتاج بند موجود في المسودة.'})
            continue
        try:
            multiplier = float(decimal_value(match[3]))
            if abs(multiplier) > 100:
                raise ValueError
            edge = Edge(source=source[0], target=target[0],
                        name_ar=f'{definitions[source[0]][2]} → {definitions[target[0]][2]}'[:160],
                        rule=EdgeRule(multiplier=multiplier))
            others = [item for item in edges if (item.source, item.target) != (edge.source, edge.target)]
            updated = Graph(nodes=graph.nodes, edges=[*others, edge])
            CausalGraph(updated)
        except (ValueError, ImpactError):
            issues.append({'source': row.source, 'message_ar': 'العلاقة أو المعامل مش صالحين أو بيعملوا دورة.'})
            continue
        edges = updated.edges
        reviewed.append({'edge': edge.model_dump(mode='json'), 'confidence': min(source[1], target[1]),
                         'source': row.source, 'evidence': text})
    return graph.model_copy(update={'edges': edges}), reviewed


def apply_estimates(model, estimates, selected):
    nodes = {node.id: node for node in model.graph.nodes}
    for index in selected:
        entry = estimates[index]
        node = nodes.get(entry['node_id'])
        if not node or not isinstance(node.baseline, Series) or entry['value'] is None:
            raise ValueError('الاقتراح المختار مفيهوش قيمة تقديرية قابلة للحفظ.')
        point = {'date': entry['date'], 'value': entry['value']}
        base = node.baseline.model_dump(mode='json')
        if point['date'] in {item['date'] for item in base['points']}:
            continue
        base['points'] = sorted([*base['points'], point], key=lambda item: item['date'])
        nodes[node.id] = Node.model_validate(node.model_dump(mode='json') | {'baseline': base})
    return ModelInput(name=model.name, graph=model.graph.model_copy(update={'nodes': list(nodes.values())}))
