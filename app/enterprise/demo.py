"""The enterprise template reuses the packaged V1 sector inputs."""
from collections import defaultdict
from datetime import date, timedelta

from app.enterprise.schema import Arm, Edge, Engine, EngineLink, Formula, Graph, Node, Scalar, Series, Template
from app.ingestion.loader import load_demo
from app.templates_registry import load_templates

SAR_PER_THOUSAND = 1000


def demo_template() -> Template:
    production = load_demo('production')
    contracting = load_demo('contracting')
    templates = load_templates()
    prod = templates['production'].model_defaults()['parameters']
    cont = templates['contracting'].model_defaults()['parameters']
    # The enterprise has one facility sized from the contracting demo and one opening balance.
    nodes, edges = [], []

    def node(key, kind, name, unit, definition, value=None, *, baseline=None):
        nodes.append(Node(id=key, type=kind, name_ar=name, unit=unit,
                          baseline=baseline if baseline is not None else Scalar(value=value),
                          definition=Formula(expression=definition) if isinstance(definition, str) else definition))

    def edge(source, target, label):
        edges.append(Edge(source=source, target=target, name_ar=label))

    def engine(sector, output, entity_id=None, *, forecast=False):
        return EngineLink(engine=Engine.FORECAST if forecast else Engine(f'{sector}_optimizer'),
                          sector=sector, output=output, entity_id=entity_id)

    skus = {sku['id']: sku for sku in production['skus']}
    booked = sum(order['qty'] * skus[order['sku_id']]['unit_price'] for order in production['orders'])
    node('sales.production', 'sales', 'قيمة طلبات الإنتاج', 'SAR', 'مجموع كميات الطلبات المؤكدة × أسعار بيعها', booked)
    node('procurement', 'procurement', 'مشتريات وتشغيل المصنع', 'SAR', 'مجموع كميات المشتريات × تكلفتها')
    node('supplier', 'supplier', 'مهلة مورد الخامات', 'days', 'افتراض الديمو لمدة توريد الخامات', 5)
    node('storage', 'storage', 'سعة المخزن المبرد', 'storage_units', 'سعة التبريد من نموذج الإنتاج', prod['cold_capacity'])

    start = date.fromisoformat(prod['plan_start_date'])
    history = defaultdict(list)
    for observation in production['demand_history']:
        history[observation['sku_id']].append(observation)
    for key, sku in skus.items():
        rows = sorted(history[key], key=lambda row: row['week'])
        points = [{'date': start - timedelta(weeks=rows[-1]['week'] - row['week'] + 1),
                   'value': row['qty']} for row in rows]
        node(f'demand.{key}', 'demand', f"طلب {sku['name_ar']}", 'units',
             engine('production', 'series.p50', key, forecast=True),
             baseline=Series(frequency='weekly', points=points))
        node(f'price.{key}', 'pricing', f"سعر {sku['name_ar']}", 'SAR/unit',
             'سعر الوحدة من كتالوج الإنتاج', sku['unit_price'])
        node(f'inventory.{key}', 'inventory', f"مخزون {sku['name_ar']}", 'units',
             engine('production', 'plan.inventory', key), sku['initial_inventory'])
        edge(f'demand.{key}', 'sales.production', 'كميات الطلب')
        edge(f'price.{key}', 'sales.production', 'سعر البيع')
        edge('storage', f'inventory.{key}', 'سعة التخزين المتاحة')
        edge(f'inventory.{key}', 'ar.production', 'توافر المنتج للشحن')
        edge(f'inventory.{key}', 'pnl.revenue', 'توافر المنتج للشحن')

    for line in production['lines']:
        key = line['id']
        eligible = [row for row in production['rates'] if row['line_id'] == key and row['units_per_day'] > 0]
        node(f'line.{key}', 'production_line', line['name_ar'], 'units',
             engine('production', 'plan.schedule', key))
        edge('cash', f'line.{key}', 'سيولة التشغيل')
        edge('credit', f'line.{key}', 'تمويل خطة الإنتاج')
        edge('storage', f'line.{key}', 'قيد سعة التبريد')
        edge('supplier', f'line.{key}', 'توقيت وصول الخامات')
        node(f'downtime.{key}', 'production_line', f"أيام توقف {line['name_ar']}", 'days',
             'عدد أيام التوقف من بداية خطة المصنع', 0)
        edge(f'downtime.{key}', f'line.{key}', 'أيام الخط المتاحة')
        edge(f'line.{key}', 'procurement', 'احتياجات الخامات')
        for row in eligible:
            sku = row['sku_id']
            edge(f'demand.{sku}', f'line.{key}', 'تخطيط الكمية')
            edge(f'price.{sku}', f'line.{key}', 'هامش الشحن في خطة الإنتاج')
            edge(f'line.{key}', f'inventory.{sku}', 'وحدات منتجة')
        edge(f'line.{key}', 'pnl.expenses', 'تكلفة التبديل والتخزين وتأخير الشحن')
        edge(f'line.{key}', 'pnl.direct_cost', 'تكلفة المنتجات المشحونة')

    activities = defaultdict(list)
    for activity in contracting['activities']:
        activities[activity['project_id']].append(activity)
    for resource in contracting['resources']:
        node(f"workforce.{resource['id']}", 'workforce', resource['name_ar'], 'teams',
             'سعة الفرق من بيانات المقاولات', resource['capacity'])
    for project in contracting['projects']:
        key = project['id']
        scope = activities[key]
        node(f'project.{key}', 'project', project['name_ar'], 'weeks',
             engine('contracting', 'plan.activities', key), sum(row['duration_weeks'] for row in scope))
        for resource in sorted({row['resource'] for row in scope}):
            edge(f'workforce.{resource}', f'project.{key}', 'تخصيص الفرق')
        edge('cash', f'project.{key}', 'سيولة تنفيذ الأنشطة')
        edge('credit', f'project.{key}', 'تمويل خطة المشروع')
        edge(f'project.{key}', 'ar.contracting', 'مستخلصات تحت التحصيل')
        edge(f'project.{key}', 'ap', 'تكلفة التنفيذ')
        edge(f'project.{key}', 'pnl.revenue', 'إيراد الأنشطة المنفّذة')
        edge(f'project.{key}', 'pnl.direct_cost', 'تكلفة الأنشطة المنفّذة')
        edge(f'project.{key}', 'pnl.expenses', 'غرامات التأخير')
        edge(f'project.{key}', 'cash', 'مدفوعات الأعمال')

    node('ar.production', 'receivables', 'مستحقات عملاء المصنع', 'SAR', 'قيمة الشحنات اللي لسه متحصّلتش')
    node('ar.contracting', 'receivables', 'مستحقات عملاء المقاولات', 'SAR', 'المستخلصات اللي لسه متحصّلتش بعد تنفيذ الأعمال')
    node('ap', 'payables', 'مستحقات الموردين والمقاولين', 'SAR', 'رصيد الالتزامات غير المدفوعة على الذراعين')
    node('cash', 'cash', 'السيولة المشتركة', 'SAR', 'رصيد أول المدة + التحصيلات − المدفوعات + صافي السحب من التسهيل',
         prod['opening_cash'])
    node('credit', 'credit', 'حد الائتمان الموحّد', 'SAR', 'حد ديمو المقاولات × 1000 للتحويل من ألف ريال إلى ريال',
         cont['credit_limit'] * SAR_PER_THOUSAND)
    node('financing.draw', 'financing', 'السحب من التسهيل', 'SAR', 'السحب المطلوب في حدود التسهيل المشترك')
    node('financing', 'financing', 'تكلفة التمويل', 'SAR', 'الفائدة على الرصيد المسحوب من التسهيل المشترك')
    node('pnl.revenue', 'profit_loss', 'الإيراد الموحّد', 'SAR', 'إيراد الشحنات المعترف بيه + إيراد أعمال المقاولات المنفّذة')
    node('pnl.direct_cost', 'profit_loss', 'التكاليف المباشرة', 'SAR', 'تكلفة المنتجات المشحونة + تكلفة الأنشطة المنفّذة')
    node('pnl.expenses', 'profit_loss', 'مصاريف التشغيل والغرامات', 'SAR', 'التبديل والتخزين وغرامات التأخير')
    node('pnl.result', 'profit_loss', 'النتيجة بعد التمويل', 'SAR', 'الإيراد − التكاليف المباشرة − مصاريف التشغيل − تكلفة التمويل')
    node('supplier.payment_lag', 'payables', 'مهلة سداد الخامات', 'days', 'مهلة سداد تكلفة وحدات الإنتاج', 0)
    node('risk.credit', 'risk_kpi', 'احتمال تجاوز حد الائتمان', '%', 'نسبة الحالات اللي فيها الاحتياج للسحب بيتجاوز الحد')
    node('risk.storage', 'risk_kpi', 'ذروة إشغال التبريد', '%', engine('production', 'kpis.peak_storage_pct'))
    node('risk.delay', 'risk_kpi', 'إجمالي تأخير المشروعات', 'weeks', engine('contracting', 'kpis.late_weeks_total'))

    for source, target, label in [
        ('sales.production', 'ar.production', 'استحقاقات بعد الشحن'),
        ('sales.production', 'pnl.revenue', 'إيراد المصنع بعد الشحن'),
        ('procurement', 'ap', 'التزامات الشراء'),
        ('procurement', 'cash', 'مدفوعات الخامات'),
        ('supplier.payment_lag', 'ap', 'توقيت سداد المشتريات'),
        ('supplier.payment_lag', 'cash', 'توقيت المدفوعات'),
        ('pnl.expenses', 'cash', 'سداد المصاريف والغرامات'),
        ('ar.production', 'cash', 'تحصيل مبيعات المصنع'),
        ('ar.contracting', 'cash', 'تحصيل المستخلصات'),
        ('ap', 'cash', 'مدفوعات الذراعين'),
        ('cash', 'financing.draw', 'الاحتياج للسحب'),
        ('credit', 'financing.draw', 'سقف السحب'),
        ('financing.draw', 'cash', 'أثر السحب والسداد'),
        ('financing.draw', 'financing', 'الرصيد المسحوب'),
        ('pnl.revenue', 'pnl.result', 'الإيراد'),
        ('pnl.direct_cost', 'pnl.result', 'التكاليف المباشرة'),
        ('pnl.expenses', 'pnl.result', 'مصاريف التشغيل'),
        ('financing', 'pnl.result', 'الفوائد'),
        ('cash', 'risk.credit', 'الاحتياج النقدي'),
        ('credit', 'risk.credit', 'الحد المتاح'),
        ('storage', 'risk.storage', 'سعة التبريد'),
    ]:
        edge(source, target, label)
    for sku in skus:
        edge(f'inventory.{sku}', 'risk.storage', 'المخزون المشغول')
    for project in contracting['projects']:
        edge(f"project.{project['id']}", 'risk.delay', 'الانحراف عن موعد التسليم')

    return Template(name='شركة روافد للتصنيع والمقاولات', graph=Graph(
        nodes=nodes, edges=edges, arms=[
            Arm(sector=sector, input_source='v1_demo') for sector in ('production', 'contracting')
        ],
    ))
