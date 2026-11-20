"""Read-only dashboard projections from saved, company-scoped evidence."""

import json
from math import isfinite

from fastapi import APIRouter, Depends, Response

from app.db import request_conn
from app.enterprise import store
from app.enterprise.schema import Series
from app.intelligence.reviews import observed_history
from app.prescriptive.records import outcome_contract
from app.routers.companies import get_company

router = APIRouter(prefix='/ui', tags=['ui'])

SECTOR_METRICS = {
    'production': [('total_cost', 'إجمالي التكلفة', 'ريال', 'operations'),
                   ('late_orders', 'الطلبات المتأخرة', 'طلب', 'sales'),
                   ('peak_od', 'ذروة العجز النقدي', 'ريال', 'finance'),
                   ('peak_storage_pct', 'ذروة إشغال التخزين', '%', 'inventory')],
    'contracting': [('total', 'إجمالي التكلفة', 'ألف ريال', 'projects'),
                    ('late_weeks_total', 'إجمالي التأخير', 'أسبوع', 'projects'),
                    ('peak_od', 'ذروة العجز النقدي', 'ألف ريال', 'finance'),
                    ('ld', 'غرامات التأخير', 'ألف ريال', 'results')],
}
ENTERPRISE_METRICS = [
    ('pnl.result', 'صافي الربح', 'ريال'), ('pnl.revenue', 'الإيرادات', 'ريال'),
    ('cash', 'الرصيد النقدي', 'ريال'), ('risk.credit', 'مخاطر الائتمان', '%'),
]


def stored_runs(connection, cid, dataset_id, kind, limit=1, base_run_id=None):
    return list(connection.execute(
        '''SELECT * FROM runs WHERE company_id = ? AND dataset_id = ? AND kind = ?
           AND (? IS NULL OR base_run_id = ?) ORDER BY id DESC LIMIT ?''',
        (cid, dataset_id, kind, base_run_id, base_run_id, limit),
    ))


def sector_kpis(template, runs):
    comparable = [row for row in runs if row['model_id'] == runs[0]['model_id']] if runs else []
    history = [json.loads(row['result_json']) for row in reversed(comparable)]
    current = history[-1] if history else {}
    result = []
    for key, name, unit, focus in SECTOR_METRICS[template]:
        points = [(row['created_at'][:10], item.get('optimized', {}).get('kpis', {}).get(key))
                  for row, item in zip(reversed(comparable), history)]
        points = [(date, value) for date, value in points if value is not None]
        has_history = len(points) > 1
        before = points[-2][1] if has_history else current.get('baseline', {}).get('kpis', {}).get(key)
        value = current.get('optimized', {}).get('kpis', {}).get(key)
        if value is not None and before is not None and len(points) == 1:
            points.insert(0, ('مرجعي', before))
        result.append(dict(name=name, unit=unit, value=value, before=before, focus=focus,
                           lower_better=True, points=points,
                           source='تشغيلات التحسين المحفوظة' if has_history else 'الخطة المحسّنة مقابل المرجع',
                           period=comparable[0]['created_at'][:10] if comparable else None))
    return result


def enterprise_kpis(connection, snapshot):
    graph = observed_history(connection, snapshot['id'], snapshot['graph'], empty_as_missing=True)[0] if snapshot else None
    nodes = {node.id: node for node in graph.nodes} if graph else {}
    result = []
    for key, name, default_unit in ENTERPRISE_METRICS:
        node = nodes.get(key)
        points = [(point.date.isoformat(), point.value) for point in node.baseline.points[-12:]] if (
            node and isinstance(node.baseline, Series)) else []
        result.append(dict(name=node.name_ar if node else name, unit=node.unit if node else default_unit,
                           value=points[-1][1] if points else None, before=points[-2][1] if len(points) > 1 else None,
                           lower_better=key == 'risk.credit', focus=key, points=points,
                           source='القراءات المرصودة في النموذج المؤسسي', period=points[-1][0] if points else None))
    return result


def sector_forecast(connection, company):
    row = connection.execute(
        '''SELECT * FROM forecasts WHERE company_id = ? AND dataset_id = ? ORDER BY id DESC LIMIT 1''',
        (company['id'], company['active_dataset_id']),
    ).fetchone()
    if not row:
        return None
    result = json.loads(row['result_json'])
    series = []
    for item in result['series']:
        weeks = item.get('history_weeks') or []
        labels = [f'أسبوع {weeks[-1] + i + 1}' if weeks else f'أفق {i + 1}' for i in range(len(item['p50']))]
        series.append(dict(key=item['key'], name=item['name_ar'], labels=labels,
                           **{key: item[key] for key in ('p10', 'p50', 'p90')}))
    production = company['template'] == 'production'
    return dict(source=f"توقع القطاع #{row['id']}", period=row['created_at'][:10], series=series,
                unit='وحدة' if production else 'أسبوع', lower_better=not production)


