import json
from typing import Literal

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from app.db import audit, request_conn
from app.delivery import approvals
from app.delivery.export import FORMATS, render_export
from app.routers.companies import get_company
from app.schemas import api_error

router = APIRouter()


class PlanCreate(BaseModel):
    model_config = ConfigDict(extra='forbid')
    run_id: int = Field(gt=0)
    variant: Literal['optimized', 'scenario']


class PlanDecision(BaseModel):
    model_config = ConfigDict(extra='forbid')
    decision: Literal['approved', 'rejected']
    comment: str = Field(max_length=2000)


@router.post('/companies/{cid}/plans')
def create_plan(cid: int, body: PlanCreate, connection=Depends(request_conn)):
    get_company(connection, cid)
    return approvals.create_plan(connection, cid, body.run_id, body.variant)


@router.get('/companies/{cid}/plans')
def list_plans(cid: int, connection=Depends(request_conn)):
    get_company(connection, cid)
    return approvals.list_plans(connection, cid)


@router.post('/plans/{pid}/decide')
def decide_plan(pid: int, body: PlanDecision, request: Request, connection=Depends(request_conn)):
    return approvals.decide_plan(connection, pid, body.decision, body.comment, request.state.principal)


@router.get('/plans/{pid}/export.{fmt}')
def export_plan(pid: int, fmt: str, connection=Depends(request_conn)):
    if fmt not in FORMATS:
        raise api_error(404, 'format_not_found', 'صيغة التصدير غير موجودة.')
    plan = approvals.get_plan(connection, pid)
    company = get_company(connection, plan['company_id'])
    template = company['template']
    if fmt.startswith('p6.') and template != 'contracting':
        raise api_error(404, 'p6_not_supported', 'تصدير بريمافيرا متاح لقالب المقاولات فقط.')
    approvals.require_approved(plan)
    output, kpis, dataset, parameters = approvals.plan_snapshot(connection, plan, template)
    content = render_export(fmt, plan, template, output, kpis, dataset, parameters)
    with connection:
        audit(connection, company['id'], f'plan.export.{fmt}', 'plan', pid, {'format': fmt})
    return Response(content, media_type=FORMATS[fmt], headers={
        'Content-Disposition': f'attachment; filename="plan-{pid}.{fmt}"',
    })


@router.get('/companies/{cid}/audit')
def audit_log(cid: int, connection=Depends(request_conn)):
    if not connection.execute('SELECT 1 FROM companies WHERE id=?', (cid,)).fetchone():
        raise api_error(404, 'company_not_found', 'الشركة غير موجودة.')
    rows = connection.execute(
        """SELECT ts, actor, user_id, action, entity, entity_id, details_json
           FROM audit_log WHERE company_id = ? ORDER BY id DESC""", (cid,),
    )
    return [
        {'ts': row['ts'], 'actor': row['actor'], 'user_id': row['user_id'], 'action': row['action'], 'entity': row['entity'],
         'entity_id': row['entity_id'], 'details': json.loads(row['details_json'] or 'null')}
        for row in rows
    ]
