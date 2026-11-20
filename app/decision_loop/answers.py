"""Server-rendered sector answers and field-by-field numeric provenance."""
import re

from app.llm.explain import TOTAL, UNITS, number_tokens
from app.llm.rules_ar import normalize, number_words, parse
from app.llm.schema import Scenario


SPECIAL = {
    'diagnose': re.compile(r'(?:ايه |فين )?(?:تعارض القيود|القيود المتعارضه)|شخص التعارض|القيود بتتعارض ليه'),
    'binding': re.compile(r'(?:ايه |فين )?(?:القيود الخانقه|القيود اللي خانقه الحل|عنق الزجاجه)'),
    'proposal': re.compile(r'(?:اقترح|راجع) (?:صياغه )?(?:قيد الائتمان|حد الائتمان)(?: (?:كقيد )?(?:الزامي|مرن))?'),
}

# These aliases preserve the existing typed parser, which expects the subject
# before a recognized direction in these common comparisons.
COLLOQUIAL_INCREASE = r'\b(?:زودنا|زودت|نزود|زود|رفعنا|نرفع|علينا)\b'
COLLOQUIAL_DECREASE = r'\b(?:قللنا|خفضنا|نقصنا|ننقص|نخفض)\b'
UNTRUSTED_TEXT = re.compile(
    r'[\r\n<>`"“”«»]|https?://|\b(?:ignore|system|assistant|document|tool|execute)\b|'
    r'تجاهل|تعليمات|المستند|الملف|تجاوز الصلاحي|نفذ الاداه|من غير اذن|اعتمد الخطه'
)


def read_request(message, ctx):
    cleaned = normalize(message)
    safe_help = Scenario(template=ctx.template, intent='help')
    if UNTRUSTED_TEXT.search(message.lower()) or UNTRUSTED_TEXT.search(cleaned):
        return None, safe_help, False
    for tool, pattern in SPECIAL.items():
        if pattern.fullmatch(cleaned.strip('؟? .')):
            return tool, Scenario(template=ctx.template, intent='explain', confidence=1), False
    cleaned = re.sub(COLLOQUIAL_INCREASE, 'زاد', cleaned)
    cleaned = re.sub(COLLOQUIAL_DECREASE, 'نقص', cleaned)
    authorized = parse(cleaned, ctx)
    if authorized.intent == 'optimize' and not re.fullmatch(
        r'(?:شغل التحسين|اعمل خطه جديده|اعد التحسين)[؟?.]?', cleaned,
    ):
        return None, safe_help, False
    if authorized.intent == 'what_if':
        # Relative and absolute quantities are distinct. Refuse unexplained
        # extra quantities even if the permissive legacy parser took the first.
        amounts = re.findall(r'(?<!\w)[+-]?\d+(?:\.\d+)?\s*%', number_words(cleaned))
        targets = [re.search(pattern, cleaned) for pattern in (
            r'ائتمان', r'\b(?:الطلب|طلب)\b', r'التبريد|المخزن المبرد|التخزين المبرد', r'تكلفه (?:التغيير|التبديل)',
        )]
        if (len(authorized.changes) != 1 or len(amounts) > 1 or sum(bool(match) for match in targets) > 1
                or re.search(r'\b(?:او|ولا)\b', cleaned)):
            return None, safe_help, False
    return None, authorized, True


