"""Current-snapshot signals and evidence for the living map."""
from math import isfinite

from app.enterprise.living_map import DEPARTMENTS, GROUP
from app.enterprise.scenarios import KPI_NODES, KPIS, kpis
from app.intelligence import analysis, grounding
from app.onboarding import projection
from app.prescriptive import monitor

ACTIONS = {
    'cash_low': 'راجع مواعيد التحصيل والمدفوعات قبل اعتماد التزامات جديدة.',
    'credit_breach': 'راجع حد الائتمان والتدفقات النقدية المتوقعة.',
    'delays': 'راجع الأنشطة المتأخرة وسعة الفرق المرتبطة بيها.',
}


def monitor_snapshot(model, thresholds):
    if any(getattr(thresholds, field) is not None for field, _ in monitor.METRICS.values()):
        return monitor.monitor(model, thresholds)
    values = {}
    # Operational plans without alert thresholds use the map's on-demand baseline.
    if not model.graph.arms:
        baseline, _ = model.baseline()
        values = kpis(baseline.metrics(), baseline.fill_rate)
    return {'kpis': values, 'alerts': [], 'unavailable': [], 'warning': None}


def signal_trace(graph, nodes, node_id):
    facts = analysis.historical_facts(graph, [node_id])
    evidence = facts[0]['evidence'] if facts else {}
    contributors = [item for item in evidence.get('coincident_changes', []) if item['before'] != item['after']]
    edges = [edge.model_dump(mode='json') for edge in graph.edges]
    path = analysis.trace(edges, node_id)
    return {
        'path': [{**edge, 'from_department': GROUP[nodes[edge['source']].type],
                  'to_department': GROUP[nodes[edge['target']].type]} for edge in path],
        'coincident_changes': contributors,
    }


def briefing(settings, graph, observed, monitored, *, historical=False):
    nodes = {node.id: node for node in graph.nodes}
    readiness = projection.readiness(graph) if graph.onboarding else None
    history = {key: node.baseline.points for key, node in analysis.history(observed).items()}
    try:
        anomalies = [row for row in analysis.anomalies(observed)
                     if row['date'] == history[row['node_id']][-1].date.isoformat() and isfinite(row['score'])]
        anomaly_warning = None
    except (FloatingPointError, OverflowError):
        anomalies = []
        anomaly_warning = 'تعذّر فحص الشذوذ بسبب نطاق الأرقام في السجلات.'
    signals = []

    def add(key, node_id, label, kind, text, action, evidence, confidence=None):
        department = GROUP[nodes[node_id].type]
        signals.append({
            'id': key, 'kind': kind, 'node_id': node_id, 'department': department, 'label': label,
            'text': f'{DEPARTMENTS[department][0]} · {text}', 'action': action, 'confidence': confidence,
            'evidence': evidence,
        })

    for alert in monitored['alerts']:
        key = alert['kpi']
        node_id = KPI_NODES[key]
        if node_id not in nodes:
            continue
        label, unit = KPIS[key]['label'], KPIS[key]['unit']
        value, threshold = (analysis.display(alert[field], unit) for field in ('value', 'threshold'))
        direction = 'أقل من' if alert['direction'] == 'below' else 'أعلى من'
        add(key, node_id, label, 'threshold',
            f'{label} بلغ {value}، {direction} الحد المحدد ({threshold}).', ACTIONS[key], alert)

    for anomaly, fact in zip(anomalies, analysis.insight_facts(anomalies)):
        node_id = anomaly['node_id']
        add(f'anomaly:{node_id}', node_id, anomaly['name_ar'], 'anomaly', fact['text'],
            'راجع سجل المؤشر ومصدر القيمة قبل تعديل الخطة.', anomaly, fact['confidence'])

    candidates = [analysis.fact(row['node_id'], f"{row['text']} {row['action']}",
                                {'kind': row['kind']}, row['confidence']) for row in signals[:12]]
    if candidates:
        narrative = grounding.answer(settings, 'اختار أهم إشارات الموجز للإدارة، لحد إشارتين.',
                                     candidates, list(range(min(2, len(candidates)))))
    else:
        enough = any(len(points) > 8 for points in history.values())
        status = anomaly_warning or ('فحص آخر القراءات ما رصدش إشارات شذوذ.' if enough else
                                     'السجلات الزمنية المتاحة لسه قصيرة على فحص الشذوذ.')
        if readiness and readiness['score'] < 100:
            section = next(row for row in readiness['sections'] if row['missing'])
            status += f" كمّل بيانات «{section['name']}» من تجهيز الشركة."
        narrative = {'provider': 'rules', 'reply_ar': status}

    traces = {node_id: signal_trace(observed, nodes, node_id)
              for node_id in dict.fromkeys(row['node_id'] for row in signals)}
    return {
        'provider': narrative['provider'], 'reply_ar': narrative['reply_ar'],
        'readiness': readiness['score'] if readiness else None,
        'kpis': monitored['kpis'], 'alerts': signals, 'traces': traces, 'historical': historical,
        'history': {key: [point.value for point in history.get(node_id, [])[-16:]]
                    for key, node_id in KPI_NODES.items()},
        'warning': ' '.join(filter(None, (monitored['warning'], anomaly_warning if signals else None))),
        'unavailable': monitored['unavailable'],
    }
