"""Versioned configuration, approval snapshots and actuals for Phase 18."""
import json
from collections import defaultdict
from datetime import date
from math import isfinite
from statistics import mean
from typing import Literal

from pydantic import Field

from app.db import audit, encode, get_conn, utc_now
from app.enterprise import store
from app.enterprise.schema import EdgeRule, ModelInput, NodeId, StrictFields
from app.prescriptive.goal import Value
from app.prescriptive.outcomes import legacy_contract
from app.schemas import api_error

DDL = """
CREATE TABLE IF NOT EXISTS enterprise_edge_calibrations (
    model_id INTEGER NOT NULL,
    version INTEGER NOT NULL,
    report_json TEXT NOT NULL,
    PRIMARY KEY(model_id, version),
    FOREIGN KEY(model_id, version) REFERENCES enterprise_model_versions(model_id, version)
);
CREATE TABLE IF NOT EXISTS enterprise_kpi_thresholds (
    model_id INTEGER PRIMARY KEY REFERENCES enterprise_models(id),
    settings_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS enterprise_decisions (
    id INTEGER PRIMARY KEY,
    model_id INTEGER NOT NULL,
    version INTEGER NOT NULL,
    name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN ('draft', 'approved', 'rejected')),
    forecast_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    decided_at TEXT,
    decided_by INTEGER REFERENCES users(id),
    reviewer TEXT,
    comment TEXT,
    FOREIGN KEY(model_id, version) REFERENCES enterprise_model_versions(model_id, version)
);
CREATE INDEX IF NOT EXISTS enterprise_decisions_model ON enterprise_decisions(model_id);
CREATE TABLE IF NOT EXISTS enterprise_decision_actuals (
    id INTEGER PRIMARY KEY,
    decision_id INTEGER NOT NULL REFERENCES enterprise_decisions(id),
    observed_on TEXT NOT NULL,
    metrics_json TEXT NOT NULL,
    recorded_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS enterprise_actuals_decision ON enterprise_decision_actuals(decision_id, id);
"""


def initialize(path):
    with get_conn(path) as connection:
        connection.executescript(DDL)
        connection.execute('BEGIN IMMEDIATE')
        columns = {row['name'] for row in connection.execute('PRAGMA table_info(enterprise_decision_actuals)')}
        if 'closed' not in columns:
            connection.execute('ALTER TABLE enterprise_decision_actuals ADD COLUMN closed INTEGER NOT NULL DEFAULT 0')
        if 'evidence' not in columns:
            connection.execute('ALTER TABLE enterprise_decision_actuals ADD COLUMN evidence TEXT')
        if 'recorded_by' not in columns:
            connection.execute('ALTER TABLE enterprise_decision_actuals ADD COLUMN recorded_by TEXT')


class Decision(StrictFields):
    decision: Literal['approved', 'rejected']
    comment: str = Field(default='', max_length=2000)


class Actuals(StrictFields):
    observed_on: date
    metrics: dict[NodeId, Value] = Field(min_length=1)
    close: bool = Field(default=False, strict=True)
    evidence: str | None = Field(default=None, min_length=1, max_length=600)


def read_calibration(connection, model_id, version):
    row = connection.execute(
        'SELECT report_json FROM enterprise_edge_calibrations WHERE model_id = ? AND version = ?',
        (model_id, version),
    ).fetchone()
    return json.loads(row['report_json']) if row else None


def original_defaults(connection, model_id, graph, version):
    rows = connection.execute(
        '''SELECT report_json FROM enterprise_edge_calibrations
           WHERE model_id = ? AND version <= ? ORDER BY version DESC''', (model_id, version),
    )
    unmatched = {(edge.source, edge.target): edge.rule for edge in graph.edges}
    defaults = {}
    for stored in rows:
        for row in json.loads(stored['report_json']):
            pair = row['source'], row['target']
            if row['source_type'] != 'calibrated' or pair not in unmatched:
                continue
            rule = EdgeRule.model_validate(row['rule'])
            if unmatched[pair] == rule:
                default = row['default_rule']
                defaults[pair] = EdgeRule.model_validate(default) if default is not None else None
                del unmatched[pair]
    return defaults


def checked_version(connection, model_id, version):
    snapshot = store.read(connection, model_id)
    if snapshot['version'] != version:
        raise api_error(409, 'enterprise_version_conflict', 'الإصدار اتغيّر. حدّث البيانات وشغّل الحساب تاني.')
    return snapshot


