import re
import unicodedata
from decimal import Decimal
from math import ceil

from app.llm.schema import ChatContext, Scenario
from app.templates_registry import load_templates

CHARACTERS = str.maketrans(
    '٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹أإآٱةى٪٫٬−',
    '01234567890123456789ااااهي%.,-',
)
WORD_NUMBERS = {
    'واحد': '1', 'اتنين': '2', 'اثنين': '2', 'اثنان': '2', 'ثلاثه': '3',
    'اربعه': '4', 'خمسه': '5', 'سته': '6', 'سبعه': '7', 'ثمانيه': '8',
    'تسعه': '9', 'عشره': '10', 'عشرين': '20', 'ثلاثين': '30',
    'خمسين': '50', 'يومين': '2 يوم', 'اسبوعين': '2 اسبوع', 'مشروعين': '2 مشروع',
}
PERCENT_MARKERS = r'بالم(?:يه|ئه|ائه)|في\s+الم(?:يه|ئه|ائه)'
NUMBER = r'(?<!\w)[+-]?\d+(?:\.\d+)?(?![\w.]|,\d)'
DECREASE = r'\b(?:قل|قلت|قلل|نقص|نقصت|انخفض|انخفضت|خفض|نزل|نزلت|تقليل|انخفاض|اقل)\b'
INCREASE = r'\b(?:زاد|زادت|ارتفع|ارتفعت|زياده|رفع|اعلي|اكثر)\b'
EARLIER = r'\b(?:قدم|قدمت|اتقدم|اتقدمت|بدري|ابكر)\b'
LATER = r'\b(?:اخر|اخرنا|اتاخر|اتاخرت|تاخر|تاخرت|اجل|اتاجل|اتاجلت|بعد|تاجيل)\b'
CREDIT = r'(?:الحد الائتماني|حد الائتمان|حد ائتمان|الائتمان)'
PAYMENT = r'(?:بيدفع|يدفع|الدفع|التحصيل|تحصيل|السداد)'
STARTERS = (
    r'لو|اذا|الحد|حد|الائتمان|الطلب|طلب|المخزن|سعه|تكلفه|غرامه|غرامات|الهامش|'
    r'خط|الخط|موعد|شحنه|فريق|طاقم|وزن|اجل|ابدا|قدم|نقص|خفض|زاد|قلل|'
    r'التحصيل|الدفع|عطل|ركز'
)
CLAUSE_BREAK = re.compile(r'\s*(?:،|[;,؛]|\bثم\b)\s*|\s+و\s*(?=(?:' + STARTERS + r')\b)')
HELP_EXAMPLES = {
    'contracting': (
        'لو الحد الائتماني قل 10% إيه اللي هيحصل؟', 'أجّل مشروع B أسبوعين',
        'ليه أخّرت B؟', 'كام إجمالي التكلفة؟', 'قارن الخطة بالأساس',
    ),
    'production': (
        'لو الطلب زاد 20%؟', 'لو الخط 1 وقف يومين؟', 'كام تغيير خط؟',
        'اشرح النتيجة', 'شغّل التحسين',
    ),
}


def normalize(message):
    normalized = ''.join(
        char for char in unicodedata.normalize('NFKC', message)
        if not unicodedata.combining(char) and char != 'ـ'
    ).translate(CHARACTERS).lower()
    normalized = re.sub(r'(?<=\d),(?=\d{3}(?!\d))', '', normalized)
    return ' '.join(normalized.split())


def number_words(message):
    message = re.sub(PERCENT_MARKERS, '%', message)
    message = re.sub(r'\b(?:نص|نصف)\s*(?=%)', '50', message)
    return re.sub(r'\b(?:' + '|'.join(WORD_NUMBERS) + r')\b',
                  lambda match: WORD_NUMBERS[match[0]], message)


def help_text(template):
    return 'جرّب واحد من الأسئلة دي:\n' + '\n'.join(f'• {item}' for item in HELP_EXAMPLES[template])


