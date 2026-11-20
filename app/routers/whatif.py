import json
from time import perf_counter

import numpy as np
from fastapi import APIRouter, Depends, Request
from pydantic import Field

from app.db import audit, encode, request_conn, utc_now
from app.llm.explain import scenario_explanation
from app.llm.provider import get_provider
from app.llm.schema import Change, StrictBody
from app.optimization import contracting, production
from app.optimization.common import OptimizationInputError
from app.routers.companies import get_company
from app.routers.forecast import company_forecast
from app.schemas import ContractingRisk, MonteCarloRequest, ProductionRisk, PlanVariant, SolverSummary, api_error
from app.simulation.montecarlo import run_mc
from app.simulation.scenario import apply_changes, kpi_diff

router = APIRouter()


class WhatIfRequest(StrictBody):
    base_run_id: int = Field(gt=0)
    changes: list[Change] = Field(min_length=1, max_length=20)


class ScenarioVariant(PlanVariant):
    solver: SolverSummary


class WhatIfResult(StrictBody):
    run_id: int
    base: dict
    scenario: ScenarioVariant
    diff: dict
    changes_ar: list[str]
    explanation_ar: str


def base_optimization(connection, cid, run_id):
    base = connection.execute(
        '''SELECT runs.*, datasets.payload_json, company_models.model_json
           FROM runs
           JOIN datasets ON datasets.id = runs.dataset_id AND datasets.company_id = runs.company_id
           JOIN company_models ON company_models.id = runs.model_id AND company_models.company_id = runs.company_id
           WHERE runs.id = ? AND runs.company_id = ? AND runs.kind = 'optimize' ''',
        (run_id, cid),
    ).fetchone()
    if base is None:
        raise api_error(404, 'run_not_found', 'تشغيل التحسين غير موجود للشركة.')
    return base


@router.post('/companies/{cid}/whatif', response_model=WhatIfResult)
def whatif(cid: int, body: WhatIfRequest, request: Request, connection=Depends(request_conn)):
    settings = request.app.state.settings
    company = get_company(connection, cid)
    return run_whatif(connection, company, body, settings, get_provider(settings))


def run_whatif(connection, company, body, settings, provider):
    cid, template = company['id'], company['template']
    base = base_optimization(connection, cid, body.base_run_id)
    original = json.loads(base['result_json'])
    base_kpis = original['optimized']['kpis']
    if not base_kpis:
        raise api_error(422, 'plan_unavailable', 'تشغيل التحسين الأساسي مفيهوش خطة للمقارنة.')
    model = json.loads(base['model_json'])
    model['weights'] = original.get('effective_weights', model['weights'])
    payload, changed_model, changes_ar = apply_changes(
        json.loads(base['payload_json']), model, body.changes, template_id=template,
    )
    parameters = changed_model['parameters']
    solver_module = {'contracting': contracting, 'production': production}[template]
    try:
        solved = solver_module.solve(payload, parameters, changed_model['weights'], settings.solver_time_limit)
    except OptimizationInputError as error:
        raise api_error(422, 'invalid_optimization_input', str(error)) from None
    solver = solved['solver']
    scenario = solved['optimized'] | {'solver': solver}
    grounded = scenario_explanation(template, base_kpis, scenario['kpis'], changes_ar, payload, parameters)
    result = {
        'base': {'kpis': base_kpis}, 'scenario': scenario,
        'diff': kpi_diff(base_kpis, scenario['kpis']), 'changes_ar': changes_ar,
        'explanation_ar': provider.explain({'grounded_ar': grounded}, grounded),
    }
    run_id = connection.execute(
        '''INSERT INTO runs (company_id, dataset_id, model_id, kind, base_run_id,
                             scenario_json, result_json, status, wall_s, created_at)
           VALUES (?, ?, ?, 'whatif', ?, ?, ?, ?, ?, ?)''',
        (cid, base['dataset_id'], base['model_id'], base['id'],
         encode({'changes': [change.model_dump(by_alias=True) for change in body.changes]}),
         encode(result), solver['status'], solver['wall_s'], utc_now()),
    ).lastrowid
    audit(connection, cid, 'run.whatif', 'run', run_id)
    return {'run_id': run_id, **result}


@router.post('/companies/{cid}/montecarlo', response_model=ContractingRisk | ProductionRisk)
def montecarlo(cid: int, body: MonteCarloRequest, request: Request, connection=Depends(request_conn)):
    company = get_company(connection, cid)
    base = base_optimization(connection, cid, body.run_id)
    plan = json.loads(base['result_json'])['optimized']['plan']
    if not plan:
        raise api_error(422, 'plan_unavailable', 'تشغيل التحسين لا يحتوي على خطة قابلة للمحاكاة.')

    start = perf_counter()
    dataset = json.loads(base['payload_json'])
    parameters = json.loads(base['model_json'])['parameters']
    forecast = company_forecast(connection, company, dataset, request)
    try:
        with np.errstate(over='raise', invalid='raise'):
            result = run_mc(
                company['template'], dataset, plan, forecast, body.n, body.seed, parameters=parameters,
            )
    except OptimizationInputError as error:
        raise api_error(422, 'invalid_simulation_input', str(error)) from None
    except (FloatingPointError, OverflowError):
        raise api_error(422, 'invalid_simulation_input', 'قيم البيانات أكبر من النطاق المدعوم للمحاكاة.') from None

    with connection:
        run_id = connection.execute(
            '''INSERT INTO runs (company_id, dataset_id, model_id, kind, base_run_id,
                                 scenario_json, result_json, status, wall_s, created_at)
               VALUES (?, ?, ?, 'montecarlo', ?, ?, ?, 'OK', ?, ?)''',
            (cid, base['dataset_id'], base['model_id'], base['id'], encode({'n': body.n, 'seed': body.seed}),
             encode(result), perf_counter() - start, utc_now()),
        ).lastrowid
        audit(connection, cid, 'run.montecarlo', 'run', run_id)
    return result
