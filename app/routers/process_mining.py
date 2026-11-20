"""Company-scoped logs and transient operational simulation."""
from typing import Annotated

from fastapi import APIRouter, Depends, File, Path, Request, UploadFile

from app.access import require_company
from app.db import request_conn
from app.process_mining import store
from app.process_mining.demo import generate_csv
from app.process_mining.discovery import discover
from app.process_mining.events import MAX_BYTES, parse
from app.process_mining.simulation import SimulationRequest, simulate
from app.schemas import api_error

router = APIRouter(tags=['process-mining'])
CompanyID = Annotated[int, Path(ge=1, lt=2**63)]


def company_template(connection, company_id):
    row = connection.execute('SELECT template FROM companies WHERE id = ?', (company_id,)).fetchone()
    if row is None:
        raise api_error(404, 'company_not_found', 'الشركة مش موجودة.')
    return row['template']


@router.get('/companies/{company_id}/process-mining')
def read_report(company_id: CompanyID, connection=Depends(request_conn)):
    company_template(connection, company_id)
    record = store.read(connection, company_id)
    return store.report(record) if record else None


@router.post('/companies/{company_id}/process-mining/import')
def upload_log(company_id: CompanyID, file: Annotated[UploadFile, File()], connection=Depends(request_conn)):
    company_template(connection, company_id)
    events, format_name = parse(file.file.read(MAX_BYTES + 1), file.filename or '')
    return store.report(store.save(connection, company_id, 'actual', format_name, discover(events)))


@router.post('/companies/{company_id}/process-mining/demo')
def demo_log(company_id: CompanyID, connection=Depends(request_conn)):
    template = company_template(connection, company_id)
    events, format_name = parse(generate_csv(template), 'demo.csv')
    profile = discover(events)
    with connection:
        connection.execute('BEGIN IMMEDIATE')
        previous = store.read(connection, company_id)
        if previous and previous['source'] == 'actual':
            raise api_error(409, 'actual_event_log_exists', 'الشركة عندها سجل فعلي بالفعل.')
        return store.report(store.save(connection, company_id, 'synthetic', format_name, profile))


@router.post('/enterprise/simulate')
def run_simulation(body: SimulationRequest, request: Request, connection=Depends(request_conn)):
    require_company(request.state.principal, body.company_id)
    company_template(connection, body.company_id)
    record = store.read(connection, body.company_id)
    if record is None:
        raise api_error(409, 'event_log_required', 'ارفع سجل أحداث أو حمّل سجل الديمو عشان تشغّل المحاكاة.')
    if body.revision is not None and body.revision != record['revision']:
        raise api_error(409, 'event_log_changed', 'سجل الأحداث اتغيّر. حدّث الخريطة وشغّل المحاكاة تاني.')
    return {'company_id': body.company_id, 'revision': record['revision'], 'source': record['source'],
            'seed': body.seed, 'scenario': body.scenario.model_dump(),
            'weeks': simulate(record['profile'], body.weeks, body.scenario, body.seed)}