def save_calibration(connection, model_id, version, graph, report):
    with connection:
        connection.execute('BEGIN IMMEDIATE')
        snapshot = checked_version(connection, model_id, version)
        changed = graph != snapshot['graph']
        latest = version + int(changed)
        if changed:
            store.write_version(connection, model_id, latest, ModelInput(name=snapshot['name'], graph=graph), origin='prescriptive_calibration')
        connection.execute(
            '''INSERT INTO enterprise_edge_calibrations VALUES (?, ?, ?)
               ON CONFLICT(model_id, version) DO UPDATE SET report_json = excluded.report_json''',
            (model_id, latest, encode(report)),
        )
        audit(connection, snapshot['company_id'], 'enterprise.calibration.apply', 'enterprise_model',
              model_id, {'version': latest})
    return latest


def create_draft(connection, model_id, version, name, forecast):
    with connection:
        connection.execute('BEGIN IMMEDIATE')
        snapshot = checked_version(connection, model_id, version)
        forecast['outcome_contract'] = legacy_contract(snapshot, forecast)
        return insert_draft(connection, snapshot, name, forecast)


def insert_draft(connection, snapshot, name, forecast):
    indicators = forecast['outcome_contract']['indicators']
    if not any(row['required'] for row in indicators.values()):
        raise api_error(422, 'decision_missing_expectation', 'القرار محتاج توقع قابل للقياس.')
    decision_id = connection.execute(
        '''INSERT INTO enterprise_decisions (model_id, version, name, forecast_json, created_at)
           VALUES (?, ?, ?, ?, ?)''',
        (snapshot['id'], snapshot['version'], name, encode(forecast), utc_now()),
    ).lastrowid
    audit(connection, snapshot['company_id'], 'enterprise.decision.create', 'enterprise_decision', decision_id,
          {'model_id': snapshot['id'], 'version': snapshot['version']})
    return decision_id


def outcome_contract(connection, snapshot, version, forecast):
    if 'outcome_contract' in forecast:
        return forecast['outcome_contract']
    return legacy_contract(store.read(connection, snapshot['id'], version), forecast)


def decide(connection, snapshot, decision_id, body, principal):
    with connection:
        connection.execute('BEGIN IMMEDIATE')
        plan = get_decision(connection, snapshot, decision_id)
        if plan['status'] != 'draft':
            raise api_error(409, 'decision_already_decided', 'المسودة دي اتراجعت بالفعل.')
        if body.decision == 'approved':
            checked_version(connection, snapshot['id'], plan['version'])
        connection.execute(
            '''UPDATE enterprise_decisions SET status = ?, decided_at = ?, decided_by = ?, reviewer = ?, comment = ?
               WHERE id = ?''', (body.decision, utc_now(), principal.id, principal.username, body.comment, decision_id),
        )
        audit(connection, snapshot['company_id'], 'enterprise.decision.' + body.decision,
              'enterprise_decision', decision_id, {'version': plan['version'], 'comment': body.comment})
    return body.decision


def get_decision(connection, snapshot, decision_id):
    row = connection.execute(
        'SELECT * FROM enterprise_decisions WHERE id = ? AND model_id = ?', (decision_id, snapshot['id']),
    ).fetchone()
    if row is None:
        raise api_error(404, 'enterprise_decision_not_found', 'القرار مش موجود للنموذج ده.')
    return row


def record_actuals(connection, snapshot, decision_id, body, principal):
    with connection:
        connection.execute('BEGIN IMMEDIATE')
        decision = get_decision(connection, snapshot, decision_id)
        if decision['status'] != 'approved':
            raise api_error(409, 'decision_not_approved', 'اعتمد القرار قبل تسجيل القيم الفعلية.')
        forecast = json.loads(decision['forecast_json'])
        indicators = outcome_contract(connection, snapshot, decision['version'], forecast)['indicators']
        if not body.metrics.keys() <= indicators.keys():
            raise api_error(422, 'missing_prediction', 'سجّل الفعلي للمؤشرات اللي لها توقع محفوظ.')
        for key, value in body.metrics.items():
            spec = indicators[key]
            if ((spec['node_id'] in ('risk.credit', 'risk.fill') and not 0 <= value <= 100)
                    or (spec['node_id'] == 'risk.delay' and value < 0)):
                raise api_error(422, 'actual_outside_range', 'القيمة الفعلية خارج نطاق المؤشر.')
            if not isfinite(value - spec['expected']):
                raise api_error(422, 'actual_outside_range', 'الفرق أكبر من نطاق الحساب المدعوم.')
        observed = body.observed_on.isoformat()
        if observed < decision['decided_at'][:10] or body.observed_on > date.today():
            raise api_error(422, 'invalid_observed_date', 'تاريخ القياس لازم يكون من يوم الاعتماد ولحد النهارده.')
        previous = latest_actuals(connection, decision_id)
        if previous and previous['closed']:
            raise api_error(409, 'decision_closed', 'القرار اتقفل بالقيم الفعلية.')
        metrics = body.metrics
        if previous and observed < previous['observed_on']:
            raise api_error(409, 'older_actuals', 'تاريخ القياس أقدم من آخر بيانات فعلية مسجلة للقرار.')
        if previous and observed == previous['observed_on']:
            metrics = previous['metrics'] | metrics
        if body.close and any(row['required'] and key not in metrics for key, row in indicators.items()):
            raise api_error(422, 'decision_missing_actuals', 'سجّل الفعلي لكل المؤشرات المطلوبة في نفس تاريخ القياس.')
        actuals_id = connection.execute(
            '''INSERT INTO enterprise_decision_actuals (decision_id, observed_on, metrics_json, recorded_at, closed, evidence, recorded_by)
               VALUES (?, ?, ?, ?, ?, ?, ?)''',
            (decision_id, observed, encode(metrics), utc_now(), body.close, body.evidence, principal.username),
        ).lastrowid
        audit(connection, snapshot['company_id'], 'enterprise.decision.close' if body.close else 'enterprise.decision.actuals',
              'enterprise_decision', decision_id,
              {'actuals_id': actuals_id, 'observed_on': observed})
    return actuals_id


