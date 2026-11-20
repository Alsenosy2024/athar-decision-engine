import csv
import re
from datetime import date, timedelta
from io import BytesIO, StringIO
from itertools import chain
from xml.etree import ElementTree as ET

from openpyxl import Workbook
from openpyxl.utils import get_column_letter

from app.db import encode
from app.schemas import api_error

INVALID_XML_TEXT = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f\ufffe\uffff]')

ACTIVITY_COLUMNS = (
    'activity_id', 'project', 'phase', 'start_week', 'finish_week', 'start_date', 'finish_date',
    'resource', 'units',
)
PRODUCTION_COLUMNS = ('date', 'line', 'sku', 'qty')
SHIPMENT_COLUMNS = ('order', 'sku', 'qty', 'ship_date', 'due_date', 'late_days')
P6_COLUMNS = ('Activity ID', 'Activity Name', 'WBS', 'Planned Start', 'Planned Finish',
              'Original Duration', 'Predecessors', 'Resource')
FORMATS = {
    'xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    'csv': 'text/csv; charset=utf-8',
    'json': 'application/json',
    'p6.csv': 'text/csv; charset=utf-8',
    'p6.xml': 'application/xml',
}


def calendar_dates(parameters, key, days_per_period):
    try:
        origin = date.fromisoformat(parameters[key])
    except ValueError:
        raise api_error(422, 'invalid_start_date', f'تاريخ بدء الخطة غير صالح ({key}).') from None

    def iso(period):
        try:
            return (origin + timedelta(days=period * days_per_period)).isoformat()
        except OverflowError:
            raise api_error(422, 'invalid_plan_date', 'تاريخ الخطة خارج النطاق المدعوم.') from None

    return iso


def contracting_tables(plan, kpis, parameters):
    iso = calendar_dates(parameters, 'project_start_date', 7)
    curve = kpis['cash_curve']
    activities = [
        (r['activity_id'], r['project'], r['phase'], r['start_week'], r['finish_week'],
         iso(r['start_week']), iso(r['finish_week']), r['resource'], r['units'])
        for r in plan['activities']
    ]
    return {
        'Activities': (ACTIVITY_COLUMNS, activities),
        'KPIs': kpi_table(kpis),
        'CashFlow': (('week', 'date', 'balance', 'net_flow'), [
            (week, iso(week), balance, balance - (curve[week - 1] if week else 0))
            for week, balance in enumerate(curve)
        ]),
    }


def production_tables(plan, kpis, dataset, parameters):
    iso = calendar_dates(parameters, 'plan_start_date', 1)
    orders = {row['id']: row for row in dataset['orders']}
    shipments = []
    for row in plan['shipments']:
        order = orders[row['order']]
        shipments.append((order['id'], order['sku_id'], order['qty'], iso(row['day']),
                          iso(order['due_day']), row['late_days']))
    return {
        'ProductionPlan': (PRODUCTION_COLUMNS, [
            (iso(row['day']), row['line'], row['sku'], row['qty']) for row in plan['schedule']
        ]),
        'Shipments': (SHIPMENT_COLUMNS, shipments),
        'Inventory': (('date', 'sku', 'qty'), [
            (iso(day), sku, qty)
            for sku, values in plan['inventory'].items() for day, qty in enumerate(values)
        ]),
        'KPIs': kpi_table(kpis),
    }


def kpi_table(kpis):
    return ('kpi', 'value'), [
        (key, value if isinstance(value, (bool, int, float, str)) else encode(value))
        for key, value in kpis.items()
    ]


def csv_file(columns, rows):
    stream = StringIO(newline='')
    writer = csv.writer(stream)
    writer.writerow(columns)
    writer.writerows(rows)
    return stream.getvalue().encode('utf-8-sig')


def excel_file(tables, *, rtl=False):
    book = Workbook()
    book.properties.creator = 'أثر | Athar'
    book.properties.title = 'أثر | Athar'
    book.properties.subject = 'كل قرار.. وأثره على شركتك كلها'
    book.remove(book.active)
    for name, (columns, rows) in tables.items():
        sheet = book.create_sheet(name)
        sheet.freeze_panes = 'A2'
        if rtl:
            sheet.sheet_view.rightToLeft = True
        for row_number, row in enumerate(chain((columns,), rows), 1):
            if rtl:
                row = [INVALID_XML_TEXT.sub('', value) if isinstance(value, str) else value for value in row]
            sheet.append(row)
            for column, value in enumerate(row, 1):
                if isinstance(value, str):
                    sheet.cell(row_number, column).data_type = 's'
        if rtl:
            for cells in sheet.columns:
                width = max(len(str(cell.value)) for cell in cells if cell.value is not None)
                sheet.column_dimensions[get_column_letter(cells[0].column)].width = min(60, max(12, width + 4))
    with BytesIO() as stream:
        book.save(stream)
        return stream.getvalue()


