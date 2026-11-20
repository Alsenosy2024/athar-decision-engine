import json

from pydantic import TypeAdapter

from app.db import audit, utc_now
from app.llm.schema import Change
from app.schemas import api_error
from app.simulation.scenario import apply_changes

RUN_KINDS = {'optimized': 'optimize', 'scenario': 'whatif'}
SCENARIO_CHANGES = TypeAdapter(list[Change])


def get_plan(connection, pid, *, company_id=None):
    row = connection.execute('SELECT * FROM plans WHERE id = ?', (pid,)).fetchone()
    if row is None or (company_id is not None and row['company_id'] != company_id):
        raise api_error(404, 'plan_not_found', 'الخطة غير موجودة.')
    return dict(row)


def list_plans(connection, cid):
    return [dict(row) for row in connection.execute(
        'SELECT * FROM plans WHERE company_id = ? ORDER BY id DESC', (cid,),
    )]


def latest_approved_plan(connection, cid):
    return connection.execute(
        """SELECT id, run_id FROM plans WHERE company_id = ? AND status = 'approved'
           ORDER BY decided_at DESC, id DESC LIMIT 1""", (cid,),
    ).fetchone()


def create_plan(connection, cid, run_id, variant):
    run = connection.execute(
        'SELECT kind, result_json FROM runs WHERE id = ? AND company_id = ?', (run_id, cid),
    ).fetchone()
    if run is None:
        raise api_error(404, 'run_not_found', 'التشغيل غير موجود للشركة.')
    if run['kind'] != RUN_KINDS[variant]:
        raise api_error(422, 'invalid_plan_variant', 'نوع الخطة لا يطابق نتيجة التشغيل.')
    result = json.loads(run['result_json'])
    policy = (result.get('decision_contract') or {}).get('approval_policy', result.get('approval_policy'))
    if policy == 'shadow_only':
        raise api_error(422, 'shadow_run', 'تشغيل التوقع للمراجعة التجريبية فقط.')
    output = result.get(variant, {})
    if not output.get('plan') or not output.get('kpis'):
        raise api_error(422, 'plan_unavailable', 'التشغيل لا يحتوي على خطة قابلة للاعتماد.')

    with connection:
        pid = connection.execute(
            """INSERT INTO plans (company_id, run_id, variant, status, created_at)
               VALUES (?, ?, ?, 'draft', ?)""", (cid, run_id, variant, utc_now()),
        ).lastrowid
        audit(connection, cid, 'plan.create', 'plan', pid, {'run_id': run_id, 'variant': variant})
        return get_plan(connection, pid)


def decide_plan(connection, pid, decision, comment, principal):
    with connection:
        changed = connection.execute(
            """UPDATE plans SET status = ?, comment = ?, actor = ?, decided_at = ?, decided_by_user_id = ?
               WHERE id = ? AND status = 'draft'""", (decision, comment, principal.username, utc_now(), principal.id, pid),
        ).rowcount
        plan = get_plan(connection, pid)
        if not changed:
            raise api_error(409, 'plan_already_decided', 'تم اتخاذ قرار بشأن الخطة بالفعل.')
        action = 'plan.approve' if decision == 'approved' else 'plan.reject'
        audit(connection, plan['company_id'], action, 'plan', pid, {'comment': comment}, actor=principal.username, user_id=principal.id)
        return plan


def require_approved(plan):
    if plan['status'] != 'approved':
        raise api_error(409, 'plan_not_approved', 'اعتمد الخطة أولًا لإتاحة هذه العملية.')


def plan_snapshot(connection, plan, template):
    row = connection.execute(
        """SELECT r.result_json, r.scenario_json, d.payload_json, m.model_json,
                  base.result_json AS base_result_json
           FROM runs r
           JOIN datasets d ON d.id = r.dataset_id AND d.company_id = r.company_id
           JOIN company_models m ON m.id = r.model_id AND m.company_id = r.company_id
           LEFT JOIN runs base ON base.id = r.base_run_id AND base.company_id = r.company_id
           WHERE r.id = ? AND r.company_id = ?""", (plan['run_id'], plan['company_id']),
    ).fetchone()
    dataset = json.loads(row['payload_json'])
    model = json.loads(row['model_json'])
    if plan['variant'] == 'scenario':
        base_result = json.loads(row['base_result_json'])
        model['weights'] = base_result.get('effective_weights', model['weights'])
        changes = SCENARIO_CHANGES.validate_python(json.loads(row['scenario_json'])['changes'])
        dataset, model, _ = apply_changes(dataset, model, changes, template_id=template)
    output = json.loads(row['result_json'])[plan['variant']]
    return output['plan'], output['kpis'], dataset, model['parameters']