def latest_actuals(connection, decision_id):
    row = connection.execute(
        '''SELECT observed_on, metrics_json, closed, evidence, recorded_by, recorded_at FROM enterprise_decision_actuals
           WHERE decision_id = ? ORDER BY id DESC LIMIT 1''', (decision_id,),
    ).fetchone()
    if not row:
        return None
    item = dict(row)
    item['metrics'] = json.loads(item.pop('metrics_json'))
    item['closed'] = bool(item['closed'])
    return item


def accuracy(predicted, actual):
    rows = []
    for key, value in actual.items():
        forecast = predicted.get(key)
        if forecast is None or value is None:
            continue
        error = value - forecast
        if not isfinite(error):
            continue
        ape = 100 * (abs(error) / abs(value)) if value else None
        rows.append({'kpi': key, 'predicted': forecast, 'actual': value, 'error': error,
                     'absolute_error': abs(error),
                     'absolute_percentage_error': ape if ape is not None and isfinite(ape) else None})
    return rows


def decision_log(connection, snapshot, *, pinned=False):
    decisions, scores = [], defaultdict(list)
    rows = connection.execute(
        'SELECT * FROM enterprise_decisions WHERE model_id = ? AND (? IS NULL OR version = ?) ORDER BY id DESC',
        (snapshot['id'], snapshot['version'] if pinned else None, snapshot['version']),
    )
    for row in rows:
        item = dict(row)
        item['forecast'] = json.loads(item.pop('forecast_json'))
        actuals = latest_actuals(connection, item['id'])
        report = accuracy(item['forecast']['kpis'], actuals['metrics']) if actuals else []
        contract = outcome_contract(connection, snapshot, item['version'], item['forecast'])
        state = ('closed' if actuals and actuals['closed'] else 'tracking' if actuals else 'awaiting_actuals'
                 if item['status'] == 'approved' else item['status'])
        item.update(actuals=actuals, accuracy=report, outcome_contract=contract, outcome_status=state)
        decisions.append(item)
        for result in report:
            scores[result['kpi']].append(result)
    summary = [{'kpi': key, 'count': len(rows), 'mae': mean(row['absolute_error'] for row in rows),
                'mape': mean(rates) if (rates := [row['absolute_percentage_error'] for row in rows
                                                  if row['absolute_percentage_error'] is not None]) else None}
               for key, rows in scores.items()]
    return {'decisions': decisions, 'accuracy': summary}


def approved_sector_log(connection, company_id):
    # Sector forecasts are already immutable run snapshots. Reuse the existing
    # actuals comparison, whose totals use only matching plan/actual rows.
    rows = connection.execute(
        '''SELECT p.id, p.actor, p.decided_at, p.variant, r.result_json, a.comparison_json,
                  a.created_at AS actuals_recorded_at
           FROM plans p JOIN runs r ON r.id = p.run_id AND r.company_id = p.company_id
           LEFT JOIN actuals a ON a.id = (
               SELECT MAX(id) FROM actuals WHERE plan_id = p.id AND company_id = p.company_id
           )
           WHERE p.company_id = ? AND p.status = 'approved' ORDER BY p.id DESC''', (company_id,),
    )
    results = []
    for row in rows:
        item = dict(row)
        item['predicted'] = json.loads(item.pop('result_json'))[item['variant']]['kpis']
        comparison = item.pop('comparison_json')
        item['accuracy'] = []
        if comparison:
            report = json.loads(comparison)
            item['observed_rows'] = report['kpis_actual']['observed_rows']
            item['accuracy'] = accuracy(report['kpis_plan'],
                                        {key: value for key, value in report['kpis_actual'].items()
                                         if key != 'observed_rows'})
        results.append(item)
    return results
