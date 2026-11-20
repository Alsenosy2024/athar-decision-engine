"""Versioned configuration, approval snapshots and actuals for Phase 18."""
import json
from collections import defaultdict
from datetime import date
from math import isfinite
from statistics import mean
from typing import Literal

from pydantic import Field, model_validator

from app.db import audit, encode, get_conn, utc_now
from app.enterprise import store
from app.enterprise.scenarios import Kpi
from app.enterprise.schema import EdgeRule, ModelInput, StrictFields
from app.prescriptive.goal import Value
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


class Decision(StrictFields):
    decision: Literal['approved', 'rejected']
    comment: str = Field(default='', max_length=2000)


class Actuals(StrictFields):
    observed_on: date
    metrics: dict[Kpi, Value] = Field(min_length=1)

    @model_validator(mode='after')
    def kpi_ranges(self):
        rates = ('credit_breach', 'fill_rate')
        if any(not 0 <= self.metrics[key] <= 100 for key in rates if key in self.metrics):
            raise ValueError('Rates must be 0–100')
        if self.metrics.get('delays', 0) < 0:
            raise ValueError('Delay cannot be negative')
        return self


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
            store.write_version(connection, model_id, latest, ModelInput(name=snapshot['name'], graph=graph))
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
        decision_id = connection.execute(
            '''INSERT INTO enterprise_decisions (model_id, version, name, forecast_json, created_at)
               VALUES (?, ?, ?, ?, ?)''', (model_id, version, name, encode(forecast), utc_now()),
        ).lastrowid
        audit(connection, snapshot['company_id'], 'enterprise.decision.create', 'enterprise_decision', decision_id,
              {'model_id': model_id, 'version': version})
        return decision_id


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


def record_actuals(connection, snapshot, decision_id, body):
    with connection:
        connection.execute('BEGIN IMMEDIATE')
        decision = get_decision(connection, snapshot, decision_id)
        if decision['status'] != 'approved':
            raise api_error(409, 'decision_not_approved', 'اعتمد القرار قبل تسجيل القيم الفعلية.')
        predicted = json.loads(decision['forecast_json'])['kpis']
        if any(predicted.get(key) is None for key in body.metrics):
            raise api_error(422, 'missing_prediction', 'سجّل الفعلي للمؤشرات اللي لها توقع محفوظ.')
        observed = body.observed_on.isoformat()
        if observed < decision['decided_at'][:10] or body.observed_on > date.today():
            raise api_error(422, 'invalid_observed_date', 'تاريخ القياس لازم يكون من يوم الاعتماد ولحد النهارده.')
        previous = latest_actuals(connection, decision_id)
        metrics = body.metrics
        if previous and observed < previous['observed_on']:
            raise api_error(409, 'older_actuals', 'تاريخ القياس أقدم من آخر بيانات فعلية مسجلة للقرار.')
        if previous and observed == previous['observed_on']:
            metrics = previous['metrics'] | metrics
        actuals_id = connection.execute(
            '''INSERT INTO enterprise_decision_actuals (decision_id, observed_on, metrics_json, recorded_at)
               VALUES (?, ?, ?, ?)''', (decision_id, observed, encode(metrics), utc_now()),
        ).lastrowid
        audit(connection, snapshot['company_id'], 'enterprise.decision.actuals', 'enterprise_decision', decision_id,
              {'actuals_id': actuals_id, 'observed_on': observed})
    return actuals_id


def latest_actuals(connection, decision_id):
    row = connection.execute(
        '''SELECT observed_on, metrics_json FROM enterprise_decision_actuals
           WHERE decision_id = ? ORDER BY id DESC LIMIT 1''', (decision_id,),
    ).fetchone()
    return {'observed_on': row['observed_on'], 'metrics': json.loads(row['metrics_json'])} if row else None


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


def decision_log(connection, snapshot):
    decisions, scores = [], defaultdict(list)
    rows = connection.execute(
        'SELECT * FROM enterprise_decisions WHERE model_id = ? ORDER BY id DESC', (snapshot['id'],),
    )
    for row in rows:
        item = dict(row)
        item['forecast'] = json.loads(item.pop('forecast_json'))
        actuals = latest_actuals(connection, item['id'])
        report = accuracy(item['forecast']['kpis'], actuals['metrics']) if actuals else []
        item.update(actuals=actuals, accuracy=report)
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