def lab_forecast(connection, cid, snapshot):
    if not snapshot:
        return None
    rows = connection.execute(
        """SELECT id, payload_json, created_at FROM ai_reviews
           WHERE company_id = ? AND model_id = ? AND model_version = ?
           AND kind = 'forecast' AND status != 'rejected' ORDER BY id DESC LIMIT 1""",
        (cid, snapshot['id'], snapshot['version']),
    )
    for row in rows:
        item = json.loads(row['payload_json'])
        points = item['points']
        return dict(source=f"توقع المختبر #{row['id']}", period=row['created_at'][:10], unit=item['unit'],
                    lower_better=item['node_id'].startswith(('risk.', 'cost.')),
                    series=[dict(key=item['node_id'], name=item['name_ar'], labels=[p['date'] for p in points],
                                 **{key: [point[key] for point in points] for key in ('p10', 'p50', 'p90')})])
    return None


def simulation(row, template, optimization):
    if not row:
        return None
    result = json.loads(row['result_json'])
    contracting = template == 'contracting'
    metric = result['late_weeks' if contracting else 'late_orders']
    bins = metric['hist']
    # The distribution is discrete. Include only the fraction of the boundary
    # bin needed to complete the worst 5% of draws.
    remaining = mass = sum(item['count'] for item in bins) * .05
    weighted = 0
    cutoff = None
    for item in reversed(bins):
        count = min(remaining, item['count'])
        weighted += item['bin'] * count
        remaining -= count
        cutoff = item['bin']
        if remaining <= 0:
            break
    if not mass:
        return None
    curve = json.loads(optimization['result_json'])['optimized']['kpis'].get('cash_curve', [])
    period = f"{row['created_at'][:10]} · أفق الخطة {len(curve)} {'أسبوع' if contracting else 'يوم'}" if curve else row['created_at'][:10]
    return dict(bins=bins, cutoff=cutoff, p50=metric['p50'], cvar=weighted / mass,
                source=f"محاكاة #{row['id']} · خطة #{row['base_run_id']}", period=period,
                n=result['n'], unit='أسبوع تأخير' if contracting else 'طلب متأخر')


def sector_actuals(connection, cid, template):
    row = connection.execute(
        'SELECT * FROM actuals WHERE company_id = ? ORDER BY id DESC LIMIT 1', (cid,),
    ).fetchone()
    if not row:
        return None
    compare = json.loads(row['comparison_json'])
    production = template == 'production'
    metric = compare['chart']['metric']
    observed = [item['actual'] for item in compare['rows'] if item['actual'][metric] is not None]
    if not observed:
        return None
    start = min(item['ship_day']
                for item in observed) if production else min(item['start_week'] for item in observed)
    end = max(item['ship_day'] for item in observed) if production else max(item['finish_week'] for item in observed)
    return dict(actual=compare['kpis_actual'][metric], plan=compare['kpis_plan'][metric],
                unit='وحدة مشحونة' if production else 'أسبوع عمل تراكمي',
                period=f"{'أيام' if production else 'أسابيع'} الخطة {start}–{end}",
                source=f"رفع الفعلي #{row['id']} · خطة #{row['plan_id']}", count=len(observed))


def enterprise_actuals(connection, models):
    ids = [model['id'] for model in models]
    if not ids:
        return None
    marks = ','.join('?' for _ in ids)
    model_by_id = {model['id']: model for model in models}
    for row in connection.execute(
        f'''SELECT a.*, d.model_id, d.version, d.name, d.forecast_json
            FROM enterprise_decision_actuals a JOIN enterprise_decisions d ON d.id = a.decision_id
            WHERE d.model_id IN ({marks}) AND d.status = 'approved'
              AND a.id = (SELECT MAX(id) FROM enterprise_decision_actuals WHERE decision_id = d.id)
            ORDER BY a.observed_on DESC, a.id DESC''', ids,
    ):
        forecast = json.loads(row['forecast_json'])
        contract = outcome_contract(connection, model_by_id[row['model_id']], row['version'], forecast)
        measured = json.loads(row['metrics_json'])
        indicators = sorted(contract['indicators'].items(), key=lambda item: not item[1]['required'])
        for key, spec in indicators:
            if key in measured:
                return dict(actual=measured[key], plan=spec['expected'], unit=spec['unit'], name=spec['label'],
                            period=f"{row['observed_on']} · أفق التوقع {spec['weeks']} أسبوع",
                            source=f"{row['name']} · قرار #{row['decision_id']} · إصدار {row['version']}", count=1)
    return None


def decision_state(status):
    return {'draft': 'pending', 'approved': 'approved', 'rejected': 'rejected'}.get(status, status)