def entity(text, field, entities):
    names = sorted(entities[field].items(), key=lambda item: len(item[1]), reverse=True)
    for code, label in names:
        label = normalize(label)
        if label and re.search(rf'(?<!\w){re.escape(label)}(?!\w)', text):
            return code
    patterns = {
        'project': r'\b([a-z][a-z0-9_-]*)\b',
        'resource': r'\b(r\d+)\b',
        'line': r'\b(l\d+)\b',
        'order': r'\b(o\d+)\b',
        'sku': r'\b(s\d+)\b',
    }
    code = re.search(patterns[field], text)
    if code:
        return next((key for key, _ in names if key.lower() == code[1]), code[1].upper())
    if field == 'line':
        slot = re.search(r'\b(?:الخط|خط)\s+(\d+|الاول|الثاني|الثالث)\b', text)
        if slot:
            return 'L' + {'الاول': '1', 'الثاني': '2', 'الثالث': '3'}.get(slot[1], slot[1])
    if field == 'order':
        slot = re.search(r'\b(?:الطلب|طلبيه|شحنه|الشحنه)\s+(\d+)\b', text)
        if slot:
            return f'O{slot[1]}'
    if field == 'resource':
        for code, label in names:
            label = re.sub(r'^(?:فرق|فريق|طاقم)\s+', '', normalize(label))
            stem = label.removeprefix('ال').removesuffix('ات')
            if label and re.search(rf'\b(?:ال)?{re.escape(stem)}(?:ات)?\b', text):
                return code
    return None


def numeric(text, *, percent=False):
    match = re.search(rf'({NUMBER})\s*%' if percent else NUMBER, text)
    return Decimal(match[1] if percent else match[0]) if match else None


def signed(value, text, *, decrease=DECREASE, increase=INCREASE):
    if value < 0 or re.search(decrease, text):
        return -abs(value)
    if re.search(increase, text):
        return abs(value)
    return value


def period(text, unit):
    match = re.search(rf'({NUMBER})\s*(?:{unit})\b', text)
    if match and Decimal(match[1]) == int(Decimal(match[1])):
        return int(Decimal(match[1]))
    return 1 if re.search(rf'\b(?:{unit})\b', text) and not match else None


def weight(text, template_id):
    aliases = {
        'ld': 'الغرامات|غرامات التاخير', 'lateness': 'الغرامات|التاخير',
        'financing': 'التمويل', 'makespan': 'مده المشروع|مده التنفيذ',
        'holding': 'التخزين', 'changeover': 'التغيير', 'unshipped': 'غير المشحون',
    }
    for spec in load_templates()[template_id].objective_weights:
        if not re.search(rf'{aliases.get(spec.id, spec.id)}|{re.escape(normalize(spec.name_ar))}', text):
            continue
        value = numeric(text)
        if value is not None:
            return {'type': 'weight_change', 'name': spec.id, 'value': float(value)}
        factor = 0.5 if re.search(DECREASE + r'|\bاهميه اقل\b', text) else 2
        return {'type': 'weight_change', 'name': spec.id, 'value': factor, 'relative': True}
    return None


def parse_change(clause, ctx, entities):
    text = number_words(clause)
    if re.search(r'\b(?:وزن|ركز|اهميه)\b', text):
        return weight(text, ctx.template)

    pct = numeric(text, percent=True)
    if re.search(CREDIT, text):
        if pct is not None:
            return {'type': 'credit_limit_pct', 'value': float(signed(pct, text))}
        if re.search(DECREASE + '|' + INCREASE, text) and not re.search(r'\b(?:الي|يبقي|بقي)\b', text):
            return None
        amount = numeric(text)
        if amount is not None and amount == int(amount):
            multiplier = (
                1000 if ctx.template == 'production' and re.search(r'\bالف\b', text) else 1
            )
            return {'type': 'credit_limit_abs', 'value': int(amount) * multiplier}

    if re.search(PAYMENT, text):
        weeks = period(text, r'اسبوع|اسابيع')
        days = period(text, r'يوم|ايام')
        if weeks is not None and days is None:
            value = weeks if ctx.template == 'contracting' else weeks * 7
        elif days is not None and weeks is None:
            value = ceil(days / 7) if ctx.template == 'contracting' else days
        else:
            value = None
        if value is not None:
            relative = bool(re.search(LATER + '|' + EARLIER, text)) and not re.search(
                r'\bبعد\b|\bبدل\b', text
            )
            return {'type': 'payment_lag', 'value': signed(value, text, decrease=EARLIER, increase=LATER)
                    if relative else value, 'relative': relative}

    if ctx.template == 'contracting':
        return contracting_change(clause, text, pct, entities)
    return production_change(clause, text, pct, entities)


