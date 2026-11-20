"""Answers assembled from the selected snapshot, dated records and calculation results."""
import json
from datetime import timedelta

from app.enterprise.impact_graph import reference
from app.enterprise.schema import Series
from app.intelligence.reviews import observed_history
from app.llm import assistant_language as language
from app.llm.enterprise import _display
from app.llm.explain import query_answer
from app.llm.service import latest_plan, view_inputs
from app.onboarding.presets import OUTPUTS
from app.onboarding.projection import latest_records

PROFIT_INPUTS = ('pnl.revenue', 'pnl.direct_cost', 'pnl.expenses', 'financing')
COSTS = PROFIT_INPUTS[1:]
CALCULATIONS = {
    'cash': 'النقد الافتتاحي + ذمم العملاء − ذمم الموردين + نتيجة الأسبوع',
    'pnl.result': 'الإيراد − التكاليف المباشرة − مصاريف التشغيل − التمويل',
    'pnl.revenue': 'الوحدات المباعة × سعر الوحدة',
    'pnl.direct_cost': 'الوحدات المباعة × تكلفة الوحدة',
    'sales.volume': 'الطلب في حدود الطاقة المسجلة',
}
SERIES_PERIOD = {'weekly': 'الأسبوع الذي يبدأ', 'daily': 'يوم', 'monthly': 'الشهر الذي يبدأ'}


def period_and_source(snapshot, *, cash_dates=None):
    graph = snapshot['graph']
    if graph.onboarding:
        records = latest_records(graph.onboarding.records)
        dates = sorted({record.date for record in records.values() if record.date})
        period = 'أسبوع تقديري واحد'
        if dates:
            span = str(dates[0]) if len(dates) == 1 else f'{dates[0]} إلى {dates[-1]}'
            period += f' · تواريخ أحدث المدخلات {span}'
        return period, 'حساب نموذج الشركة من سجلات التجهيز'
    if cash_dates:
        return f'{cash_dates[0]} إلى {cash_dates[-1]}', 'دفتر السيولة وقائمة الدخل المحسوبة'
    if snapshot.get('created_at'):
        return f"لقطة النموذج بتاريخ {snapshot['created_at'][:10]}", 'حساب النموذج وجدول مؤشراته'
    return 'لقطة النموذج المرجعية', 'حساب النموذج وجدول مؤشراته'


def citation(period, source):
    return f'الفترة {period}. المصدر {source}.'


def point_period(series, point):
    return f'{SERIES_PERIOD.get(series.frequency, "بتاريخ")} {point.date}'


def profit_breakdown(observed, previous, current, delta):
    contributions = []
    for key, factor in zip(PROFIT_INPUTS, (1, -1, -1, -1)):
        node = observed.get(key)
        if node is None or not isinstance(node.baseline, Series):
            return ''
        values = {point.date: point.value for point in node.baseline.points}
        if previous.date not in values or current.date not in values:
            return ''
        contribution = factor * (values[current.date] - values[previous.date])
        contributions.append((node.name_ar, contribution))
    if abs(sum(change for _, change in contributions) - delta) > .01:
        return ''
    details = '، '.join(f'{name} {_display(change, "SAR")}' for name, change in contributions if change)
    return f' مساهمة بنود قائمة الدخل في الفرق: {details}.' if details else ''


