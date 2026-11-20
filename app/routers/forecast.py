import json

import numpy as np
from fastapi import APIRouter, Depends, Request

from app.db import audit, encode, request_conn, utc_now
from app.decision_loop.contract import forecast_evidence
from app.forecasting.service import run_forecast
from app.learning.recalibrate import list_calibrations
from app.routers.companies import get_company
from app.routers.model import current_model
from app.schemas import ForecastRequest, ForecastResult, api_error

router = APIRouter()


def company_forecast(connection, company, dataset, request: Request, *, calibration_rows=None):
    if calibration_rows is None:
        calibration_rows = list_calibrations(connection, company['id'])
    calibrations = {row['key']: row['value'] for row in calibration_rows}
    try:
        with np.errstate(over='raise', invalid='raise'):
            return run_forecast(
                company['template'], dataset, calibrations,
                engine=request.app.state.settings.forecast_engine,
                foundation=request.app.state.forecast_worker,
            )
    except (FloatingPointError, OverflowError):
        raise api_error(422, 'invalid_forecast_input', 'قيم البيانات أكبر من النطاق المدعوم للتوقعات.') from None


@router.post('/companies/{cid}/forecast', response_model=ForecastResult)
def forecast(cid: int, request: Request, body: ForecastRequest | None = None, connection=Depends(request_conn)):
    company = get_company(connection, cid)
    dataset_id = body.dataset_id if body and body.dataset_id is not None else company['active_dataset_id']
    dataset = connection.execute(
        'SELECT payload_json FROM datasets WHERE id = ? AND company_id = ?', (dataset_id, cid),
    ).fetchone()
    if dataset is None:
        raise api_error(404, 'dataset_not_found', 'مجموعة البيانات غير موجودة للشركة.')

    payload = json.loads(dataset['payload_json'])
    model = current_model(connection, company)
    calibration_rows = list_calibrations(connection, cid)
    cutoff = utc_now()
    result = company_forecast(connection, company, payload, request, calibration_rows=calibration_rows)
    result['decision_evidence'] = forecast_evidence(payload, model, calibration_rows, cutoff)
    with connection:
        forecast_id = connection.execute(
            '''INSERT INTO forecasts (company_id, dataset_id, method, result_json, created_at)
               VALUES (?, ?, ?, ?, ?)''',
            (cid, dataset_id, result['method'], encode(result), utc_now()),
        ).lastrowid
        audit(connection, cid, 'forecast.run', 'forecast', forecast_id, {'dataset_id': dataset_id})
    return {'forecast_id': forecast_id, **result}


@router.get('/companies/{cid}/forecast/latest', response_model=ForecastResult | None)
def latest_forecast(cid: int, allow_missing: bool = False, connection=Depends(request_conn)):
    get_company(connection, cid)
    row = connection.execute(
        'SELECT id, result_json FROM forecasts WHERE company_id = ? ORDER BY id DESC LIMIT 1', (cid,),
    ).fetchone()
    if row is None:
        if allow_missing:
            return None
        raise api_error(404, 'forecast_not_found', 'لا توجد توقعات محفوظة للشركة.')
        audit(connection, cid, 'forecast.run', 'forecast', forecast_id, {'dataset_id': dataset_id})
    return {'forecast_id': row['id'], **json.loads(row['result_json'])}
