import json

from fastapi import APIRouter, Depends, Request

from app.db import audit, encode, request_conn, utc_now
from app.llm.explain import optimization_explanation
from app.llm.provider import get_provider
from app.optimization import contracting, production
from app.optimization.common import OptimizationInputError
from app.routers.companies import get_company, get_template
from app.routers.model import current_model
from app.schemas import OptimizeRequest, OptimizeResult, RunDetail, RunKind, api_error

router = APIRouter()


@router.post('/companies/{cid}/optimize', response_model=OptimizeResult)
def optimize(cid: int, request: Request, body: OptimizeRequest | None = None,
             connection=Depends(request_conn)):
    settings = request.app.state.settings
    return run_optimization(connection, cid, body or OptimizeRequest(), settings, get_provider(settings))


def run_optimization(connection, cid, body, settings, provider, *, dataset_id=None):
    company = get_company(connection, cid)
    dataset_id = body.dataset_id or dataset_id or company['active_dataset_id']
    dataset = connection.execute(
        'SELECT payload_json FROM datasets WHERE id = ? AND company_id = ?', (dataset_id, cid),
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

    time_limit = body.time_limit if body.time_limit is not None else settings.solver_time_limit
    solver_module = {'contracting': contracting, 'production': production}[company['template']]
    payload = json.loads(dataset['payload_json'])
    try:
        result = solver_module.solve(payload, model['parameters'], weights, time_limit)
    except OptimizationInputError as error:
        raise api_error(422, 'invalid_optimization_input', str(error)) from None

    solver = result['solver']
    grounded = optimization_explanation(company['template'], result, payload, model['parameters'])
    result['explanation_ar'] = provider.explain({'grounded_ar': grounded}, grounded)
    stored = result | {'effective_weights': weights}
    run_id = connection.execute(
        '''INSERT INTO runs (company_id, dataset_id, model_id, kind, result_json, status, wall_s, created_at)
           VALUES (?, ?, ?, 'optimize', ?, ?, ?, ?)''',
        (cid, dataset_id, model['id'], encode(stored), solver['status'], solver['wall_s'], utc_now()),
    ).lastrowid
    audit(connection, cid, 'run.optimize', 'run', run_id)
    return {'run_id': run_id, **result}


def run_detail(row):
    if row is None:
        raise api_error(404, 'run_not_found', 'تشغيل التحسين غير موجود.')
    run = dict(row)
    run['scenario'] = json.loads(run.pop('scenario_json') or 'null')
    run['result'] = json.loads(run.pop('result_json'))
    if run['kind'] == 'optimize':
        run['result'].pop('effective_weights', None)
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
    return run_detail(row)


@router.get('/runs/{rid}', response_model=RunDetail)
def get_run(rid: int, connection=Depends(request_conn)):
    return run_detail(connection.execute('SELECT * FROM runs WHERE id = ?', (rid,)).fetchone())