class Answer:
    def __init__(self, template, run_id, record):
        self.template = template
        self.run_id = run_id
        self.record = record
        self.evidence = []
        self.unit = UNITS[template]

    def value(self, metric, value, path, unit, *, signed=False, constraint_id=None, root='result', source_url=None):
        if type(value) not in (int, float):
            return '—'
        display = format(value, '+,' if signed else ',')
        self.evidence.append({
            'metric': metric, 'value': value, 'display': display, 'path': path,
            'unit': unit, 'run_id': self.run_id, 'constraint_id': constraint_id,
            'root': root, 'source_url': source_url or f'/api/runs/{self.run_id}',
            'dataset_id': self.record['snapshot']['dataset_id'],
            'model_version': self.record['snapshot']['model_version'],
        })
        return f'{display} {unit}'.strip()

    def reference(self, variant, approved, cid):
        if approved:
            value = self.value('approved_plan_id', approved['id'], ['id'], '', root='plan',
                               source_url=f'/api/companies/{cid}/plans')
            label = 'الخطة المعتمدة رقم ' + value
            if variant == 'scenario':
                label += ' · السيناريو'
            return label + '.'
        value = self.value('run_id', self.run_id, ['id'], '', root='run')
        return ('السيناريو رقم ' if variant == 'scenario' else 'الخطة المحسّنة من التشغيل رقم ') + value + '.'

    def cost(self, result, variant, compare=False):
        key = TOTAL[self.template]
        value = self.value(key, result[variant]['kpis'].get(key), [variant, 'kpis', key], self.unit)
        prefix = f'إجمالي التكلفة {value}.'
        if not compare:
            return prefix
        reference = 'base' if variant == 'scenario' else 'baseline'
        before = self.value(key, result[reference]['kpis'].get(key), [reference, 'kpis', key], self.unit)
        if before == '—':
            return prefix + ' مفيش تكلفة مرجعية متاحة.'
        path = ['diff', key, 'delta'] if variant == 'scenario' else ['solution_record', 'cost_delta']
        delta = result['diff'][key]['delta'] if variant == 'scenario' else self.record['cost_delta']
        difference = self.value(key + '_delta', delta, path, self.unit, signed=True)
        return f'{prefix} المرجع {before}، والفرق {difference}.'

    def query(self, query, result, variant):
        contracting = self.template == 'contracting'
        if query == 'total':
            return self.cost(result, variant)
        key, label, unit = {
            'late': ('late_weeks_total' if contracting else 'late_orders',
                     'إجمالي أسابيع التأخير' if contracting else 'الطلبات المتأخرة',
                     'أسبوع' if contracting else 'طلب'),
            'peak_od': ('peak_od', 'أعلى عجز نقدي', self.unit),
            'ld': ('ld' if contracting else 'lateness_cost', 'غرامات التأخير', self.unit),
            'changeovers': ('changeovers', 'تبديلات المنتجات', 'تبديل'),
            'storage': ('peak_storage_pct', 'ذروة إشغال التبريد', '%'),
        }[query]
        field = 'delayed_projects' if query == 'late' and contracting else None
        if field:
            value = self.value(field, self.record.get(field), ['solution_record', field], 'مشروع')
            label = 'المشاريع المتأخرة'
        else:
            value = self.value(key, result[variant]['kpis'].get(key), [variant, 'kpis', key], unit)
        return f'{label} {value}.' if value != '—' else 'المؤشر ده مش متاح في التشغيل.'

    def subject(self, subject, dataset, result, variant):
        kpis = result[variant]['kpis']
        if self.template == 'contracting':
            project = next(row for row in dataset['projects'] if row['id'] == subject)
            # Dataset labels may contain numbers. Numeric prose comes from typed fields.
            name = project.get('name_ar', '')
            if not name or any(number_tokens(name)) or UNTRUSTED_TEXT.search(normalize(name)):
                name = 'المشروع'
            late = self.value('late', kpis.get('late', {}).get(subject), [variant, 'kpis', 'late', subject], 'أسبوع')
            return f'{name}، التأخير المسجل {late}.' if late != '—' else 'المشروع مفيهوش أنشطة مجدولة.'
        for index, shipment in enumerate(kpis.get('shipments', [])):
            if shipment['order'] == subject:
                day = self.value('shipment_day', shipment['day'], [variant, 'kpis', 'shipments', index, 'day'], 'يوم')
                late = self.value('late_days', shipment['late_days'],
                                  [variant, 'kpis', 'shipments', index, 'late_days'], 'يوم')
                return f'موعد الشحن في الخطة عند اليوم {day}، والتأخير {late}.'
        return 'الطلب غير مشحون في الخطة الناتجة.'

    def constraints(self, *, all_checks=False):
        rows = self.record['optimized']['constraints']
        lines = []
        for key, item in rows.items():
            if item['overrun']:
                value = self.value('overrun', item['overrun'],
                                   ['solution_record', 'optimized', 'constraints', key, 'overrun'],
                                   item['unit'], constraint_id=key)
                kind = 'إلزامي' if item['kind'] == 'hard' else 'مرن'
                lines.append(f'{item["label_ar"]}، قيد {kind}، التجاوز {value}.')
            elif all_checks and item['binding_count']:
                count = self.value('binding_count', item['binding_count'],
                                   ['solution_record', 'optimized', 'constraints', key, 'binding_count'],
                                   'حالة', constraint_id=key)
                lines.append(f'{item["label_ar"]} عند الحد في {count}.')
        return ' '.join(lines)

    def risk(self, result):
        risk = result.get('risk_assessment')
        if risk is None:
            return ''
        for index, row in enumerate(risk['candidates']):
            if row['id'] == risk['selected_id']:
                path = ['risk_assessment', 'candidates', index, 'assessment']
                cost = self.value('expected_cost', row['assessment']['mean']['cost_sar'],
                                  [*path, 'mean', 'cost_sar'], self.unit)
                tail = self.value('cost_cvar', row['assessment']['cvar']['cost_sar'],
                                  [*path, 'cvar', 'cost_sar'], self.unit)
                return (f'بديل السيناريوهات حقق أقل درجة بين البدائل المؤهلة للتجربة. '
                        f'متوسط تكلفته {cost}، ومتوسط تكلفة الذيل (CVaR) {tail}.')
        return 'بوابة المخاطر استبعدت كل البدائل من الاختيار التجريبي.'

    def summary(self, result, variant, *, compare=False, show_limit=False):
        solver = self.record['solver']
        status = solver.get('search_status') or solver['status']
        verdict = self.record['optimized']
        eligible = 'الخطة مؤهلة للاعتماد.' if verdict['eligible'] else 'الخطة غير مؤهلة للاعتماد.'
        text = f'حالة السولفر {status}. {eligible}'
        if 'inputs_unapproved' in verdict['reasons']:
            text += ' المدخلات محتاجة اعتماد.'
        if not verdict['has_candidate']:
            label = 'خطة مركزية' if 'risk_assessment' in result else 'خطة'
            return ' '.join(filter(None, (
                text + f' مفيش {label} قابلة للعرض للتغييرات دي.', self.risk(result),
            )))
        if show_limit and (change := self.record.get('changed_input')):
            key, unit, name = change['metric'], change['unit'], change['label_ar']
            constraint_id = change.get('constraint_id')
            prior = self.value(key, change['before'], ['solution_record', 'changed_input', 'before'],
                               unit, constraint_id=constraint_id)
            new = self.value(key, change['after'], ['solution_record', 'changed_input', 'after'],
                             unit, constraint_id=constraint_id)
            text += f' {name} اتغير من {prior} إلى {new}.'
        return ' '.join(filter(None, (text, self.cost(result, variant, compare), self.constraints(), self.risk(result))))
