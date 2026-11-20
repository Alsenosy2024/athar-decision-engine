import re
from decimal import Decimal

from app.llm.rules_ar import WORD_NUMBERS, normalize
from app.simulation.scenario import format_num_ar as num

TOTAL = {'contracting': 'total', 'production': 'total_cost'}
UNITS = {'contracting': 'ألف ريال', 'production': 'ريال'}
NUMBER_TOKEN = re.compile(r'[+-]?(?:\d+(?:\.\d+)?|\.\d+)(?:e[+-]?\d+)?')
NUMBER_WORD_TOKEN = re.compile(
    r'\b(?:و)?(' + '|'.join(WORD_NUMBERS) +
    r'|صفر|مئه|مائه|مئتين|مائتين|الفين|الاف|مليون|ملايين|مليار|مليارات|'
    r'احد عشر|احدي عشره|عشره|ثلاثون|اربعين|اربعون|خمسون|ستين|ستون|سبعين|سبعون|'
    r'ثمانين|ثمانون|تسعين|تسعون)\b'
)


def number_tokens(value):
    if type(value) in (int, float):
        yield Decimal(str(value))
    elif isinstance(value, str):
        text = normalize(value)
        for match in NUMBER_TOKEN.finditer(text):
            yield Decimal(match[0])
        for match in NUMBER_WORD_TOKEN.finditer(text):
            yield match[1]
    elif isinstance(value, dict):
        for item in value.values():
            yield from number_tokens(item)
    elif isinstance(value, (tuple, list)):
        for item in value:
            yield from number_tokens(item)


def number_guard(text, facts):
    return set(number_tokens(text)) <= set(number_tokens(facts))


def credit_note(template, kpis, parameters):
    peak, limit, breach = kpis['peak_od'], parameters['credit_limit'], kpis['breach']
    unit = UNITS[template]
    text = f'أعلى عجز نقدي {num(peak)} مقابل حد ائتماني {num(limit)} {unit}.'
    if breach:
        return f'{text} الخطة الناتجة تتجاوز الحد الائتماني بمقدار {num(breach)} {unit}.'
    if limit:
        text += f' هامش الائتمان المتبقي {num(round(100 * (limit - peak) / limit, 1))}%.'
    return text


def project_finish(dataset, kpis):
    finish = {}
    for activity in dataset['activities']:
        project = activity['project_id']
        week = kpis['finishes'][activity['id']]
        finish[project] = max(week, finish.get(project, 0))
    return finish


def contracting_notes(base, chosen, dataset, *, project=None):
    projects = {row['id']: row for row in dataset['projects']}
    if project is not None:
        row = projects[project]
        late = chosen['late'][project]
        finish = project_finish(dataset, chosen).get(project)
        if finish is None:
            return 'المشروع مفيهوش أنشطة مجدولة في الخطة.'
        cost = late * row['ld_per_week']
        return (f'في الخطة الناتجة، {row["name_ar"]} ينتهي في الأسبوع {finish} '
                f'وموعد التسليم الأسبوع {row["deadline_week"]}. '
                f'التأخير {late} أسبوع وغرامته {num(cost)} ألف ريال '
                f'بمعدل {num(row["ld_per_week"])} ألف ريال لكل أسبوع.')
    notes = []
    if base:
        finish_base = project_finish(dataset, base)
        for key, finish in project_finish(dataset, chosen).items():
            shift = finish_base[key] - finish
            if shift:
                verb = 'تقدّم' if shift > 0 else 'اتأخر'
                notes.append(f'{verb} إنهاء {projects[key]["name_ar"]} بمقدار {abs(shift)} أسبوع مقارنة بخط الأساس.')
    for key, late in chosen['late'].items():
        if late:
            notes.append(f'تأخير {projects[key]["name_ar"]} {late} أسبوع، ومعدل الغرامة '
                         f'{num(projects[key]["ld_per_week"])} ألف ريال لكل أسبوع.')
    names = {row['id']: row['name_ar'] for row in dataset['resources']}
    binding = chosen['binding_weeks']
    if binding and max(binding.values()):
        key = max(binding, key=binding.get)
        notes.append(f'أعلى عدد أسابيع عند السعة الكاملة كان لفئة «{names[key]}» في {binding[key]} أسبوع.')
    return ' '.join(notes)


