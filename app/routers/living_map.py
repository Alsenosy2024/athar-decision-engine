"""Authenticated home map and its transient goal-seek preview."""
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from pydantic import Field

from app.access import require_company
from app.db import request_conn
from app.enterprise import store
from app.enterprise.living_map import map_payload
from app.enterprise.result_context import effective_weeks, result_context
from app.enterprise.scenarios import kpis
from app.enterprise.schema import StrictFields
from app.intelligence import decision
from app.prescriptive.records import decision_log
from app.process_mining.map_projection import with_process_flow
from app.routers.enterprise import CompanyScope, impact_errors, impact_model, whatif_snapshot
from app.schemas import api_error

router = APIRouter(prefix='/enterprise/map', tags=['enterprise-map'])
Selection = Annotated[int | None, Query(ge=1, lt=2**63)]


@router.get('')
def read_map(request: Request, company_id: CompanyScope = None, model_id: Selection = None,
             version: Selection = None, connection=Depends(request_conn)):
    if version is not None and model_id is None:
        raise api_error(422, 'version_requires_model', 'اختار نموذج محفوظ عشان تحدد الإصدار.')
    company, models = None, []
    if company_id is not None:
        require_company(request.state.principal, company_id)
        row = connection.execute('SELECT id, name, template FROM companies WHERE id = ?', (company_id,)).fetchone()
        if row is None:
            raise api_error(404, 'company_not_found', 'الشركة مش موجودة.')
        company = dict(row)
        models = [item for item in store.list_models(connection) if item['company_id'] == company_id]
        onboarding = connection.execute('SELECT model_id FROM onboarding_companies WHERE company_id = ?',
                                        (company_id,)).fetchone()
        available = {item['id'] for item in models}
        if model_id is None and models:
            model_id = onboarding['model_id'] if onboarding and onboarding['model_id'] in available else models[-1]['id']
        if model_id is None:
            return with_process_flow(connection, {
                'company': company, 'models': [], 'model': None, 'is_demo': False,
            }, company_id)

    snapshot = whatif_snapshot(connection, model_id, version)
    if company and snapshot['company_id'] != company_id:
        raise api_error(404, 'enterprise_model_not_found', 'النموذج المؤسسي مش موجود للشركة دي.')
    payload = map_payload(snapshot, company, models, demo=model_id is None)
    payload['context'] = result_context(connection, snapshot)
    payload['decision_outcomes'] = [
        {key: item[key] for key in ('id', 'name', 'version', 'outcome_status', 'outcome_contract', 'accuracy')}
        for item in decision_log(connection, snapshot, pinned=True)['decisions']
        if item['status'] == 'approved'
    ] if model_id is not None else []
    return with_process_flow(connection, payload, snapshot.get('company_id'))


class MapSelection(StrictFields):
    model_id: int | None = Field(default=None, ge=1, lt=2**63, strict=True)
    version: int | None = Field(default=None, ge=1, lt=2**63, strict=True)

    def snapshot(self, connection):
        if self.version is not None and self.model_id is None:
            raise api_error(422, 'version_requires_model', 'اختار نموذج محفوظ عشان تحدد الإصدار.')
        return whatif_snapshot(connection, self.model_id, self.version)


class MapBaseline(MapSelection):
    weeks: int | None = Field(default=None, ge=1, le=104, strict=True)
    include_ledger: bool = False


@router.post('/baseline')
def baseline(body: MapBaseline, request: Request, connection=Depends(request_conn)):
    snapshot = body.snapshot(connection)
    weeks = effective_weeks(snapshot['graph'], body.weeks)
    with impact_errors():
        computed, _ = impact_model(snapshot, request, weeks).baseline()
        metrics = kpis(computed.metrics(), computed.fill_rate)
        if body.include_ledger:
            return {'kpis': metrics, 'weeks': weeks,
                    'cash_ledger': {'period': 'week', 'currency': 'SAR', 'rows': computed.ledger}
                    if computed.ledger is not None else None}
        return metrics


@router.get('/baseline')
def read_baseline(request: Request, model_id: Selection = None, version: Selection = None,
                  weeks: Annotated[int | None, Query(ge=1, le=104)] = None,
                  include_ledger: bool = False, connection=Depends(request_conn)):
    return baseline(MapBaseline(model_id=model_id, version=version, weeks=weeks, include_ledger=include_ledger),
                    request, connection)


class MapGoal(MapSelection):
    goal: decision.Goal


@router.post('/goal-seek')
def goal_seek(body: MapGoal, request: Request, connection=Depends(request_conn)):
    snapshot = body.snapshot(connection)
    with impact_errors():
        result = decision.recommend(impact_model(snapshot, request, body.goal.weeks), body.goal)
    return {'model_id': snapshot['id'], 'version': snapshot['version'],
            'context': result_context(connection, snapshot, body.goal.weeks), **result}
