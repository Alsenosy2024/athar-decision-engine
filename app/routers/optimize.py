import json

from fastapi import APIRouter, Depends, Request

from app.db import audit, encode, request_conn, utc_now
from app.decision_loop.approval_gate import effective_inputs, read_record
from app.decision_loop.contract import ContractError, contracting_dataset, solution_fingerprint
from app.decision_loop.eligibility import approved_inputs, constraint_policy
from app.decision_loop.service import pinned_experiment
from app.decision_loop.solution_record import solution_record, solve_options, stored_record
from app.decision_loop.storage import decision_contract, previous_plan, replay_inputs
from app.llm.explain import optimization_explanation
from app.llm.provider import get_provider
from app.optimization import contracting, production
from app.optimization.common import OptimizationInputError
from app.routers.companies import get_company, get_template
from app.routers.model import current_model
from app.schemas import OptimizeRequest, OptimizeResult, RunDetail, RunKind, api_error

router = APIRouter()


@router.post('/companies/{cid}/optimize', response_model=OptimizeResult, response_model_exclude_unset=True)
def optimize(cid: int, request: Request, body: OptimizeRequest | None = None,
             connection=Depends(request_conn)):
    settings = request.app.state.settings
    return run_optimization(connection, cid, body or OptimizeRequest(), settings, get_provider(settings))


def run_optimization(connection, cid, body, settings, provider, *, dataset_id=None):
    company = get_company(connection, cid)
    template = company['template']
    legacy_replay = False
    try:
        if body.replay_run_id is not None:
            if body.model_fields_set - {'replay_run_id'} or dataset_id is not None:
                raise ContractError('invalid_replay', 'إعادة التشغيل تستخدم المدخلات المثبتة من غير تعديلات.')
            dataset, payload, model, contract, previous, original = replay_inputs(
                connection, company, body.replay_run_id,
            )
            dataset_id = dataset['dataset_id']
            legacy_replay = original.get('legacy_contract_solver', 'solution_record' not in original)
            weights = contract['weights']
            time_limit = contract['solver']['wall_limit_s']
        else:
            if body.risk_policy is not None and (template != 'production' or body.forecast_id is None):
                raise ContractError('forecast_required', 'تقييم المخاطر محتاج توقع مثبت في قالب الإنتاج.')
            dataset_id, dataset, payload, model, weights = optimization_inputs(
                connection, company, body, dataset_id,
            )
            time_limit = body.time_limit if body.time_limit is not None else settings.solver_time_limit
            previous = previous_plan(connection, cid, body.previous_run_id)
            contract = None
            if body.forecast_id is not None:
                contract = decision_contract(
                    connection, company, dataset_id, payload, dataset['created_at'], model, weights,
                    body.forecast_id, time_limit, body.previous_run_id, previous, body.risk_policy,
                )

        solver_module = {'contracting': contracting, 'production': production}[template]
        policy = constraint_policy(template, model)
        options = {} if legacy_replay else solve_options(template, policy)
        result = solver_module.solve(
            payload, model['parameters'], weights, time_limit, decision_contract=contract, previous=previous,
            **options,
        )
        if contract is not None:
            result['decision_contract'] = contract
            if 'risk_policy' in contract:
                result['risk_assessment'] = pinned_experiment(payload, model | {'weights': weights}, contract)
            result['solution_fingerprint'] = solution_fingerprint(result)
            if body.replay_run_id is not None and result['solution_fingerprint'] != original['solution_fingerprint']:
                raise ContractError('replay_result_changed', 'نتيجة الإعادة اختلفت عن التشغيل المحفوظ.')
    except ContractError as error:
        if error.code in ('forecast_not_found', 'run_not_found'):
            status = 404
        else:
            status = 409 if error.code.startswith('replay_') else 422
        raise api_error(status, error.code, str(error)) from None
    except OptimizationInputError as error:
        raise api_error(422, 'invalid_optimization_input', str(error)) from None

    view_dataset = contracting_dataset(payload, contract) if template == 'contracting' else payload
    result['solution_record'] = solution_record(
        template, view_dataset, model | {'weights': weights}, policy, result,
        dataset_id=dataset_id, inputs_approved=approved_inputs(json.loads(dataset['validation_json'])),
        time_limit=time_limit, contract=contract,
    )
    solver = result['solver']
    grounded = optimization_explanation(template, result, payload, model['parameters'])
    result['explanation_ar'] = grounded if contract is not None else provider.explain(
        {'grounded_ar': grounded}, grounded,
    )
    stored = result | {'effective_weights': weights}
    if legacy_replay:
        stored['legacy_contract_solver'] = True
    run_id = connection.execute(
        '''INSERT INTO runs (company_id, dataset_id, model_id, kind, result_json, status, wall_s, created_at)
           VALUES (?, ?, ?, 'optimize', ?, ?, ?, ?)''',
        (cid, dataset_id, model['id'], encode(stored), solver['status'], solver['wall_s'], utc_now()),
    ).lastrowid
    details = {key: value for key, value in {
        'input_fingerprint': contract['input_fingerprint'] if contract is not None else None,
        'replay_run_id': body.replay_run_id,
        'previous_run_id': body.previous_run_id,
    }.items() if value is not None}
    audit(connection, cid, 'run.optimize', 'run', run_id, details or None)
    return {'run_id': run_id, **result}


