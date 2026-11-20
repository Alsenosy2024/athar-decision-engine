"""Reviewable anomaly, history, and propagation evidence."""
from datetime import timedelta
import re

import numpy as np

from app.enterprise.schema import Series, Unit
from app.intelligence.extraction import norm
from app.intelligence.grounding import answer

UNITS_AR = {
    Unit.SAR: 'ريال', Unit.SAR_PER_UNIT: 'ريال للوحدة', Unit.DAYS: 'يوم',
    Unit.WEEKS: 'أسبوع', Unit.TEAMS: 'فريق', Unit.UNITS: 'وحدة',
    Unit.STOCK_SPACE: 'وحدة تخزين', Unit.PERCENT: '%',
}
SYNONYMS = (('cash', ('الكاش', 'النقد', 'السيوله', 'النقديه')),
            ('pnl.result', ('الربح', 'النتيجه بعد التمويل')),
            ('pnl.revenue', ('الايراد', 'المبيعات', 'الايرادات')),
            ('risk.credit', ('تجاوز', 'مخاطر', 'الائتمان')))


def display(value, unit=None):
    if value is None:
        return 'غير متاح'
    return f'{value:,.2f}'.rstrip('0').rstrip('.') + (' ' + UNITS_AR.get(unit, unit) if unit else '')


def history(graph):
    return {node.id: node for node in graph.nodes if isinstance(node.baseline, Series)}


def anomalies(graph):
    found = []
    for node in history(graph).values():
        points = node.baseline.points
        for index in range(max(8, len(points) - 40), len(points)):
            point = points[index]
            past = [p.value for p in points[max(0, index - 24):index]]
            center = float(np.median(past))
            mad = float(np.median(np.abs(np.asarray(past) - center)))
            # Floor scales with the level, so insignificant deviations from a flat
            # history aren't called anomalies solely because MAD is zero.
            scale = max(mad * 1.4826, abs(center) * 0.01, 1e-9)
            score = abs(point.value - center) / scale
            if score >= 3.5:
                found.append({
                    'node_id': node.id, 'name_ar': node.name_ar, 'date': point.date.isoformat(),
                    'value': point.value, 'reference': center, 'score': round(score, 3),
                    'unit': node.unit, 'confidence': min(0.95, len(past) / 25),
                    'method': 'rolling_median_mad', 'reference_n': len(past),
                })
    return sorted(found, key=lambda item: (-item['score'], item['node_id'], item['date']))[:40]


def fact(node_id, text, evidence, confidence=1.0):
    return {'node_id': node_id, 'text': text, 'evidence': evidence, 'confidence': confidence}


def insight_facts(items):
    return [fact(item['node_id'],
                 f"{item['name_ar']} يوم {item['date']} سجّل {display(item['value'], item['unit'])}، "
                 f"والوسيط السابق {display(item['reference'], item['unit'])}.",
                 {'kind': 'anomaly', **item}, item['confidence']) for item in items]


def trace(edges, target):
    pending = [target]
    visited = {target}
    chosen = []
    while pending:
        next_target = pending.pop()
        for edge in edges:
            if edge['target'] != next_target:
                continue
            chosen.append(edge)
            if edge['source'] not in visited:
                visited.add(edge['source'])
                pending.append(edge['source'])
    return chosen[:50]


def result_facts(result, targets):
    facts = []
    rows = {row['id']: row for row in result['nodes']}
    for target in targets:
        row = rows.get(target)
        if not row or row['after'] is None:
            continue
        unit = row['unit']
        evidence = {'kind': 'simulation', 'path': trace(result['paths'], target),
                    **{key: row[key] for key in ('before', 'after', 'delta', 'quantiles')},
                    'unit': unit}
        text = (f"{row['name_ar']} في المحاكاة اتغيّر من {display(row['before'], unit)} "
                f"إلى {display(row['after'], unit)}.")
        labels = list(dict.fromkeys(edge['name_ar'] for edge in evidence['path']))
        if labels:
            text += ' مسار الأثر المحسوب: ' + ' ← '.join(labels[:6]) + '.'
        facts.append(fact(target, text, evidence))
    return facts


