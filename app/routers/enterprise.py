from base64 import b64encode
from contextlib import contextmanager
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, Response
import numpy as np

from app.access import require_role
from app.db import audit, request_conn
from app.delivery.export import scenario_excel, tornado_excel
from app.enterprise import scenarios, store
from app.enterprise.result_context import result_context
from app.enterprise.demo import demo_template
from app.enterprise.impact_graph import ImpactError, WhatIfRequest
from app.enterprise.schema import ModelInput, ModelSnapshot, ModelSummary, ModelUpdate, Template
from app.enterprise.whatif import ImpactModel
from app.optimization.common import OptimizationInputError
from app.schemas import api_error

router = APIRouter(prefix='/enterprise', tags=['enterprise'])
CompanyScope = Annotated[int | None, Query(ge=1, lt=2**63)]


@router.get('/templates/enterprise', response_model=Template)
def template():
    return demo_template()


@router.get('/models', response_model=list[ModelSummary])
def list_models(connection=Depends(request_conn)):
    return store.list_models(connection)


@router.post('/models', response_model=ModelSnapshot, status_code=201)
def create(body: ModelInput, company_id: CompanyScope = None, connection=Depends(request_conn)):
    return store.create(connection, body, company_id)


@router.post('/models/demo', response_model=ModelSnapshot, status_code=201)
def create_demo(company_id: CompanyScope = None, connection=Depends(request_conn)):
    return store.create(connection, demo_template(), company_id)


@router.get('/models/{model_id}', response_model=ModelSnapshot)
def read(model_id: int, connection=Depends(request_conn)):
    return store.read(connection, model_id)


@router.put('/models/{model_id}', response_model=ModelSnapshot)
def update(model_id: int, body: ModelUpdate, connection=Depends(request_conn)):
    return store.update(connection, model_id, body)


@router.delete('/models/{model_id}', status_code=204)
def archive(model_id: int, connection=Depends(request_conn)):
    store.archive(connection, model_id)
    return Response(status_code=204)


@router.put('/models/{model_id}/company/{cid}')
def assign_company(model_id: int, cid: int, request: Request, connection=Depends(request_conn)):
    require_role(request.state.principal)
    with connection:
        connection.execute('BEGIN IMMEDIATE')
        if not connection.execute('SELECT 1 FROM companies WHERE id = ?', (cid,)).fetchone():
            raise api_error(404, 'company_not_found', 'الشركة مش موجودة.')
        model = store.read(connection, model_id)
        if model['company_id'] is not None:
            raise api_error(409, 'company_already_assigned', 'النموذج مربوط بالفعل بشركة.')
        connection.execute('UPDATE enterprise_models SET company_id = ? WHERE id = ?', (cid, model_id))
        connection.execute('UPDATE enterprise_scenarios SET company_id = ? WHERE model_id = ?', (cid, model_id))
        audit(connection, cid, 'enterprise.company.assign', 'enterprise_model', model_id)
        return store.read(connection, model_id)


@router.get('/models/{model_id}/versions', response_model=list[ModelSummary])
def versions(model_id: int, connection=Depends(request_conn)):
    return store.list_versions(connection, model_id)


@router.get('/models/{model_id}/versions/{version}', response_model=ModelSnapshot)
def read_version(model_id: int, version: int, connection=Depends(request_conn)):
    return store.read(connection, model_id, version)


def whatif_snapshot(connection, model_id, version):
    if model_id is None:
        template = demo_template()
        return {'id': None, 'version': None, 'name': template.name, 'graph': template.graph}
    return store.read(connection, model_id, version)


@router.get('/whatif/drivers')
def whatif_drivers(model_id: int | None = Query(default=None, ge=1),
                   version: int | None = Query(default=None, ge=1), connection=Depends(request_conn)):
    if version is not None and model_id is None:
        raise api_error(422, 'version_requires_model', 'اختار نموذج محفوظ عشان تحدد الإصدار.')
    snapshot = whatif_snapshot(connection, model_id, version)
    try:
        model = ImpactModel(snapshot['graph'], weeks=1)
        return {'drivers': model.drivers(), 'version': snapshot['version']}
    except ImpactError as error:
        raise api_error(422, 'invalid_enterprise_graph', str(error)) from None


def impact_model(snapshot, request, weeks):
    state = request.app.state
    return ImpactModel(snapshot['graph'], weeks=weeks, time_limit=state.settings.solver_time_limit,
                       forecast_engine=state.settings.forecast_engine, foundation=state.forecast_worker)