def scenario_excel(result, snapshot):
    scenarios = result['scenarios']
    settings = result['settings']
    return excel_file({
        'المقارنة': (
            ('المؤشر', 'الوحدة', 'خط الأساس', *(row['name'] for row in scenarios)),
            [(metric['label'], metric['unit'], result['baseline'][key], *(row['kpis'][key] for row in scenarios))
             for key, metric in result['kpis'].items()],
        ),
        'الفرضيات': (
            ('السيناريو', 'الرقم', 'المدخل', 'نوع التغيير', 'القيمة', 'عدم اليقين ±'),
            [(row['name'], row['id'], item['node_id'],
              {'pct': 'تغيير %', 'add': 'فرق بالقيمة', 'set': 'قيمة جديدة'}[item['operation']],
              item['value'], item['uncertainty'])
             for row in scenarios for item in row['assumptions']],
        ),
        'الإعدادات': (
            ('النموذج', 'رقم الإصدار', 'الأفق بالأسابيع', 'عدد العينات', 'بذرة المحاكاة'),
            [(snapshot['name'], snapshot['version'], settings['weeks'], settings['n'], settings['seed'])],
        ),
    }, rtl=True)


def tornado_excel(result, snapshot):
    return excel_file({
        'الحساسية': (
            ('المدخل', 'المعرّف', 'القيمة المرجعية', 'القيمة عند الخفض', 'الأثر عند الخفض',
             'القيمة عند الزيادة', 'الأثر عند الزيادة', 'مدى التأثير', 'سبب تعذّر الخفض', 'سبب تعذّر الزيادة'),
            [(row['driver']['name_ar'], row['driver']['id'], row['driver']['value'],
              row['low']['value'], row['low']['delta'], row['high']['value'], row['high']['delta'],
              row['span'], row['low']['error'], row['high']['error']) for row in result['rows']],
        ),
        'الإعدادات': (
            ('النموذج', 'رقم الإصدار', 'المؤشر', 'الوحدة', 'خط الأساس', 'التغيير ±%', 'الأفق بالأسابيع'),
            [(snapshot['name'], snapshot['version'], result['metric']['label'], result['metric']['unit'],
              result['baseline'], result['percent'], result['weeks'])],
        ),
    }, rtl=True)


def p6_rows(activities, dataset):
    previous = {}
    predecessors = {}
    for row in sorted(dataset['activities'], key=lambda row: row['seq']):
        project, aid = row['project_id'], row['id']
        predecessors[aid] = previous.get(project, '')
        previous[project] = aid
    return [
        (aid, f'{project} - {phase}', project, start, finish, (last - first) * 7,
         predecessors[aid], resource)
        for aid, project, phase, first, last, start, finish, resource, _ in activities
    ]


def xml_fields(parent, **values):
    for key, value in values.items():
        ET.SubElement(parent, key).text = str(value)


def p6_xml(plan_id, dataset, rows):
    root = ET.Element('APIBusinessObjects')
    project = ET.SubElement(root, 'Project')
    xml_fields(project, ObjectId=1, Id=f'PLAN-{plan_id}', Name=f'خطة المقاولات {plan_id}')
    wbs_ids = {}
    for index, item in enumerate(dataset['projects'], 1):
        wbs_ids[item['id']] = index
        wbs = ET.SubElement(project, 'WBS')
        xml_fields(wbs, ObjectId=index, ProjectObjectId=1, Code=item['id'], Name=item['name_ar'])

    activity_ids = {row[0]: index for index, row in enumerate(rows, 1)}
    weekly_costs = {row['id']: row['cost_per_week'] for row in dataset['activities']}
    for aid, name, wbs, start, finish, days, _, _ in rows:
        activity = ET.SubElement(project, 'Activity')
        xml_fields(activity, ObjectId=activity_ids[aid], Id=aid, Name=name, ProjectObjectId=1,
                   WBSObjectId=wbs_ids[wbs], PlannedStartDate=start, PlannedFinishDate=finish,
                   PlannedDuration=days * 8, PlannedTotalCost=weekly_costs[aid] * (days // 7) * 1000)
    for aid, _, _, _, _, _, predecessor, _ in rows:
        if predecessor:
            relationship = ET.SubElement(project, 'Relationship')
            xml_fields(relationship, PredecessorActivityObjectId=activity_ids[predecessor],
                       SuccessorActivityObjectId=activity_ids[aid], Type='Finish to Start', Lag=0)
    return ET.tostring(root, encoding='utf-8', xml_declaration=True)


def render_export(fmt, plan, template, output, kpis, dataset, parameters):
    tables = (contracting_tables(output, kpis, parameters) if template == 'contracting'
              else production_tables(output, kpis, dataset, parameters))
    if fmt == 'xlsx':
        return excel_file(tables)
    if fmt in ('csv', 'json'):
        columns, primary_rows = next(iter(tables.values()))
        if fmt == 'csv':
            return csv_file(columns, primary_rows)
        return encode({
            'plan': plan, 'template': template, 'rows': [dict(zip(columns, row)) for row in primary_rows],
            'schedule': output, 'kpis': kpis,
        }).encode('utf-8')
    rows = p6_rows(tables['Activities'][1], dataset)
    if fmt == 'p6.csv':
        return csv_file(P6_COLUMNS, rows)
    return p6_xml(plan['id'], dataset, rows)
