import json
from collections import defaultdict
from math import log
from statistics import mean, stdev

from app.db import audit, encode, utc_now


def observed_runs(connection, cid, key, observed_metric):
    # Keep the latest observation for each run and item, even across repeat uploads.
    runs = defaultdict(dict)
    rows = connection.execute(
        """SELECT a.comparison_json, p.run_id FROM actuals a
           JOIN plans p ON p.id = a.plan_id AND p.company_id = a.company_id
           WHERE a.company_id = ? ORDER BY a.id""", (cid,),
    )
    for upload in rows:
        for row in json.loads(upload['comparison_json'])['rows']:
            if row['actual'][observed_metric] is not None:
                runs[upload['run_id']][row[key]] = row
    return runs


def duration_calibrations(connection, cid, dataset_id):
    runs = observed_runs(connection, cid, 'activity_id', 'duration_weeks')
    ratios = defaultdict(list)
    for activities in runs.values():
        for activity in activities.values():
            planned = activity['plan']['duration_weeks']
            actual = activity['actual']['duration_weeks']
            ratios[activity['phase']].append(log(actual / planned))
    if not ratios:
        return {}

    dataset = connection.execute('SELECT payload_json FROM datasets WHERE id = ? AND company_id = ?',
                                 (dataset_id, cid)).fetchone()
    if dataset:
        for row in json.loads(dataset['payload_json']).get('history', []):
            if row['phase'] in ratios:
                ratios[row['phase']].append(log(row['actual_weeks']) - log(row['planned_weeks']))
    return {
        f'dur_ratio.{phase}': ({'mu': mean(values), 'sigma': stdev(values) if len(values) > 1 else 0.15,
                              'n': len(values)}, len(values))
        for phase, values in ratios.items()
    }


def demand_calibrations(connection, cid):
    runs = observed_runs(connection, cid, 'order_id', 'qty')
    biases, counts = {}, defaultdict(int)
    for orders in runs.values():
        totals = defaultdict(lambda: [0, 0, 0])
        for order in orders.values():
            sku = order['sku_id']
            # The order is the planned demand, including orders the optimizer left unshipped.
            totals[sku][0] += order['demand_qty']
            totals[sku][1] += order['actual']['qty']
            totals[sku][2] += 1
        for sku, (planned, actual, n) in totals.items():
            if planned:
                ratio = max(0.5, min(2.0, actual / planned))
                biases[sku] = 0.7 * biases.get(sku, 1.0) + 0.3 * ratio
                counts[sku] += n
    return {f'demand_bias.{sku}': (value, counts[sku]) for sku, value in biases.items()}


def list_calibrations(connection, cid):
    return [
        {'key': row['key'], 'value': json.loads(row['value_json']), 'updated_at': row['updated_at']}
        for row in connection.execute(
            'SELECT key, value_json, updated_at FROM calibrations WHERE company_id = ? ORDER BY key', (cid,),
        )
    ]


def recalibrate(connection, company):
    cid = company['id']
    before = {row['key']: row['value'] for row in list_calibrations(connection, cid)}
    if company['template'] == 'contracting':
        values = duration_calibrations(connection, cid, company['active_dataset_id'])
    else:
        values = demand_calibrations(connection, cid)

    now = utc_now()
    results = []
    for key, (value, n) in sorted(values.items()):
        previous = before.get(key)
        results.append({'key': key, 'before': previous, 'after': value, 'n': n})
        if previous != value:
            calibration_id = connection.execute(
                """INSERT INTO calibrations (company_id, key, value_json, updated_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(company_id, key) DO UPDATE
                   SET value_json = excluded.value_json, updated_at = excluded.updated_at
                   RETURNING id""",
                (cid, key, encode(value), now),
            ).fetchone()['id']
            audit(connection, cid, 'calibration.update', 'calibration', calibration_id, results[-1])
    if not results:
        message = 'لا توجد بيانات فعلية كافية للمعايرة.'
    elif any(row['before'] != row['after'] for row in results):
        message = 'تم تحديث معاملات التوقع من البيانات الفعلية.'
    else:
        message = 'المعاملات الحالية تعكس البيانات الفعلية المحفوظة.'
    return {'calibrations': results, 'msg_ar': message}
