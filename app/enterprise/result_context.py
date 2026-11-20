"""Data identity and calculation period for enterprise outputs."""
from app.enterprise.schema import Series

DEMO_VERSION = 1
WEEKLY_CASH_LABEL = 'رصيد السيولة التقديري بنهاية الأسبوع'


def calculation_weeks(graph, requested):
    return 1 if graph.onboarding else requested


def result_context(connection, snapshot, weeks=None):
    graph = snapshot['graph']
    company_id = snapshot.get('company_id')
    demo = snapshot['id'] is None
    company = connection.execute('SELECT name FROM companies WHERE id=?', (company_id,)).fetchone()
    dates = [point.date for node in graph.nodes if isinstance(node.baseline, Series)
             for point in node.baseline.points]
    if graph.onboarding:
        dates.extend(record.date for record in graph.onboarding.records if record.date)
    return {
        'company_id': company_id,
        'company_name': company['name'] if company else snapshot['name'],
        'model_version': DEMO_VERSION if demo else snapshot['version'],
        'source': 'بيانات ديمو روافد التجريبية' if demo else
                  'سجل تجهيز الشركة' if graph.onboarding else
                  'نموذج محفوظ بمحركات تستخدم بيانات تجريبية' if graph.arms else
                  'بيانات النموذج المؤسسي المحفوظ',
        'data_from': min(dates) if dates else None,
        'data_to': max(dates) if dates else None,
        'weekly_snapshot': bool(graph.onboarding),
        'calculation_weeks': calculation_weeks(graph, weeks) if weeks is not None else None,
    }
