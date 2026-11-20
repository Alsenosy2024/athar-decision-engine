from fastapi import APIRouter, Depends, Form, UploadFile

from app.db import request_conn
from app.delivery import approvals
from app.learning import actuals, recalibrate
from app.routers.companies import get_company
from app.schemas import api_error

router = APIRouter()


@router.post('/companies/{cid}/actuals/upload')
def upload_actuals(cid: int, file: UploadFile, plan_id: int = Form(gt=0),
                   connection=Depends(request_conn)):
    company = get_company(connection, cid)
    plan = approvals.get_plan(connection, plan_id, company_id=cid)
    approvals.require_approved(plan)
    if not (file.filename or '').lower().endswith('.csv'):
        raise api_error(422, 'invalid_actuals_filename', 'ارفع ملف البيانات الفعلية بصيغة CSV.')
    content = file.file.read(actuals.MAX_UPLOAD_BYTES + 1)
    if len(content) > actuals.MAX_UPLOAD_BYTES:
        raise api_error(422, 'actuals_file_too_large', 'حجم ملف البيانات الفعلية أكبر من 2 ميغابايت.')
    schedule, _, dataset, _ = approvals.plan_snapshot(connection, plan, company['template'])
    return actuals.save_actuals(connection, plan, company['template'], schedule, dataset, content)


@router.get('/companies/{cid}/actuals/latest')
def latest_actuals(cid: int, allow_missing: bool = False, connection=Depends(request_conn)):
    get_company(connection, cid)
    return actuals.latest_actuals(connection, cid, allow_missing=allow_missing)


@router.post('/companies/{cid}/recalibrate')
def update_calibrations(cid: int, connection=Depends(request_conn)):
    with connection:
        connection.execute('BEGIN IMMEDIATE')
        return recalibrate.recalibrate(connection, get_company(connection, cid))


@router.get('/companies/{cid}/calibrations')
def list_calibrations(cid: int, connection=Depends(request_conn)):
    get_company(connection, cid)
    return recalibrate.list_calibrations(connection, cid)
