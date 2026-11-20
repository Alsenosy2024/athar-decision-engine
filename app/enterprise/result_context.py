"""Data identity and calculation period for enterprise outputs."""
from app.enterprise.schema import Series

DEMO_VERSION = 1
ENDING_CASH_LABEL = 'رصيد السيولة التقديري في نهاية الأفق'


def effective_weeks(graph, requested=None):
    if requested is not None:
        return requested
    return 1 if graph.onboarding else 52


def result_context(connection, snapshot, weeks=None):
    graph = snapshot['graph']
    company_id = snapshot.get('company_id')
    demo = snapshot['id'] is None
    company = connection.execute('SELECT name FROM companies WHERE id=?', (company_id,)).fetchone()
    dates = [point.date for node in graph.nodes if isinstance(node.baseline, Series)
             for point in node.baseline.points]
    if graph.company_data is not None:
        dates = [item.day for item in graph.company_data.observations]
    if graph.onboarding:
        dates.extend(record.date for record in graph.onboarding.records if record.date)
    return {
        'company_id': company_id,
        'company_name': company['name'] if company else snapshot['name'],
        'model_version': DEMO_VERSION if demo else snapshot['version'],
        'source': 'بيانات ديمو روافد التجريبية' if demo else
                  'ملفات الشركة المعتمدة' if graph.company_data is not None else
                  'سجل تجهيز الشركة' if graph.onboarding else
                  'خطة إنتاج معتمدة من بيانات الشركة' if graph._client_sources else
                  'نموذج محفوظ بمحركات تستخدم بيانات تجريبية' if graph.arms else
                  'بيانات النموذج المؤسسي المحفوظ',
        'data_from': min(dates) if dates else None,
        'data_to': max(dates) if dates else None,
        'weekly_snapshot': bool(graph.onboarding and weeks == 1),
        'calculation_weeks': weeks,
        **({'operating_sources': {sector: source.identity for sector, source in graph._client_sources.items()}}
           if graph._client_sources else {}),
    }
