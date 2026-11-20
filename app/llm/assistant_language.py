"""Shared vocabulary for Egyptian, Gulf and standard Arabic planning questions."""
import re

from app.llm.rules_ar import normalize

METRICS = {
    'cash': ('السيولة', 'سيولة', 'الكاش', 'كاش', 'النقد', 'النقدية', 'الرصيد النقدي', 'رصيد النقد'),
    'pnl.result': ('الربح', 'ربح', 'الارباح', 'ارباح', 'ربحية', 'صافي الدخل', 'النتيجة بعد التمويل'),
    'pnl.revenue': ('المبيعات', 'مبيعات', 'الايراد', 'الايرادات', 'ايراد', 'ايرادات', 'الدخل'),
    'sales.volume': ('حجم المبيعات', 'عدد الوحدات المباعة', 'كمية المبيعات'),
    'pnl.direct_cost': ('التكلفة المباشرة', 'التكاليف المباشرة'),
    'cost.total': ('التكاليف', 'تكاليف', 'التكلفة', 'تكلفة', 'المصاريف', 'كلفة'),
    'demand': ('الطلب', 'طلب'),
    'supplier.delay': ('مهلة المورد بالأيام',),
    'credit': ('حد الائتمان', 'الحد الائتماني', 'حد التمويل', 'التسهيل الائتماني', 'الائتمان'),
    'supplier': ('المورد', 'مورد', 'مورد الخامات', 'مهلة التوريد', 'التوريد'),
}
DECREASE = r'خفض\w*|انخفض\w*|قل\w*|نزل\w*|هبط\w*|تراجع\w*|نقص\w*'
INCREASE = r'زود\w*|زد\w*|زاد\w*|زياده|رفع\w*|ارتفع\w*|طلع\w*|علي\w*'
WHY = r'ليه|لماذا|ليش|شنو السبب|وش السبب|ايش السبب|شو السبب|سبب|فسر\w*'
QUESTION_TAIL = re.compile(
    r'(?:[،,؟?]\s*|\s+)(?:و\s*)?(?:ايه|ايش|وش|شو|شنو|ماذا|ما الاثر|ما تاثير|'
    r'ازاي|كيف|هيحصل|هل)\b.*$'
)


def has(text, words):
    return bool(re.search(rf'(?<!\w)(?:و)?(?:{words})(?!\w)', text))


def alias(text, labels):
    return any(has(text, re.escape(normalize(label))) for label in labels)


def metrics(text):
    result = [key for key, labels in METRICS.items() if alias(text, labels)]
    if 'sales.volume' in result and 'pnl.revenue' in result and not alias(text, ('الايراد', 'الايرادات', 'الدخل')):
        result.remove('pnl.revenue')
    if 'pnl.direct_cost' in result and 'cost.total' in result:
        result.remove('cost.total')
    if 'pnl.result' in result and alias(text, ('صافي الدخل',)) and 'pnl.revenue' in result:
        result.remove('pnl.revenue')
    return result


def intent(text):
    if has(text, r'لو|اذا|ماذا لو|افترض\w*|سيناريو') or (
        re.search(r'\d', text) and has(text, rf'{INCREASE}|{DECREASE}|اخر\w*|اتاخر\w*|تاخر\w*')
        and not has(text, WHY)
    ):
        return 'what_if'
    if has(text, f'{WHY}|{DECREASE}|{INCREASE}'):
        return 'explain'
    return 'query'
