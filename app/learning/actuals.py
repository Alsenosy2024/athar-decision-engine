import csv
import json
from io import StringIO
from math import isfinite

from app.db import audit, encode, utc_now
from app.ingestion.sample_templates import ACTUALS
from app.schemas import api_error

MAX_UPLOAD_BYTES = 2 * 1024 * 1024


def invalid_row(number, message):
    return api_error(422, 'invalid_actuals_row', f'السطر {number}: {message}')


def nonnegative_int(value):
    number = int(value)
    if not 0 <= number <= 2**53 - 1:
        raise ValueError
    return number


def parse_actuals(template, content, known_ids):
    columns = ACTUALS[template][0]
    try:
        reader = csv.reader(StringIO(content.decode('utf-8-sig'), newline=''), strict=True)
        headers = [value.strip() for value in next(reader, [])]
        if len(headers) != len(columns) or set(headers) != set(columns):
            raise api_error(422, 'invalid_actuals_header',
                            f'استخدم أعمدة قالب الفعلي: {", ".join(columns)}.')

        records = {}
        for values in reader:
            if not any(value.strip() for value in values):
                continue
            if len(values) != len(columns):
                raise invalid_row(reader.line_num, 'عدد الخانات لا يطابق العناوين.')
            row = dict(zip(headers, (value.strip() for value in values)))
            key = row[columns[0]]
            if key not in known_ids or key in records:
                raise invalid_row(reader.line_num, 'المعرّف غير موجود في بيانات الخطة أو مكرر في الملف.')
            try:
                for field in columns[1:]:
                    if field == 'actual_cost':
                        cost = float(row[field])
                        if not isfinite(cost) or not 0 <= cost <= 2**53 - 1:
                            raise ValueError
                        row[field] = cost
                    else:
                        row[field] = nonnegative_int(row[field])
            except ValueError:
                raise invalid_row(reader.line_num, 'أدخل قيمًا رقمية غير سالبة بالصيغة المطلوبة.') from None
            if template == 'contracting' and row['actual_finish_week'] <= row['actual_start_week']:
                raise invalid_row(reader.line_num, 'أسبوع النهاية لازم يكون بعد أسبوع البداية.')
            records[key] = row
    except (UnicodeError, csv.Error):
        raise api_error(422, 'invalid_actuals_csv', 'تعذّر قراءة ملف CSV بترميز UTF-8.') from None
    if not records:
        raise api_error(422, 'empty_actuals', 'الملف لا يحتوي على بيانات فعلية.')
    return records


def deltas(planned, actual):
    return {key: None if actual[key] is None or value is None else round(actual[key] - value, 6)
            for key, value in planned.items()}


def contracting_rows(plan, specs, records):
    for activity in plan['activities']:
        key = activity['activity_id']
        planned_duration = activity['finish_week'] - activity['start_week']
        observation = records.get(key, {})
        start, finish = observation.get('actual_start_week'), observation.get('actual_finish_week')
        planned = {'start_week': activity['start_week'], 'finish_week': activity['finish_week'],
                   'duration_weeks': planned_duration, 'cost': planned_duration * specs[key]['cost_per_week']}
        actual = {'start_week': start, 'finish_week': finish,
                  'duration_weeks': finish - start if finish is not None else None,
                  'cost': observation.get('actual_cost')}
        yield {'activity_id': key, 'project': activity['project'], 'phase': activity['phase'],
               'plan': planned, 'actual': actual, 'variance': deltas(planned, actual)}


def production_rows(plan, specs, records):
    shipments = {row['order']: row for row in plan['shipments']}
    # Include actual shipments for orders the plan left unshipped.
    for key in dict.fromkeys([*shipments, *records]):
        order = specs[key]
        shipment, observation = shipments.get(key), records.get(key, {})
        day, qty = observation.get('actual_ship_day'), observation.get('actual_qty')
        planned = {'ship_day': shipment['day'] if shipment else None,
                   'qty': order['qty'] if shipment else 0,
                   'late_days': shipment['late_days'] if shipment else None}
        actual = {'ship_day': day, 'qty': qty,
                  'late_days': max(0, day - order['due_day']) if qty else None}
        yield {'order_id': key, 'sku_id': order['sku_id'], 'demand_qty': order['qty'],
               'plan': planned, 'actual': actual, 'variance': deltas(planned, actual)}


def compare(template, plan, specs, records):
    if template == 'contracting':
        rows = list(contracting_rows(plan, specs, records))
        key, metrics = 'activity_id', ('duration_weeks', 'cost')
    else:
        rows = list(production_rows(plan, specs, records))
        key, metrics = 'order_id', ('qty', 'late_days')
    # Compare totals across the same scope when an upload only covers part of the plan.
    matched = [row for row in rows if row[key] in records]
    totals = {
        side: {'observed_rows': len(matched), **{
            metric: round(sum(row[side][metric] or 0 for row in matched), 6) for metric in metrics
        }}
        for side in ('plan', 'actual')
    }
    metric = metrics[0]
    return {'rows': rows, 'kpis_plan': totals['plan'], 'kpis_actual': totals['actual'],
            'variance': deltas(totals['plan'], totals['actual']),
            'chart': {'metric': metric, 'labels': [row[key] for row in rows],
                      'plan': [row['plan'][metric] for row in rows],
                      'actual': [row['actual'][metric] for row in rows]}}


def save_actuals(connection, plan, template, schedule, dataset, content):
    entity = 'activities' if template == 'contracting' else 'orders'
    specs = {row['id']: row for row in dataset[entity]}
    records = parse_actuals(template, content, specs)
    comparison = compare(template, schedule, specs, records)
    cid, pid = plan['company_id'], plan['id']
    with connection:
        aid = connection.execute(
            """INSERT INTO actuals (company_id, plan_id, payload_json, comparison_json, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (cid, pid, encode(list(records.values())), encode(comparison), utc_now()),
        ).lastrowid
        audit(connection, cid, 'actuals.upload', 'actuals', aid, {'plan_id': pid, 'rows': len(records)})
    return {'actuals_id': aid, 'comparison': comparison}


def latest_actuals(connection, cid, *, allow_missing=False):
    row = connection.execute(
        'SELECT id, comparison_json FROM actuals WHERE company_id = ? ORDER BY id DESC LIMIT 1', (cid,),
    ).fetchone()
    if row is None:
        if allow_missing:
            return None
        raise api_error(404, 'actuals_not_found', 'لا توجد بيانات فعلية محفوظة للشركة.')
    return {'actuals_id': row['id'], 'comparison': json.loads(row['comparison_json'])}