@contextmanager
def impact_errors():
    try:
        with np.errstate(over='raise', invalid='raise', divide='raise'):
            yield
    except (ImpactError, OptimizationInputError) as error:
        raise api_error(422, 'invalid_enterprise_scenario', str(error)) from None
    except (OverflowError, FloatingPointError):
        raise api_error(422, 'invalid_enterprise_numbers', 'الأرقام أكبر من النطاق المدعوم للحساب.') from None


@router.post('/whatif')
def whatif(body: WhatIfRequest, request: Request, connection=Depends(request_conn)):
    snapshot = whatif_snapshot(connection, body.model_id, body.version)
    with impact_errors():
        result = impact_model(snapshot, request, body.weeks).scenario(body)
    return {'model_id': snapshot['id'], 'version': snapshot['version'], 'name': snapshot['name'],
            'context': result_context(connection, snapshot, body.weeks), **result}


@router.get('/scenarios')
def list_scenarios(connection=Depends(request_conn)):
    return scenarios.list_saved(connection)


@router.post('/scenarios', status_code=201)
def save_scenario(body: scenarios.ScenarioSave, request: Request, company_id: CompanyScope = None,
                  connection=Depends(request_conn)):
    snapshot = whatif_snapshot(connection, body.model_id, body.version)
    if company_id is not None and company_id != snapshot.get('company_id'):
        raise api_error(422, 'enterprise_scenario_scope', 'احفظ السيناريو في نطاق مصدره.')
    with impact_errors():
        impact_model(snapshot, request, 52).assumption_values(body.assumptions)
    return scenarios.save(connection, body.name, snapshot, body.assumptions)


def comparison(body, request, connection):
    saved = scenarios.read_saved(connection, body.scenario_ids)
    scopes = {(row['model_id'], row['version'], row['company_id']) for row in saved}
    if len(scopes) != 1:
        raise api_error(422, 'enterprise_scenario_scope', 'اختار سيناريوهات من نفس النموذج ونفس الإصدار.')
    model_id, version, _ = scopes.pop()
    snapshot = whatif_snapshot(connection, model_id, version)
    with impact_errors():
        result = scenarios.compare(impact_model(snapshot, request, body.weeks), saved, body)
    result['context'] = result_context(connection, snapshot, body.weeks)
    return result, snapshot


def sensitivity(body, request, connection):
    snapshot = whatif_snapshot(connection, body.model_id, body.version)
    with impact_errors():
        result = scenarios.tornado(impact_model(snapshot, request, body.weeks), body)
    result['context'] = result_context(connection, snapshot, body.weeks)
    return result, snapshot


def excel_response(content, filename):
    return Response(content, media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                    headers={'Content-Disposition': f'attachment; filename="{filename}"'})


@router.post('/scenarios/compare')
def compare_scenarios(body: scenarios.ComparisonRequest, request: Request, include_excel: bool = False,
                      connection=Depends(request_conn)):
    result, snapshot = comparison(body, request, connection)
    if include_excel:
        result['xlsx_base64'] = b64encode(scenario_excel(result, snapshot)).decode('ascii')
    return {**result, 'model_id': snapshot['id'], 'version': snapshot['version'], 'name': snapshot['name']}


@router.post('/scenarios/compare.xlsx', response_class=Response)
def export_comparison(body: scenarios.ComparisonRequest, request: Request, connection=Depends(request_conn)):
    result, snapshot = comparison(body, request, connection)
    return excel_response(scenario_excel(result, snapshot), 'enterprise-scenarios.xlsx')


@router.post('/scenarios/tornado')
def sensitivity_analysis(body: scenarios.TornadoRequest, request: Request, include_excel: bool = False,
                         connection=Depends(request_conn)):
    result, snapshot = sensitivity(body, request, connection)
    if include_excel:
        result['xlsx_base64'] = b64encode(tornado_excel(result, snapshot)).decode('ascii')
    return {**result, 'model_id': snapshot['id'], 'version': snapshot['version'], 'name': snapshot['name']}


@router.post('/scenarios/tornado.xlsx', response_class=Response)
def export_sensitivity(body: scenarios.TornadoRequest, request: Request, connection=Depends(request_conn)):
    result, snapshot = sensitivity(body, request, connection)
    return excel_response(tornado_excel(result, snapshot), 'enterprise-tornado.xlsx')
