"""Sector presets and the nine ordered onboarding contracts."""
from dataclasses import dataclass

from app.enterprise.schema import Graph
from app.onboarding.records import OnboardingData

SECTORS = {
    'trading': 'التجارة والتوزيع',
    'retail': 'التجزئة',
    'services': 'الخدمات والاستشارات',
    'custom': 'قطاع مخصص',
}
SECTIONS = {
    'structure': 'الهيكل والأقسام',
    'sales': 'المبيعات والطلب',
    'catalog': 'المنتجات والخدمات والتسعير',
    'operations': 'الإنتاج والمشاريع',
    'inventory': 'المخزون',
    'suppliers': 'الموردين',
    'workforce': 'العمالة',
    'finance': 'المالية',
    'risk': 'المخاطر',
}


@dataclass(frozen=True)
class Input:
    section: str
    label: str
    kind: str
    unit: str
    optional: bool = False


INPUTS = {
    'demand': Input('sales', 'الطلب الأسبوعي', 'demand', 'units'),
    'price': Input('catalog', 'متوسط سعر البيع للوحدة', 'pricing', 'SAR/unit'),
    'unit_cost': Input('catalog', 'متوسط التكلفة المباشرة للوحدة', 'procurement', 'SAR/unit'),
    'capacity': Input('operations', 'الطاقة التشغيلية الأسبوعية', 'production_line', 'units'),
    'inventory': Input('inventory', 'المخزون المتاح', 'inventory', 'units'),
    'supplier.delay': Input('suppliers', 'مهلة المورد بالأيام', 'supplier', 'days'),
    'workforce': Input('workforce', 'عدد فرق العمل', 'workforce', 'teams'),
    'cash.opening': Input('finance', 'رصيد الكاش الافتتاحي', 'cash', 'SAR'),
    'ar': Input('finance', 'ذمم العملاء المتوقع تحصيلها خلال الأسبوع', 'receivables', 'SAR'),
    'ap': Input('finance', 'ذمم الموردين المتوقع سدادها خلال الأسبوع', 'payables', 'SAR'),
    'credit': Input('finance', 'حد الائتمان المتاح', 'credit', 'SAR'),
    'financing': Input('finance', 'تكلفة التمويل الأسبوعية', 'financing', 'SAR'),
    'pnl.expenses': Input('finance', 'المصروفات الثابتة الأسبوعية', 'profit_loss', 'SAR'),
    'risk.exposure': Input('risk', 'تقدير المخاطر التشغيلية (0–100%)', 'risk_kpi', '%'),
}
ITEMS = {
    'structure': ('department', 'اسم القسم'),
    'catalog': ('offering', 'اسم المنتج أو الخدمة'),
}
OUTPUTS = {
    'sales.volume': ('المبيعات الأسبوعية المتوقعة', 'sales', 'units', ('demand', 'capacity')),
    'pnl.revenue': ('الإيراد الأسبوعي المتوقع', 'profit_loss', 'SAR', ('sales.volume', 'price')),
    'pnl.direct_cost': ('التكلفة المباشرة الأسبوعية', 'profit_loss', 'SAR', ('sales.volume', 'unit_cost')),
    'pnl.result': ('النتيجة الأسبوعية بعد التمويل', 'profit_loss', 'SAR',
                   ('pnl.revenue', 'pnl.direct_cost', 'pnl.expenses', 'financing')),
    'cash': ('رصيد الكاش المتوقع بعد أسبوع', 'cash', 'SAR', ('cash.opening', 'ar', 'ap', 'pnl.result')),
    'risk.shortfall': ('العجز المتوقع بعد الائتمان', 'risk_kpi', 'SAR', ('cash', 'credit')),
}
# Defaults are assumptions for initial what-if work, with no claim of calibration.
DRIVER_RULES = {
    'trading': [('inventory', 'sales.volume', .25), ('supplier.delay', 'sales.volume', -.2),
                ('workforce', 'capacity', .4), ('price', 'demand', -.6), ('risk.exposure', 'capacity', -.2)],
    'retail': [('inventory', 'sales.volume', .45), ('supplier.delay', 'sales.volume', -.15),
               ('workforce', 'capacity', .55), ('price', 'demand', -1.1), ('risk.exposure', 'capacity', -.2)],
    'services': [('workforce', 'capacity', .85), ('supplier.delay', 'capacity', -.15),
                 ('price', 'demand', -.35), ('risk.exposure', 'capacity', -.15)],
    'custom': [('workforce', 'capacity', .5), ('supplier.delay', 'capacity', -.2),
               ('price', 'demand', -.5), ('risk.exposure', 'capacity', -.2)],
}


def field(sector, key):
    item = INPUTS[key]
    if key == 'inventory' and sector == 'services':
        return Input(item.section, item.label, item.kind, item.unit, optional=True)
    if key == 'capacity' and sector == 'services':
        return Input(item.section, item.label, 'project', item.unit)
    return item


def section_specs(sector):
    return [
        {'id': key, 'name': title, 'fields': [
            {'key': node, 'label': spec.label, 'unit': spec.unit, 'optional': spec.optional}
            for node in INPUTS if (spec := field(sector, node)).section == key
        ], 'item': ITEMS.get(key)}
        for key, title in SECTIONS.items()
    ]


def graph_for(sector):
    nodes = []
    for key in INPUTS:
        spec = field(sector, key)
        nodes.append(dict(id=key, type=spec.kind, name_ar=spec.label, unit=spec.unit,
                          baseline={'kind': 'scalar', 'value': None},
                          definition={'kind': 'formula', 'expression': 'مدخل من تجهيز الشركة'}))
    nodes += [
        dict(id=key, type=kind, name_ar=label, unit=unit, baseline={'kind': 'scalar', 'value': None},
             definition={'kind': 'formula', 'expression': 'ناتج تقديري من مدخلات النموذج'})
        for key, (label, kind, unit, _) in OUTPUTS.items()
    ]
    edges = [
        dict(source=source, target=target, name_ar='مدخل للحساب')
        for target, (_, _, _, sources) in OUTPUTS.items() for source in sources
    ]
    edges += [
        dict(source=source, target=target, name_ar='مرونة افتراضية للقطاع',
             rule={'mode': 'elasticity', 'multiplier': multiplier})
        for source, target, multiplier in DRIVER_RULES[sector]
    ]
    return Graph.model_validate(dict(nodes=nodes, edges=edges, onboarding=OnboardingData(sector=sector)))
