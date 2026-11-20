"""Read company-scoped evidence and construct or verify a pinned decision run."""
from dataclasses import asdict
import json

from app.db import utc_now
from app.decision_loop.contract import (
    SCHEMA_VERSION, ContractError, fingerprint, freeze_contract, model_fingerprint, solver_settings,
)
from app.decision_loop.warm_start import PreviousPlan


def optimization_snapshot(connection, cid, run_id):
    row = connection.execute(
        """SELECT r.*, d.payload_json, d.validation_json, m.model_json, m.version
           FROM runs r
           JOIN datasets d ON d.id = r.dataset_id AND d.company_id = r.company_id
           JOIN company_models m ON m.id = r.model_id AND m.company_id = r.company_id
           WHERE r.id = ? AND r.company_id = ? AND r.kind = 'optimize'""",
        (run_id, cid),
    ).fetchone()
    if row is None:
        raise ContractError('run_not_found', 'تشغيل التحسين غير موجود للشركة.')
    return row


def previous_plan(connection, cid, run_id):
    if run_id is None:
        return None
    row = optimization_snapshot(connection, cid, run_id)
    optimized = json.loads(row['result_json'])['optimized']
    kpis = optimized['kpis']
    plan = optimized['plan'] if (
        row['status'] in ('FEASIBLE', 'OPTIMAL') and kpis.get('credit_ok') and kpis.get('feasible', True)
    ) else {}
    return PreviousPlan(plan, json.loads(row['payload_json']), json.loads(row['model_json'])['parameters'])


def decision_contract(connection, company, dataset_id, payload, dataset_created_at, model, weights,
                      forecast_id, time_limit, previous_run_id, previous, risk_policy=None):
    row = connection.execute(
        'SELECT * FROM forecasts WHERE id = ? AND company_id = ?', (forecast_id, company['id']),
    ).fetchone()
    if row is None:
        raise ContractError('forecast_not_found', 'التوقع غير موجود للشركة.')
    if row['dataset_id'] != dataset_id:
        raise ContractError('forecast_dataset_mismatch', 'التوقع مربوط بمجموعة بيانات مختلفة.')
    return freeze_contract(
        company=company, dataset_id=dataset_id, dataset=payload, dataset_created_at=dataset_created_at,
        model=model, weights=weights, forecast_id=forecast_id, forecast=json.loads(row['result_json']),
        forecast_created_at=row['created_at'], cutoff_at=utc_now(), time_limit=time_limit,
        previous_run_id=previous_run_id, previous=asdict(previous) if previous is not None else None,
        risk_policy=risk_policy,
    )


def replay_inputs(connection, company, run_id):
    row = optimization_snapshot(connection, company['id'], run_id)
    original = json.loads(row['result_json'])
    contract = original.get('decision_contract')
    if not contract or contract['version'] != SCHEMA_VERSION:
        raise ContractError('replay_unavailable', 'التشغيل محفوظ من غير إصدار مدعوم لعقد القرار.')
    payload = json.loads(row['payload_json'])
    model = json.loads(row['model_json']) | {'id': row['model_id'], 'version': row['version']}
    previous = previous_plan(connection, company['id'], contract['previous_run_id'])
    previous_hash = fingerprint(asdict(previous)) if previous is not None else None
    time_limit = contract['solver']['wall_limit_s']
    input_hash = fingerprint({key: value for key, value in contract.items() if key != 'input_fingerprint'})
    if (input_hash != contract['input_fingerprint']
            or contract['company_id'] != company['id'] or contract['template'] != company['template']
            or contract['dataset_id'] != row['dataset_id'] or contract['dataset_fingerprint'] != fingerprint(payload)
            or contract['model_id'] != model['id'] or contract['model_fingerprint'] != model_fingerprint(model)
            or contract['previous_plan_fingerprint'] != previous_hash
            or contract['solver'] != solver_settings(time_limit)):
        raise ContractError('replay_inputs_changed', 'مدخلات التشغيل أو إصدار السولفر اتغيروا.')
    return row, payload, model, contract, previous, original