def historical_facts(graph, targets, computed=None):
    nodes = {node.id: node for node in graph.nodes}
    edges = [edge.model_dump(mode='json') for edge in graph.edges]
    facts = []
    for target in targets:
        node = nodes[target]
        if computed and computed.get(target) is not None:
            value = computed[target]
            name = 'أقل رصيد سيولة' if target == 'cash' else node.name_ar
            facts.append(fact(target, f'{name} في خط الأساس المحسوب: {display(value, node.unit)}.',
                              {'kind': 'computed_baseline', 'value': value, 'unit': node.unit}))
            continue
        if not isinstance(node.baseline, Series):
            if node.baseline.value is not None:
                facts.append(fact(target, f'{node.name_ar} في النموذج: {display(node.baseline.value, node.unit)}.',
                                  {'kind': 'baseline', 'value': node.baseline.value, 'unit': node.unit}))
            continue
        points = node.baseline.points
        point = points[-1]
        text = f'{node.name_ar} يوم {point.date.isoformat()} سجّل {display(point.value, node.unit)}.'
        path, contributors = [], []
        if len(points) >= 2:
            previous = points[-2]
            text += f' القيمة السابقة {display(previous.value, node.unit)}.'
            path = trace(edges, target)
            paths_at_lag = [(target, 0)]
            visited = {(target, 0)}
            for downstream, lag in paths_at_lag:
                for edge in path:
                    if edge['target'] != downstream:
                        continue
                    offset = lag + (edge['rule'] or {}).get('lag_weeks', 0)
                    pair = edge['source'], offset
                    if pair not in visited and len(visited) < 50:
                        paths_at_lag.append(pair)
                        visited.add(pair)
            for source, lag in paths_at_lag[1:]:
                upstream = nodes[source]
                if not isinstance(upstream.baseline, Series):
                    continue
                lookup = {item.date: item.value for item in upstream.baseline.points}
                try:
                    start, stop = previous.date - timedelta(weeks=lag), point.date - timedelta(weeks=lag)
                except OverflowError:
                    continue
                if start in lookup and stop in lookup:
                    contributors.append({
                        'id': upstream.id, 'name_ar': upstream.name_ar, 'unit': upstream.unit,
                        'before': lookup[start], 'after': lookup[stop], 'lag_weeks': lag,
                    })
            unique = {item['id']: item for item in contributors}
            contributors = list(unique.values())
            if contributors:
                descriptions = [
                    f"{item['name_ar']} من {display(item['before'], item['unit'])} "
                    f"إلى {display(item['after'], item['unit'])}" for item in contributors[:4]
                ]
                text += ' المدخلات المرتبطة اتحرّكت في الفترات المقابلة: ' + '؛ '.join(descriptions) + '.'
                text += ' العلاقات دي مسارات للفحص، وتأثيرها الفعلي محتاج اختبار فرضية.'
        facts.append(fact(target, text,
                          {'kind': 'history', 'point': point.model_dump(mode='json'), 'unit': node.unit,
                           'path': path, 'coincident_changes': contributors}))
    return facts


def target_ids(graph, question, explicit=None):
    text = norm(question)

    def contains(alias):
        return bool(re.search(r'(?<!\w)(?:ال)?' + re.escape(norm(alias)) + r'(?!\w)', text))

    ids = {node.id for node in graph.nodes}
    found = [node.id for node in graph.nodes if contains(node.id) or contains(node.name_ar)]
    found += [key for key, aliases in SYNONYMS if key in ids and any(map(contains, aliases))]
    return list(dict.fromkeys([*([explicit] if explicit else []), *found]))


def refresh(settings, graph):
    items = anomalies(graph)
    facts = insight_facts(items)
    if facts:
        narrative = answer(settings, 'لخّص أهم إشارات البيانات الجديدة', facts, list(range(min(3, len(facts)))))
    else:
        enough = any(len(node.baseline.points) > 8 for node in history(graph).values())
        narrative = {'provider': 'rules', 'facts': [], 'reply_ar':
                     'مفيش قيم شاذة فوق حد الفحص في السجلات المتاحة.' if enough else
                     'السجل الزمني لسه قصير على فحص القيم الشاذة.'}
    return {'anomalies': items, 'insight': narrative}


def question_answer(settings, graph, question, result=None, target=None, computed=None):
    ids = target_ids(graph, question, target)
    other = [node.id for node in graph.nodes if node.id not in ids]
    pool = (ids + other)[:45]
    facts = result_facts(result, pool) if result else historical_facts(graph, pool, computed)
    fallback = [i for i, item in enumerate(facts) if item['node_id'] in ids][:5]
    if not facts or (not fallback and settings.llm_provider != 'codex'):
        return {'provider': 'rules', 'facts': [],
                'reply_ar': 'حدّد اسم المؤشر اللي بتسأل عنه، أو شغّل فرضية عشان تراجع مسار الأثر.'}
    response = answer(settings, question, facts, fallback)
    if not response['facts']:
        response['reply_ar'] = 'مفيش قيمة محسوبة متاحة للمؤشر المطلوب في الإصدار ده.'
    return response
