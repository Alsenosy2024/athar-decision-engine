"""Recheck persisted runs, including runs created before solution records."""
import json

from pydantic import TypeAdapter

from app.decision_loop.contract import contracting_dataset
from app.decision_loop.eligibility import approved_inputs, assess, constraint_policy
from app.llm.schema import Change
from app.schemas import api_error
from app.simulation.scenario import apply_changes

CHANGES = TypeAdapter(list[Change])
RUN_INPUTS = """
    SELECT r.*, c.template, d.payload_json, d.validation_json, m.model_json, m.version,
           base.result_json AS base_result_json
    FROM runs r
    JOIN companies c ON c.id = r.company_id
    JOIN datasets d ON d.id = r.dataset_id AND d.company_id = r.company_id
    JOIN company_models m ON m.id = r.model_id AND m.company_id = r.company_id
    LEFT JOIN runs base ON base.id = r.base_run_id AND base.company_id = r.company_id
    WHERE r.id = ? AND r.company_id = ? AND r.kind IN ('optimize', 'whatif')
"""


def effective_inputs(row):
    dataset = json.loads(row['payload_json'])
    model = json.loads(row['model_json'])
    original = json.loads(row['base_result_json'] if row['kind'] == 'whatif' else row['result_json'])
    model['weights'] = original.get('effective_weights', model['weights'])
    if row['kind'] == 'whatif':
        changes = CHANGES.validate_python(json.loads(row['scenario_json'])['changes'])
        dataset, model, _ = apply_changes(dataset, model, changes, template_id=row['template'])
    if row['template'] == 'contracting':
        dataset = contracting_dataset(dataset, original.get('decision_contract'))
    return dataset, model | {'id': row['model_id'], 'version': row['version']}


def read_record(connection, cid, run_id):
    row = connection.execute(RUN_INPUTS, (run_id, cid)).fetchone()
    if row is None:
        raise api_error(404, 'run_not_found', 'تشغيل التحسين أو السيناريو مش موجود للشركة.')
    return row


def eligibility_for_run(connection, cid, run_id, variant):
    row = connection.execute(RUN_INPUTS, (run_id, cid)).fetchone()
    if row is None:
        return {'eligible': False, 'has_candidate': False, 'reasons': ['inputs_unapproved'],
                'constraints': {}, 'hard_overruns': []}
    dataset, model = effective_inputs(row)
    result = json.loads(row['result_json'])
    output = result.get(variant, {})
    solver = (output.get('solver') if variant == 'scenario' else result.get('solver')) or {}
    current = connection.execute(
        'SELECT model_json FROM company_models WHERE company_id = ? ORDER BY version DESC LIMIT 1', (cid,),
    ).fetchone()
    policy = constraint_policy(row['template'], model)
    for key, rule in constraint_policy(row['template'], json.loads(current['model_json'])).items():
        if rule['kind'] == 'hard':
            policy[key] = rule
    return assess(
        row['template'], dataset, model['parameters'], policy, output.get('plan') if output.get('kpis') else None,
        solver | {'status': row['status']}, inputs_approved=approved_inputs(json.loads(row['validation_json'])),
        approval_policy=(result.get('decision_contract') or {}).get('approval_policy', result.get('approval_policy')),
    )


def enforce_eligibility(connection, cid, run_id, variant):
    eligibility = eligibility_for_run(connection, cid, run_id, variant)
    if not eligibility['eligible']:
        raise api_error(422, 'plan_ineligible', 'الخطة غير مؤهلة للاعتماد. راجع سجل الحل وتحقق من المدخلات.')
    return eligibility