def read_metrics(connection, snapshot, model, text, keys, explain):
    nodes = model.causal.nodes
    observed = (observed_history(connection, snapshot['id'], model.graph, empty_as_missing=True)[0]
                if snapshot['id'] is not None else model.graph)
    historical = {node.id: node for node in observed.nodes}
    evaluation = None
    replies = []
    for key in keys:
        if key in historical and isinstance(historical[key].baseline, Series) and not (
            model.template and key.startswith('demand.')
        ):
            node = historical[key]
            series = node.baseline
            current = series.points[-1]
            reply = f'{node.name_ar} بلغ {_display(current.value, node.unit)}.'
            period = point_period(series, current)
            if explain and len(series.points) > 1:
                previous = series.points[-2]
                delta = current.value - previous.value
                reply += (f' القراءة السابقة {_display(previous.value, node.unit)}'
                          f' والفرق {_display(delta, node.unit)}.')
                period = f'{point_period(series, previous)} إلى {point_period(series, current)}'
                breakdown = profit_breakdown(historical, previous, current, delta) if key == 'pnl.result' else ''
                reply += breakdown or ' السجل يوضح اتجاه التغيّر، وتحديد السبب محتاج بنود مرتبطة لنفس الفترتين.'
            elif explain:
                reply += ' فيه قراءة مؤرخة واحدة، وتفسير التغيّر محتاج قراءة سابقة لنفس المؤشر.'
            replies.append(f'{reply} {citation(period, "سجل المؤشر في النموذج")}')
            continue

        if key == 'cash' and 'cash' in nodes and model.template and not explain and language.alias(
            text, ('دلوقتي', 'الحين', 'الان', 'هلا', 'حاليا', 'حاليًا', 'الافتتاحي')
        ):
            opening = reference(nodes['cash'])
            if opening is not None:
                period, _ = period_and_source(snapshot)
                source = 'جدول مدخلات السيولة في النموذج (حقل الرصيد الافتتاحي)'
                replies.append(f'الرصيد الافتتاحي المسجّل {_display(opening, "SAR")}. {citation(period, source)}')
                continue

        components = tuple(item for item in COSTS if item in nodes)
        candidates = components if key == 'cost.total' and components else (key,)
        if not all(item in nodes for item in candidates):
            continue
        # A series built entirely from estimated observations should not be presented as a recorded reading.
        if key in nodes and isinstance(nodes[key].baseline, Series) and not model.template:
            continue
        if evaluation is None:
            evaluation = model.baseline()[0]
        values = evaluation.metrics()
        if any(values.get(item) is None for item in candidates):
            continue
        dates = None
        if evaluation.cash_start is not None and values.get('cash') is not None:
            start = evaluation.cash_start - timedelta(days=1)
            dates = (start.isoformat(), (start + timedelta(days=len(evaluation.values['cash']) - 1)).isoformat())
        period, source = period_and_source(snapshot, cash_dates=dates)
        if key != 'cost.total':
            node = nodes[key]
            name, unit = node.name_ar, node.unit
            if key == 'cash' and dates:
                name = 'أقل رصيد سيولة في دفتر النموذج'
            formula = CALCULATIONS.get(key) if model.graph.onboarding and key in OUTPUTS else getattr(
                node.definition, 'expression', None)
        else:
            name, unit = 'إجمالي التكاليف المتاحة', nodes[candidates[0]].unit
            formula = ' + '.join(nodes[item].name_ar for item in candidates)
        if formula:
            source += f' ({formula})'
        if model.graph.onboarding and key in OUTPUTS:
            deps = OUTPUTS[key][3]
            inputs = [f'{nodes[item].name_ar} {_display(values[item], nodes[item].unit)}'
                      for item in deps if item in nodes and values.get(item) is not None]
        else:
            inputs = []
        amount = sum(values[item] for item in candidates)
        reply = f'{name} بلغ {_display(amount, unit)}.'
        if inputs:
            reply += ' بنود الحساب: ' + '، '.join(inputs) + '.'
        if explain:
            reply += ' المتاح لقطة مرجعية واحدة. محتاج قراءتين مؤرختين عشان أحدد التغيّر وسببه.'
        replies.append(f'{reply} {citation(period, source)}')

    if replies:
        return ' '.join(replies)
    period, source = period_and_source(snapshot)
    if keys:
        return f'المؤشر المطلوب محتاج بيانات إضافية في الإصدار ده. {citation(period, source)}'
    return None


def sector_answer(connection, company, keys, intent):
    plan = latest_plan(connection, company['id'])
    template = company['template']
    if template not in ('contracting', 'production'):
        return None
    if plan is None or plan['dataset_id'] != company['active_dataset_id']:
        return None
    result = json.loads(plan['result_json'])
    variant = 'scenario' if plan['kind'] == 'whatif' else 'optimized'
    kpis = result[variant]['kpis']
    if not kpis or intent == 'what_if':
        return None
    _, params = view_inputs(template, plan)
    period = (f"{params['horizon_weeks']} أسبوع في خطة المقاولات" if template == 'contracting'
              else f"{params['horizon_days']} يوم في خطة الإنتاج")
    source = f"جدول نتائج التخطيط · تشغيل {plan['id']} · مجموعة بيانات {plan['dataset_id']}"
    answers = []
    for key in keys:
        if key == 'cost.total':
            answers.append(query_answer(template, 'total', kpis, f"التشغيل رقم {plan['id']}"))
        elif key == 'cash' and kpis.get('cash_curve'):
            curve = kpis['cash_curve']
            scale = 1000 if template == 'contracting' else 1
            unit = 'أسبوع' if template == 'contracting' else 'يوم'
            answers.append(f'رصيد السيولة بنهاية {len(curve)} {unit} من التدفقات المخططة '
                           f'{_display(curve[-1] * scale, "SAR")}.')
        elif key == 'pnl.revenue' and 'revenue' in kpis:
            answers.append(f'إيراد الشحنات المخطط {_display(kpis["revenue"], "SAR")}.')
        elif key == 'pnl.result' and 'margin' in kpis and 'total_cost' in kpis:
            result = kpis['margin'] - kpis['total_cost']
            answers.append(f'هامش الشحنات بعد تكلفة التخطيط {_display(result, "SAR")}'
                           f' (هامش الشحنات {_display(kpis["margin"], "SAR")} − تكلفة التخطيط '
                           f'{_display(kpis["total_cost"], "SAR")}).')
        elif key == 'pnl.direct_cost' and 'revenue' in kpis and 'margin' in kpis:
            cost = kpis['revenue'] - kpis['margin']
            answers.append(f'تكلفة الشحنات المباشرة المخططة {_display(cost, "SAR")}.')
    if not answers:
        return None
    if intent == 'explain':
        answers.append('تفسير التغيّر محتاج نتائج لنفس المؤشر في فترتين.')
    return f'{" ".join(answers)} {citation(period, source)}'


def requested_keys(text, model, focus_node=None):
    by_label = [node.id for node in model.graph.nodes if language.alias(text, (node.name_ar,))]
    keys = language.metrics(text)
    for key in by_label:
        if key in COSTS and 'cost.total' in keys:
            continue
        if key not in keys:
            keys.append(key)
    return keys or ([focus_node] if focus_node and language.intent(text) == 'explain' else [])