def optimization_explanation(template, result, dataset, params):
    base, chosen = (result[key]['kpis'] for key in ('baseline', 'optimized'))
    if not chosen:
        return 'المحلّل ما رجّعش خطة قابلة للعرض بالإعدادات الحالية.'
    total = TOTAL[template]
    unit = UNITS[template]
    if base:
        before, after = base[total], chosen[total]
        text = f'الكلفة الإجمالية {num(before)} في خط الأساس و{num(after)} {unit} في الخطة المحسّنة.'
        if before:
            change = round((after - before) / before * 100, 2)
            text += f' التغير {num(change, signed=True)}%.' if change else ' الكلفة متساوية.'
    else:
        text = f'الكلفة الإجمالية للخطة المحسّنة {num(chosen[total])} {unit}. مفيش خط أساس متاح للمقارنة.'
    if template == 'contracting':
        text += (f' الغرامات {num(chosen["ld"])} والتمويل {num(chosen["interest"])} {unit}. '
                 + contracting_notes(base, chosen, dataset))
        if base:
            text += (f' في خط الأساس الغرامات {num(base["ld"])} '
                     f'والتمويل {num(base["interest"])} {unit}.')
    else:
        text += ' ' + production_notes(chosen, dataset, params)
        if base:
            text += (f' خط الأساس فيه {num(base["late_orders"])} طلب متأخر و'
                     f'{num(base["changeovers"])} تبديل منتجات و'
                     f'{num(base["unshipped"])} طلب غير مشحون، وذروة إشغال التبريد {num(base["peak_storage_pct"])}%.')
    return f'{text.strip()} {credit_note(template, chosen, params)}'


def production_notes(kpis, dataset, params):
    slots = {(row['line'], row['day']) for row in kpis['schedule']}
    occupied = [
        sum(line == row['id'] for line, _ in slots) for row in dataset['lines']
    ]
    return (f'الطلبات المتأخرة {num(kpis["late_orders"])}، وتبديلات المنتجات {num(kpis["changeovers"])}، '
            f'والطلبات غير المشحونة {num(kpis["unshipped"])}. '
            f'ذروة إشغال التبريد {num(kpis["peak_storage_pct"])}%. '
            f'إجمالي أيام تشغيل الخطوط {num(sum(occupied))}.') + binding_lines(kpis, dataset, params)


def binding_lines(kpis, dataset, params):
    rates = {(r['line_id'], r['sku_id']): r['units_per_day'] for r in dataset['rates']}
    losses = {r['id']: r['changeover_loss_units'] for r in dataset['lines']}
    changes = {(r['line'], r['day']) for r in kpis['changeover_days']}
    at_capacity = 0
    for row in kpis['schedule']:
        line, day = row['line'], row['day']
        available = rates[line, row['sku']] - (losses[line] if (line, day) in changes else 0)
        at_capacity += row['qty'] == available
    total = sum(params['horizon_days'] - len(set(params['line_down'].get(line, []))) for line in losses)
    return f' الإنتاج بلغ سعة التشغيل في {num(at_capacity)} من {num(total)} يوم خط متاح.'


def scenario_explanation(template, base, chosen, descriptions, dataset, params):
    text = 'التغييرات: ' + '، '.join(descriptions) + '.'
    if not chosen:
        return f'{text} المحلّل ما رجّعش خطة قابلة للعرض للتغييرات دي.'
    total = TOTAL[template]
    before, after = base[total], chosen[total]
    text += (f' الكلفة {num(after)} مقابل {num(before)} {UNITS[template]} '
             f'(فرق {num(round(after - before, 2), signed=True)}).')
    if template == 'contracting':
        changes = {key: abs(value - base['late'][key]) for key, value in chosen['late'].items()}
        if changes and max(changes.values()):
            project = max(changes, key=changes.get)
            name = next(row['name_ar'] for row in dataset['projects'] if row['id'] == project)
            text += f' أكبر تغير في التأخير كان في {name} من {base["late"][project]} إلى {chosen["late"][project]} أسبوع.'
    else:
        text += ' ' + production_notes(chosen, dataset, params)
    return f'{text} {credit_note(template, chosen, params)}'


def query_answer(template, query, kpis, source):
    contracting = template == 'contracting'
    unit = UNITS[template]
    key, label, suffix = {
        'total': (TOTAL[template], 'إجمالي التكلفة', unit),
        'late': ('late' if contracting else 'late_orders', 'عدد المشاريع المتأخرة' if contracting
                 else 'عدد الطلبات المتأخرة', ''),
        'peak_od': ('peak_od', 'أعلى عجز نقدي', unit),
        'ld': ('ld' if contracting else 'lateness_cost', 'غرامات التأخير', unit),
        'changeovers': ('changeovers', 'عدد تبديلات المنتجات', ''),
        'storage': ('peak_storage_pct', 'ذروة إشغال التبريد', '%'),
    }[query]
    if key not in kpis:
        return 'المؤشر ده مش متاح في نتيجة التشغيل الحالية.'
    value = sum(weeks > 0 for weeks in kpis[key].values()) if key == 'late' else kpis[key]
    return f'{label} في {source} هو {num(value)}{(" " + suffix) if suffix and suffix != "%" else suffix}.'


def order_answer(order, kpis, dataset):
    row = next((row for row in kpis['shipments'] if row['order'] == order), None)
    if row is None:
        return f'الطلب {order} غير مشحون في الخطة الناتجة.'
    due = next(item['due_day'] for item in dataset['orders'] if item['id'] == order)
    return (f'الطلب {order} موعده اليوم {due}، والخطة بتشحنه في اليوم {row["day"]}. '
            f'التأخير {row["late_days"]} يوم.')