def optimization_inputs(connection, company, body, default_dataset):
    dataset_id = body.dataset_id or default_dataset or company['active_dataset_id']
    dataset = connection.execute(
        'SELECT payload_json, created_at, validation_json FROM datasets WHERE id = ? AND company_id = ?',
        (dataset_id, company['id']),
    ).fetchone()
    if dataset is None:
        raise api_error(404, 'dataset_not_found', 'مجموعة البيانات غير موجودة للشركة.')
    model = current_model(connection, company)
    template = get_template(company['template'])
    weight_specs = {item.id: item for item in template.objective_weights}
    weights = model['weights'] | (body.weights or {})
    for key, value in weights.items():
        if key not in weight_specs or not weight_specs[key].min <= value <= weight_specs[key].max:
            raise api_error(422, 'invalid_weight', f'الوزن خارج النطاق المسموح ({key}).')
    return dataset_id, dataset, json.loads(dataset['payload_json']), model, weights


def run_detail(row, connection):
    if row is None:
        raise api_error(404, 'run_not_found', 'تشغيل التحسين غير موجود.')
    run = dict(row)
    run['scenario'] = json.loads(run.pop('scenario_json') or 'null')
    run['result'] = json.loads(run.pop('result_json'))
    if run['kind'] in ('optimize', 'whatif') and 'solution_record' not in run['result']:
        source = read_record(connection, run['company_id'], run['id'])
        dataset, model = effective_inputs(source)
        run['result']['solution_record'] = stored_record(source, dataset, model, run['result'])
    if run['kind'] == 'optimize':
        run['result'].pop('effective_weights', None)
        run['result'].pop('legacy_contract_solver', None)
    return run


@router.get('/companies/{cid}/runs/latest', response_model=RunDetail | None)
def latest_run(cid: int, kind: RunKind = 'optimize', allow_missing: bool = False,
               connection=Depends(request_conn)):
    get_company(connection, cid)
    row = connection.execute(
        'SELECT * FROM runs WHERE company_id = ? AND kind = ? ORDER BY id DESC LIMIT 1',
        (cid, kind),
    ).fetchone()
    if row is None and allow_missing:
        return None
    return run_detail(row, connection)


@router.get('/runs/{rid}', response_model=RunDetail)
def get_run(rid: int, connection=Depends(request_conn)):
    return run_detail(connection.execute('SELECT * FROM runs WHERE id = ?', (rid,)).fetchone(), connection)
