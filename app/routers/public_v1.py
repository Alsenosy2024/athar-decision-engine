"""Versioned REST access for server-to-server integrations."""
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query, Request, Response, Security
from fastapi.encoders import jsonable_encoder
from fastapi.openapi.utils import get_openapi
from fastapi.security import APIKeyHeader
from pydantic import Field

from app.db import request_conn
from app.enterprise.impact_graph import WhatIfRequest
from app.enterprise.result_context import effective_weeks, result_context
from app.enterprise.scenarios import kpis
from app.integrations import contracts, credentials
from app.integrations.snapshots import project_map, snapshot_for
from app.routers.enterprise import impact_errors, impact_model, requested_weeks
from app.routers.living_map import MapSelection
from app.schemas import api_error

PREFIX = '/api/v1'
docs = APIRouter(prefix=PREFIX, include_in_schema=False)
router = APIRouter(prefix=PREFIX, tags=['Athar v1'], responses={
    code: {'model': contracts.ApiError, 'description': description}
    for code, description in [(401, 'Missing, invalid or revoked API key'), (403, 'Missing scope or wrong company'),
                              (404, 'Enterprise model or version unavailable'), (422, 'Invalid request')]
})
company_route = '/companies/{company_id}'
CompanyId = Annotated[int, Path(ge=1, lt=2**63)]
Selection = Annotated[int | None, Query(ge=1, lt=2**63)]
Weeks = Annotated[int | None, Query(ge=1, le=104)]
key_header = APIKeyHeader(name='X-Athar-API-Key', scheme_name='AtharApiKey', auto_error=False,
                          description='Server-side key issued by an Athar administrator for one company.')


def require_key(scope: credentials.Scope):
    def check(company_id: CompanyId, response: Response, token=Security(key_header),
              connection=Depends(request_conn)):
        key = credentials.authenticate(connection, token, scope)
        if key['company_id'] != company_id:
            raise api_error(403, 'wrong_company', 'المفتاح مربوط بشركة تانية.')
        response.headers['Cache-Control'] = 'no-store'
        return key
    return check


map_key = require_key('map:read')


@router.get(company_route + '/map', response_model=contracts.MapResult, response_model_exclude_unset=True, dependencies=[Depends(map_key)])
def get_map(company_id: CompanyId, model_id: Selection = None, version: Selection = None,
            connection=Depends(request_conn)):
    """Return the saved map, including baseline indicators, departments and roads. Requires map:read."""
    return project_map(connection, company_id, snapshot_for(connection, company_id, model_id, version))


@router.get(company_route + '/kpis', response_model=contracts.KpiResult, response_model_exclude_unset=True,
            dependencies=[Depends(require_key('kpis:read'))])
def get_kpis(company_id: CompanyId, request: Request, model_id: Selection = None,
             version: Selection = None, weeks: Weeks = None, connection=Depends(request_conn)):
    """Calculate baseline KPIs and available cash movements for the requested horizon. Requires kpis:read."""
    snapshot = snapshot_for(connection, company_id, model_id, version)
    weeks = effective_weeks(snapshot['graph'], weeks)
    with impact_errors():
        baseline, _ = impact_model(snapshot, request, weeks).baseline()
        metrics = kpis(baseline.metrics(), baseline.fill_rate)
    context = result_context(connection, snapshot, weeks)
    return {'company_id': company_id, 'model_id': snapshot['id'], 'version': snapshot['version'],
            'weeks': context['calculation_weeks'], 'context': context, 'kpis': metrics,
            **({'cash_ledger': {'period': 'week', 'currency': 'SAR', 'rows': baseline.ledger}}
               if baseline.ledger is not None else {})}


class PublicWhatIf(WhatIfRequest):
    model_id: int | None = Field(default=None, ge=1, lt=2**63, strict=True)
    version: int | None = Field(default=None, ge=1, lt=2**63, strict=True)


@router.post(company_route + '/whatif', response_model=contracts.WhatIfResult, response_model_exclude_unset=True,
             dependencies=[Depends(require_key('whatif:run'))])
def whatif(company_id: CompanyId, body: PublicWhatIf, request: Request, connection=Depends(request_conn)):
    """Evaluate a transient scenario with the enterprise impact engine. Requires whatif:run."""
    snapshot = snapshot_for(connection, company_id, body.model_id, body.version)
    weeks = requested_weeks(snapshot, body)
    with impact_errors():
        result = impact_model(snapshot, request, weeks).scenario(body)
    if 'onboarding' in result:
        result['onboarding'] = jsonable_encoder(result['onboarding'])
    return {'company_id': company_id, 'model_id': snapshot['id'], 'version': snapshot['version'],
            'name': snapshot['name'], 'context': result_context(connection, snapshot, weeks), **result}


class EmbedRequest(MapSelection):
    expires_in: int = Field(default=credentials.MAX_EMBED_SECONDS, ge=1,
                            le=credentials.MAX_EMBED_SECONDS, strict=True)


@router.post(company_route + '/embed-token', response_model=contracts.EmbedLink, status_code=201)
def issue_embed(company_id: CompanyId, body: EmbedRequest, request: Request, key=Depends(map_key),
                connection=Depends(request_conn)):
    """Create a short-lived iframe link pinned to one map version. Requires map:read."""
    snapshot = snapshot_for(connection, company_id, body.model_id, body.version)
    token, expiry = credentials.sign_embed(
        request.app.state.embed_secret, key, snapshot['id'], snapshot['version'], body.expires_in,
    )
    path = request.url_for('embedded_map').path
    return {'path': f'{path}?token={token}', 'expires_at': expiry}


@docs.get('/openapi.json')
def openapi(request: Request):
    paths = [route for route in request.app.routes if route.path.startswith(PREFIX + '/')]
    schema = get_openapi(title='Athar Integration API', version='1.0.0', routes=paths,
                         description='Company-scoped KPI, map, transient what-if and embedded map access.')
    root_path = request.scope.get('root_path', '').rstrip('/')
    if root_path:
        schema['servers'] = [{'url': root_path}]
    return schema