def decision_cards(connection, cid, template, models):
    cards = []
    for model in models:
        for row in connection.execute(
            'SELECT * FROM enterprise_decisions WHERE model_id = ? ORDER BY id DESC LIMIT 3', (model['id'],),
        ):
            forecast = json.loads(row['forecast_json'])
            contract = outcome_contract(connection, model, row['version'], forecast)
            monetary = [(key, spec) for key, spec in contract['indicators'].items() if spec['unit'] == 'SAR']
            key, spec = next(((key, spec) for key, spec in monetary if spec['required']),
                             next(iter(monetary), (None, {})))
            before = (forecast.get('baseline') or {}).get(key)
            value = spec.get('expected')
            assumptions = [item['node_id'] for item in forecast.get('assumptions', [])]
            cards.append(dict(id=f"enterprise-{row['id']}", name=row['name'], status=decision_state(row['status']),
                              impact=value - before if value is not None and before is not None else None,
                              impact_label=spec.get('label'), before=before, value=value,
                              confidence=forecast.get('confidence'), focus=assumptions + ([spec['node_id']] if spec else []),
                              created=row['created_at'], href='#/prescriptive',
                              model_id=model['id'], version=row['version'], note=row['comment'],
                              source=f"{model['name']} · إصدار {row['version']}"))
    for row in connection.execute(
        '''SELECT p.*, r.result_json,
                  (SELECT c.scenario_json FROM chat_messages c
                   WHERE c.company_id = p.company_id AND c.run_id = p.run_id AND c.role = 'assistant'
                   ORDER BY c.id DESC LIMIT 1) AS chat_scenario
           FROM plans p JOIN runs r ON r.id = p.run_id AND r.company_id = p.company_id
           WHERE p.company_id = ? ORDER BY p.id DESC LIMIT 3''', (cid,),
    ):
        key = 'total_cost' if template == 'production' else 'total'
        scale = 1 if template == 'production' else 1000
        run = json.loads(row['result_json'])
        value = run[row['variant']]['kpis'].get(key)
        before = run.get('baseline', run.get('base', {})).get('kpis', {}).get(key)
        cards.append(dict(id=f"sector-{row['id']}", name=f"خطة التشغيل {row['id']}",
                          status=decision_state(row['status']), impact=(before - value) * scale
                          if value is not None and before is not None else None,
                          confidence=json.loads(row['chat_scenario'] or '{}').get('confidence'),
                          impact_label='وفر التكلفة', before=before * scale if before is not None else None,
                          value=value * scale if value is not None else None,
                          focus=['operations' if template == 'production' else 'projects'], created=row['created_at'],
                          note=row['comment'] or run.get('explanation_ar'), href='#/approvals',
                          source=f"تشغيل #{row['run_id']}"))
    for row in connection.execute(
        """SELECT id, result_json, created_at FROM runs
           WHERE company_id = ? AND kind IN ('optimize', 'whatif') AND status = 'INFEASIBLE'
           ORDER BY id DESC LIMIT 3""", (cid,),
    ):
        result = json.loads(row['result_json'])
        cards.append(dict(id=f"infeasible-{row['id']}", name=f"بديل التشغيل {row['id']}", status='infeasible',
                          impact=None, confidence=None, focus=['projects' if template == 'contracting' else 'operations'],
                          created=row['created_at'], note=result.get('explanation_ar'),
                          href='#/whatif' if 'scenario' in result else '#/optimize', source=f"تشغيل #{row['id']}"))
    return sorted(cards, key=lambda row: (
        row['impact'] is not None and isfinite(row['impact']), abs(row['impact'] or 0), row['created'],
    ), reverse=True)[:3]


@router.get('/companies/{cid}/now')
def company_now(cid: int, response: Response, connection=Depends(request_conn)):
    connection.execute('BEGIN')
    company = get_company(connection, cid, allow_enterprise=True)
    models = [item for item in store.list_models(connection) if item['company_id'] == cid]
    onboarding = connection.execute('SELECT model_id FROM onboarding_companies WHERE company_id = ?', (cid,)).fetchone()
    model_ids = {model['id'] for model in models}
    model_id = onboarding['model_id'] if onboarding and onboarding['model_id'] in model_ids else (
        models[-1]['id'] if models else None)
    snapshot = store.read(connection, model_id) if model_id is not None else None
    template = company['template']
    dataset = company['active_dataset_id']
    runs = stored_runs(connection, cid, dataset, 'optimize', 12) if template in SECTOR_METRICS else []
    monte = stored_runs(connection, cid, dataset, 'montecarlo', base_run_id=runs[0]['id']) if runs else []
    response.headers['Cache-Control'] = 'no-store'
    return dict(
        kpis=sector_kpis(template, runs) if runs else enterprise_kpis(connection, snapshot) if snapshot
        else sector_kpis(template, []) if template in SECTOR_METRICS else enterprise_kpis(connection, None),
        forecast=(sector_forecast(connection, company) or lab_forecast(connection, cid, snapshot)),
        simulation=simulation(monte[0], template, runs[0]) if monte else None,
        actuals=sector_actuals(connection, cid, template) or enterprise_actuals(connection, models),
        decisions=decision_cards(connection, cid, template, models),
    )
