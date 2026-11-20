"""Scenario contract and deterministic Arabic fallback for the enterprise assistant."""
import re
from typing import Literal

from pydantic import Field, ValidationError, model_validator

from app.enterprise.impact_graph import Assumption, ImpactError
from app.llm.rules_ar import CREDIT, DECREASE, INCREASE, normalize, number_words
from app.llm.schema import StrictBody

EXAMPLE = 'لو خفّضنا حد الائتمان 15% وأخّرنا المورد أسبوعين، إيه الأثر؟'
HELP = f'حدّد المدخل والتغيير المطلوب في كل فرضية. مثال: «{EXAMPLE}»'
UNIT_LABELS = {
    'SAR': 'ريال', 'SAR/unit': 'ريال لكل وحدة', 'units': 'وحدة', 'storage_units': 'وحدة تخزين',
    'teams': 'فرق', 'days': 'يوم', 'weeks': 'أسبوع', '%': '%',
}
COMPARE_FIRST = ('pnl.result', 'cash', 'risk.credit', 'risk.delay')

CLAUSES = re.compile(
    r'\s*(?:[،,;؛]|\bثم\b)\s*|\s+و\s*(?=(?:اخر\w*|تاخير|تاخر\w*|خفض\w*|تقليل|'
    r'زياده|رفع\w*|زاد\w*|قل\w*|حد|المورد|مهله|سعه|الطلب)\b)'
)
DELAY = r'\b(?:اخر\w*|تاخر\w*|تاخير|اجل\w*|زاد\w*|زياده)\b'
SUPPLIER = r'\b(?:المورد|مورد الخامات|مهله التوريد)\b'
AMOUNT = re.compile(r'(?<!\w)[+-]?\d+(?:\.\d+)?(?![\w.])')


class EnterpriseScenario(StrictBody):
    template: Literal['enterprise'] = 'enterprise'
    intent: Literal['what_if', 'help']
    assumptions: list[Assumption] = Field(default_factory=list, max_length=50)

    @model_validator(mode='after')
    def validate_intent(self):
        if (self.intent == 'what_if') != bool(self.assumptions):
            raise ValueError('Only what_if may have assumptions and it requires at least one')
        if len({item.node_id for item in self.assumptions}) != len(self.assumptions):
            raise ValueError('Apply one assumption per driver')
        return self


def validate_enterprise_changes(model, scenario):
    if scenario.assumptions:
        model.assumption_values(scenario.assumptions)


def _rule_change(clause, drivers):
    if re.search(r'\b(?:بدل|مليون|ملايين|الف|الاف|نص|نصف|بدون|متخفضش|ماتخفضش|مش)\b', clause):
        return None
    if 'credit' in drivers and re.search(rf'\b{CREDIT}\b', clause):
        key = 'credit'
    elif 'supplier' in drivers and re.search(SUPPLIER, clause) and not re.search(r'\b(?:دفع|سداد|تحصيل)\b', clause):
        key = 'supplier'
    else:
        matches = {(key, label) for key, row in drivers.items()
                   for label in (normalize(key), normalize(row['name_ar']))
                   if re.search(rf'(?<!\w){re.escape(label)}(?!\w)', clause)}
        if len({key for key, _ in matches}) != 1:
            return None
        key, label = max(matches, key=lambda item: len(item[1]))
        clause = clause.replace(label, ' ')
    if key == 'supplier':
        if not AMOUNT.search(clause):
            clause = re.sub(r'\b(اسبوع|يوم)\b', r'1 \1', clause)
    numbers = list(AMOUNT.finditer(clause))
    if len(numbers) != 1:
        return None
    token = numbers[0][0]
    value = float(token)
    if '%' in clause:
        if re.search(DECREASE, clause) or re.search(r'\bخفض\w*\b', clause):
            value = -abs(value)
        elif re.search(INCREASE, clause) or re.search(r'\bرفع\w*\b', clause):
            value = abs(value)
        elif not token.startswith(('+', '-')):
            return None
        return Assumption(node_id=key, operation='pct', value=value)
    if key == 'supplier' and re.search(DELAY, clause):
        if re.search(r'\bاسبوع(?:ات|ا)?\b|\bاسابيع\b', clause):
            value *= 7
        elif not re.search(r'\b(?:يوم|ايام)\b', clause):
            return None
        return Assumption(node_id=key, operation='add', value=value)
    if re.search(r'\b(?:يبقي|تبقي|يصبح|تصبح|اضبط|حدد|الي)\b', clause):
        return Assumption(node_id=key, operation='set', value=value)
    return None


def parse_rules(message, model):
    drivers = {row['id']: row for row in model.drivers()}
    assumptions = []
    text = number_words(normalize(message)).strip(' ؟?')
    text = re.sub(r'[ ،,]*(?:ايه الاثر|ايه اللي هيحصل|هيحصل ايه)$', '', text)
    for clause in CLAUSES.split(text.strip(' ،,؛;')):
        try:
            change = _rule_change(clause, drivers)
        except ValidationError:
            return EnterpriseScenario(intent='help')
        if change is None:
            return EnterpriseScenario(intent='help')
        assumptions.append(change)
    if len({item.node_id for item in assumptions}) != len(assumptions) or len(assumptions) > 50:
        return EnterpriseScenario(intent='help')
    scenario = EnterpriseScenario(intent='what_if', assumptions=assumptions)
    try:
        validate_enterprise_changes(model, scenario)
    except (ImpactError, FloatingPointError, OverflowError):
        return EnterpriseScenario(intent='help')
    return scenario


def _display(value, unit):
    if abs(value) < .005 and value:
        formatted = f'{value:.2e}'
    else:
        formatted = f'{value:,.2f}'.rstrip('0').rstrip('.')
    if unit == '%':
        return f'\u2066{formatted}%\u2069'
    if value < 0:
        formatted = f'\u2066{formatted}\u2069'
    return f'{formatted} {UNIT_LABELS[unit]}'


def grounded_explanation(result):
    rows = {row['id']: row for row in result['nodes'] if row['before'] is not None and row['after'] is not None}
    selected = [rows[key] for key in COMPARE_FIRST if key in rows]
    selected.extend(row for key, row in rows.items()
                    if key not in COMPARE_FIRST and row['before'] != row['after'])
    if not selected:
        return 'النموذج ما حسبش تغيّر في المؤشرات المتاحة.'
    parts = []
    for row in selected[:6]:
        name = 'أقل رصيد سيولة' if row['id'] == 'cash' else row['name_ar']
        if row['id'] == 'risk.credit':
            parts.append(f"{name} بعد التغيير بلغ {_display(row['after'], row['unit'])} حسب المحاكاة.")
        else:
            parts.append(f"«{name}» من {_display(row['before'], row['unit'])} قبل السيناريو "
                         f"لـ{_display(row['after'], row['unit'])} بعده.")
    return ' '.join(parts)
