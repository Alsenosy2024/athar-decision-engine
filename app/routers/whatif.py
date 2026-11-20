import json
from time import perf_counter
from typing import Literal

import numpy as np
from fastapi import APIRouter, Depends, Request
from pydantic import Field, JsonValue

from app.decision_loop.contract import contracting_dataset
from app.decision_loop.eligibility import approved_inputs, constraint_policy
from app.decision_loop.service import pinned_experiment
from app.decision_loop.solution_record import changed_input, solution_record, solve_options
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
    approval_policy: Literal['shadow_only'] | None = None
    source_input_fingerprint: str | None = None
    solution_record: dict[str, JsonValue] | None = None
    risk_assessment: dict[str, JsonValue] | None = None


def base_optimization(connection, cid, run_id):
    base = connection.execute(
        '''SELECT runs.*, datasets.payload_json, datasets.validation_json, company_models.model_json, company_models.version
           FROM runs
           JOIN datasets ON datasets.id = runs.dataset_id AND datasets.company_id = runs.company_id
           JOIN company_models ON company_models.id = runs.model_id AND company_models.company_id = runs.company_id
           WHERE runs.id = ? AND runs.company_id = ? AND runs.kind = 'optimize' ''',
        (run_id, cid),
    ).fetchone()
    if base is None:
        raise api_error(404, 'run_not_found', 'تشغيل التحسين غير موجود للشركة.')
    return base


@router.post('/companies/{cid}/whatif', response_model=WhatIfResult, response_model_exclude_unset=True)
def whatif(cid: int, body: WhatIfRequest, request: Request, connection=Depends(request_conn)):
    settings = request.app.state.settings
    company = get_company(connection, cid)
    return run_whatif(connection, company, body, settings, get_provider(settings))


def run_whatif(connection, company, body, settings, provider):
    cid, template = company['id'], company['template']
    base = base_optimization(connection, cid, body.base_run_id)
    original = json.loads(base['result_json'])
    contract = original.get('decision_contract')
    base_kpis = original['optimized']['kpis']
    if not base_kpis:
        raise api_error(422, 'plan_unavailable', 'تشغيل التحسين الأساسي مفيهوش خطة للمقارنة.')
    model = json.loads(base['model_json'])
    model['weights'] = original.get('effective_weights', model['weights'])
    original_data = json.loads(base['payload_json'])
    payload, changed_model, changes_ar = apply_changes(
        original_data, model, body.changes, template_id=template,
    )
    parameters = changed_model['parameters']
    policy = constraint_policy(template, changed_model)
    time_limit = (contract['solver']['wall_limit_s'] if contract else
                  original.get('solution_record', {}).get('snapshot', {}).get('time_limit_s')
                  or settings.solver_time_limit)
    solver_module = {'contracting': contracting, 'production': production}[template]
    try:
        solved = solver_module.solve(
            payload, parameters, changed_model['weights'], time_limit,
            decision_contract=contract, **solve_options(template, policy),
        )
        risk = pinned_experiment(payload, changed_model, contract) if contract and 'risk_policy' in contract else None
    except OptimizationInputError as error:
        raise api_error(422, 'invalid_optimization_input', str(error)) from None
    solver = solved['solver']
    scenario = solved['optimized'] | {'solver': solver}
    grounded = scenario_explanation(template, base_kpis, scenario['kpis'], changes_ar, payload, parameters)
    view_dataset = contracting_dataset(payload, contract) if template == 'contracting' else payload
    record = solution_record(
        template, view_dataset, changed_model | {'id': base['model_id'], 'version': base['version']}, policy, solved,
        dataset_id=base['dataset_id'], inputs_approved=approved_inputs(json.loads(base['validation_json'])),
        time_limit=time_limit, contract=contract,
    )
    record['base_run_id'] = base['id']
    record['changed_input'] = changed_input(template, original_data, model, payload, changed_model, body.changes)
    result = {
        'solution_record': record,
        'base': {'kpis': base_kpis}, 'scenario': scenario,
        'diff': kpi_diff(base_kpis, scenario['kpis']), 'changes_ar': changes_ar,
        'explanation_ar': grounded if contract else provider.explain({'grounded_ar': grounded}, grounded),
    }
    if contract:
        result['approval_policy'] = contract['approval_policy']
        result['source_input_fingerprint'] = contract['input_fingerprint']
    if risk is not None:
        result['risk_assessment'] = risk
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


@router.post('/companies/{cid}/montecarlo', response_model=ContractingRisk | ProductionRisk,
             response_model_exclude_unset=True)
def montecarlo(cid: int, body: MonteCarloRequest, request: Request, connection=Depends(request_conn)):
    company = get_company(connection, cid)
    base = base_optimization(connection, cid, body.run_id)
    original = json.loads(base['result_json'])
    plan = original['optimized']['plan']
    contract = original.get('decision_contract')
    if not plan:
        raise api_error(422, 'plan_unavailable', 'تشغيل التحسين لا يحتوي على خطة قابلة للمحاكاة.')

    start = perf_counter()
    dataset = json.loads(base['payload_json'])
    parameters = json.loads(base['model_json'])['parameters']
    forecast = None if contract else company_forecast(connection, company, dataset, request)
    try:
        with np.errstate(over='raise', invalid='raise'):
            result = run_mc(
                company['template'], dataset, plan, forecast, body.n, body.seed,
                parameters=parameters, decision_contract=contract,
            )
    except OptimizationInputError as error:
        raise api_error(422, 'invalid_simulation_input', str(error)) from None
    except (FloatingPointError, OverflowError):
        raise api_error(422, 'invalid_simulation_input', 'قيم البيانات أكبر من النطاق المدعوم للمحاكاة.') from None

    evidence = {'n': body.n, 'seed': body.seed}
    if contract:
        evidence['input_fingerprint'] = contract['input_fingerprint']
        evidence['scenario_set_fingerprint'] = contract['scenario_set_fingerprint']
    with connection:
        run_id = connection.execute(
            '''INSERT INTO runs (company_id, dataset_id, model_id, kind, base_run_id,
                                 scenario_json, result_json, status, wall_s, created_at)
               VALUES (?, ?, ?, 'montecarlo', ?, ?, ?, 'OK', ?, ?)''',
            (cid, base['dataset_id'], base['model_id'], base['id'], encode(evidence),
             encode(result), perf_counter() - start, utc_now()),
        ).lastrowid
        audit(connection, cid, 'run.montecarlo', 'run', run_id)
    return result