def contracting_change(clause, text, pct, entities):
    if re.search(r'\bالهامش\b', text) and pct is not None:
        return {'type': 'margin_pct', 'value': float(pct)}
    if re.search(r'\b(?:فريق|طاقم|فرق)\b', text):
        resource = entity(clause, 'resource', entities)
        if resource:
            span = re.search(r'\bمن\s+(?:الاسبوع\s+)?(\d+)\s*(?:ل(?:ـ|حد)?|الي|حتي|و)\s*(\d+)\b', text)
            if span:
                first, last = map(int, span.groups())
            else:
                first = 0
                last = (period(text, r'اسبوع|اسابيع') or 1) - 1
            return {'type': 'crew_unavailable', 'resource': resource, 'from': first, 'to': last}

    project = entity(clause, 'project', entities)
    if not project:
        return None
    if re.search(r'\b(?:غرامه|الغرامه)\b', text):
        value = numeric(text)
        if value is not None and value == int(value):
            return {'type': 'ld_change', 'project': project, 'value': int(value)}
    weeks = period(text, r'اسبوع|اسابيع')
    if weeks is not None and re.search(EARLIER + '|' + LATER, text):
        if re.search(r'\b(?:موعد|تسليم|التسليم)\b', text) or re.search(EARLIER, text):
            return {'type': 'deadline_shift', 'project': project,
                    'weeks': signed(weeks, text, decrease=EARLIER, increase=LATER)}
        if re.search(r'\b(?:اجل|ابدا|بدايه|بدء|تاجيل)\b', text):
            return {'type': 'project_start_delay', 'project': project, 'weeks': weeks}
    return None


def production_change(clause, text, pct, entities):
    if pct is not None:
        value = float(signed(pct, text))
        if re.search(r'تكلفه (?:التغيير|التبديل)', text):
            return {'type': 'changeover_cost_pct', 'value': value}
        if re.search(r'المخزن المبرد|سعه (?:التبريد|التخزين)|التخزين المبرد', text):
            return {'type': 'storage_pct', 'value': value}
        if re.search(r'\b(?:الطلب|طلب)\b', text):
            sku = entity(clause, 'sku', entities)
            if sku is None and re.search(r'\bعلي\b', text):
                return None
            return {'type': 'demand_pct', 'value': value, 'sku': sku}

    line = entity(clause, 'line', entities)
    if line and re.search(r'\b(?:وقف|توقف|اتوقف|عطلان|عطل|متعطل)\b', text):
        start = re.search(r'\bمن\s+(?:يوم\s+)?(\d+)\b', text)
        duration_text = text[start.end():] if start else text
        days = period(duration_text, r'يوم|ايام')
        hours = period(duration_text, r'ساعه|ساعات')
        if days or hours:
            return {'type': 'line_down', 'line': line, 'from_day': int(start[1]) if start else 0,
                    'days': days or ceil(hours / 24)}

    order = entity(clause, 'order', entities)
    if order and re.search(EARLIER + '|' + LATER, text):
        days = period(text, r'يوم|ايام')
        if days is not None:
            return {'type': 'order_due_shift', 'order': order,
                    'days': signed(days, text, decrease=EARLIER, increase=LATER)}
    return None


def parse(message: str, ctx: ChatContext) -> Scenario:
    text = normalize(message)
    entities = ctx.entities()
    fields = {'template': ctx.template, 'confidence': 0.95}
    if re.search(r'\b(?:ليه|لماذا|اشرح|فسر)\b', text):
        subject_field = 'project' if ctx.template == 'contracting' else 'order'
        return Scenario(**fields, intent='explain', subject=entity(text, subject_field, entities))
    if re.search(r'\b(?:قارن|الفرق|مقارنه)\b', text):
        return Scenario(**fields, intent='compare')
    if re.search(r'شغل التحسين|اعمل خطه جديده|اعد التحسين', text):
        return Scenario(**fields, intent='optimize')

    clauses = [clause for clause in CLAUSE_BREAK.split(text) if clause.strip()]
    changes = [parse_change(clause, ctx, entities) for clause in clauses]
    if changes and all(change is not None for change in changes):
        return Scenario(**fields, intent='what_if', changes=changes)

    if re.search(r'\b(?:كام|ايه|كم|قد ايه|ما هي|ما هو)\b', text):
        for key, pattern in (
            ('total', 'اجمالي التكلفه|الكلفه الاجماليه|التكلفه الاجماليه'),
            ('peak_od', 'عجز نقدي|السحب علي المكشوف'),
            ('late', 'متاخر|التاخير'),
            ('ld', 'الغرامات|غرامات'),
            ('changeovers', 'تغيير خط|تغييرات الخط'),
            ('storage', 'التخزين|التبريد'),
        ):
            if re.search(pattern, text):
                return Scenario(**fields, intent='query', query_kpi=key)
    return Scenario(template=ctx.template, intent='help', echo_ar=help_text(ctx.template))
