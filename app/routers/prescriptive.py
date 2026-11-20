"""Phase 18 endpoints for saved company enterprise models."""
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Request
from fastapi.encoders import jsonable_encoder
from pydantic import Field, ValidationError

from app.db import audit, encode, request_conn
from app.enterprise import store
from app.enterprise.impact_graph import ImpactError
from app.enterprise.result_context import effective_weeks, result_context
from app.enterprise.scenarios import KPIS
from app.enterprise.schema import StrictFields
from app.enterprise.whatif import ImpactModel
from app.prescriptive import calibration, goal, monitor, records
from app.routers.enterprise import impact_errors
from app.schemas import api_error

router = APIRouter(tags=['prescriptive'])
scope = '/enterprise/models/{model_id}/prescriptive'
RecordId = Annotated[int, Path(ge=1, lt=2**63)]


class Revision(StrictFields):
    version: int = Field(ge=1, strict=True)


class GoalText(StrictFields):
    message: str = Field(min_length=1, max_length=300)


def engine(snapshot, request):
    settings = request.app.state.settings
    return ImpactModel(snapshot['graph'], weeks=effective_weeks(snapshot['graph']),
                       time_limit=min(settings.solver_time_limit, settings.interactive_solver_time_limit),
                       forecast_engine='stat')


def thresholds(connection, model_id):
    row = connection.execute('SELECT settings_json FROM enterprise_kpi_thresholds WHERE model_id = ?',
                             (model_id,)).fetchone()
    return monitor.Thresholds.model_validate_json(row['settings_json']) if row else monitor.Thresholds()


@router.get(scope + '/setup')
def setup(model_id: RecordId, request: Request, connection=Depends(request_conn)):
    snapshot = store.read(connection, model_id)
    with impact_errors():
        model = engine(snapshot, request)
        drivers = goal.controllable(model)
    return {'version': snapshot['version'], 'controls': drivers, 'drivers': model.drivers(), 'kpis': KPIS,
            'thresholds': thresholds(connection, model_id),
            'calibration': records.read_calibration(connection, model_id, snapshot['version'])}


def calibration_report(connection, snapshot, request):
    graph, report = calibration.calibrate(engine(snapshot, request), records.original_defaults(
        connection, snapshot['id'], snapshot['graph'], snapshot['version'],
    ))
    return graph, jsonable_encoder(report)


@router.get(scope + '/calibration')
def preview_calibration(model_id: RecordId, request: Request, connection=Depends(request_conn)):
    snapshot = store.read(connection, model_id)
    with impact_errors():
        _, report = calibration_report(connection, snapshot, request)
    return {'version': snapshot['version'], 'edges': report}


@router.post(scope + '/calibration')
def apply_calibration(model_id: RecordId, body: Revision, request: Request, connection=Depends(request_conn)):
    snapshot = records.checked_version(connection, model_id, body.version)
    with impact_errors():
        graph, report = calibration_report(connection, snapshot, request)
        # Recompile before committing the fitted graph.
        engine({**snapshot, 'graph': graph}, request)
    version = records.save_calibration(connection, model_id, body.version, graph, report)
    return {'version': version, 'edges': report}


@router.post(scope + '/goal-input')
def goal_input(model_id: RecordId, body: GoalText, connection=Depends(request_conn)):
    store.read(connection, model_id)
    with impact_errors():
        try:
            return goal.parse_target(body.message)
        except ValidationError:
            raise ImpactError('هدف الربح أو السيولة خارج النطاق المدعوم.') from None


@router.post(scope + '/goal-seek')
def goal_seek(model_id: RecordId, body: goal.Goal, request: Request, connection=Depends(request_conn)):
    snapshot = records.checked_version(connection, model_id, body.version)
    with impact_errors():
        model = engine(snapshot, request)
        result = goal.search(model, body)
    return {'version': snapshot['version'], 'context': result_context(connection, snapshot, model.weeks), **result}


@router.get(scope + '/monitor')
def alerts(model_id: RecordId, request: Request, connection=Depends(request_conn)):
    snapshot = store.read(connection, model_id)
    config = thresholds(connection, model_id)
    with impact_errors():
        result = monitor.monitor(engine(snapshot, request), config)
    return {'version': snapshot['version'], 'thresholds': config, **result}


@router.put(scope + '/thresholds')
def update_thresholds(model_id: RecordId, body: monitor.Thresholds, request: Request,
                      connection=Depends(request_conn)):
    snapshot = store.read(connection, model_id)
    with impact_errors():
        result = monitor.monitor(engine(snapshot, request), body, strict=True)
    with connection:
        connection.execute('BEGIN IMMEDIATE')
        records.checked_version(connection, model_id, snapshot['version'])
        connection.execute(
            '''INSERT INTO enterprise_kpi_thresholds VALUES (?, ?)
               ON CONFLICT(model_id) DO UPDATE SET settings_json = excluded.settings_json''',
            (model_id, encode(body.model_dump(mode='json'))),
        )
        audit(connection, snapshot['company_id'], 'enterprise.thresholds.update', 'enterprise_model', model_id)
    return {'version': snapshot['version'], 'thresholds': body, **result}


@router.post(scope + '/decisions', status_code=201)
def save_decision(model_id: RecordId, body: goal.Draft, request: Request, connection=Depends(request_conn)):
    snapshot = records.checked_version(connection, model_id, body.goal.version)
    with impact_errors():
        forecast = goal.validate_draft(engine(snapshot, request), body)
    decision_id = records.create_draft(connection, model_id, body.goal.version, body.name, forecast)
    return {'id': decision_id, 'status': 'draft'}


@router.post(scope + '/decisions/{decision_id}/decide')
def decide(model_id: RecordId, decision_id: RecordId, body: records.Decision, request: Request,
           connection=Depends(request_conn)):
    snapshot = store.read(connection, model_id)
    status = records.decide(connection, snapshot, decision_id, body, request.state.principal)
    return {'id': decision_id, 'status': status}


@router.post(scope + '/decisions/{decision_id}/actuals', status_code=201)
def record_actuals(model_id: RecordId, decision_id: RecordId, body: records.Actuals, request: Request,
                   connection=Depends(request_conn)):
    snapshot = store.read(connection, model_id)
    return {'id': records.record_actuals(connection, snapshot, decision_id, body, request.state.principal)}


@router.get(scope + '/decisions')
def decision_log(model_id: RecordId, connection=Depends(request_conn)):
    return records.decision_log(connection, store.read(connection, model_id))


@router.get('/companies/{cid}/decision-log')
def approved_sector_log(cid: RecordId, connection=Depends(request_conn)):
    if not connection.execute('SELECT 1 FROM companies WHERE id = ?', (cid,)).fetchone():
        raise api_error(404, 'company_not_found', 'الشركة غير موجودة.')
    return records.approved_sector_log(connection, cid)
