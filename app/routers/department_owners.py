"""Scoped data-owner entry and administrator review endpoints."""
from dataclasses import replace
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import Field, StringConstraints

from app.access import request_principal, require_role
from app.config import STATIC_DIR
from app.connectors.files import read_tables, upload_bytes
from app.db import request_conn
from app.department_owners import extract, workflow
from app.enterprise.schema import StrictFields
from app.intelligence import documents
from app.onboarding import ingest, storage
from app.onboarding.presets import section_specs
from app.routers.enterprise import excel_response
from app.routers.onboarding import Commit, CompanyID, Manual
from app.schemas import api_error

page = APIRouter()
router = APIRouter(prefix='/department-owners', tags=['department owners'])
COMPANY = '/companies/{company_id}'
SECTION = COMPANY + '/sections/{section}'
Id = Annotated[int, Field(gt=0, lt=2**63)]


class Assignment(StrictFields):
    user_id: Annotated[int, Field(gt=0, lt=2**63, strict=True)] | None


class Decision(StrictFields):
    decision: Literal['approved', 'rejected']
    comment: Annotated[str, StringConstraints(strip_whitespace=True, max_length=500)] = ''


def admin(request):
    principal = request.state.principal
    require_role(principal)
    return principal


def own_model(connection, principal, company_id, section, version=None):
    require_role(principal, 'planner')
    workflow.ensure_owner(connection, company_id, section, principal.id)
    model = storage.snapshot(connection, company_id)
    if version is not None:
        storage.check_version(model, version)
    return model


@page.get('/department-owners', include_in_schema=False)
def workspace_page(request: Request):
    principal = request_principal(request)
    headers = {'Cache-Control': 'no-store'}
    if principal is None:
        return RedirectResponse('/login', headers=headers)
    if principal.role != 'admin' and not principal.department_scoped:
        return RedirectResponse('/', headers=headers)
    return FileResponse(STATIC_DIR / 'department-owners.html', headers=headers)


@router.get('/workspace')
def workspace(request: Request, connection=Depends(request_conn)):
    return workflow.workspace(connection, request.state.principal)


@router.get(COMPANY)
def admin_company(company_id: CompanyID, request: Request, connection=Depends(request_conn)):
    admin(request)
    connection.execute('BEGIN')
    model = storage.snapshot(connection, company_id)
    owners = workflow.assignments(connection, company_id)
    specs = {spec['id']: spec for spec in section_specs(model['graph'].onboarding.sector)}
    sections = [
        item | {'owner': owners.get(item['id']), 'fields': specs[item['id']]['fields']}
        for item in workflow.section_status(connection, model)
    ]
    planners = connection.execute("""
        SELECT u.id, u.username FROM users u JOIN user_companies g ON g.user_id=u.id
        WHERE u.role='planner' AND g.company_id=? ORDER BY u.username
    """, (company_id,))
    return {'name': model['name'], 'version': model['version'], 'sections': sections,
            'reminders': [{'section': item['id'], 'reason': item['reminder']}
                          for item in sections if item['reminder']],
            'planners': [dict(row) for row in planners], 'submissions': workflow.submissions(connection, company_id)}


@router.put(SECTION + '/assignment')
def assignment(company_id: CompanyID, section: str, body: Assignment, request: Request,
               connection=Depends(request_conn)):
    return workflow.assign(connection, company_id, section, body.user_id, admin(request))


@router.get(SECTION)
def owner_section(company_id: CompanyID, section: str, request: Request, connection=Depends(request_conn)):
    principal = request.state.principal
    connection.execute('BEGIN')
    model = own_model(connection, principal, company_id, section)
    graph = model['graph']
    status, = workflow.section_status(connection, model, section)
    spec = next(item for item in section_specs(graph.onboarding.sector) if item['id'] == section)
    return {'company': model['name'], 'version': model['version'], 'spec': spec, 'readiness': status,
            'records': list(workflow.records_for(graph, section).values()),
            'submissions': workflow.submissions(connection, company_id, section, principal.id)}


@router.get(SECTION + '/workbook')
def workbook(company_id: CompanyID, section: str, request: Request, connection=Depends(request_conn)):
    model = own_model(connection, request.state.principal, company_id, section)
    sector = model['graph'].onboarding.sector
    return excel_response(ingest.workbook(sector, section), f'athar-{sector}-{section}.xlsx')


@router.post(SECTION + '/manual/preview')
def manual(company_id: CompanyID, section: str, body: Manual, request: Request,
           connection=Depends(request_conn)):
    principal = request.state.principal
    model = own_model(connection, principal, company_id, section, body.expected_version)
    rows = ((section, i, record.model_dump()) for i, record in enumerate(body.records, 1))
    records, errors = ingest.validate(rows, section)
    return workflow.make_preview(connection, model, section, principal.id, records, errors)


@router.post(SECTION + '/excel/preview')
def excel(company_id: CompanyID, section: str, request: Request, file: Annotated[UploadFile, File()],
          expected_version: Annotated[int, Form(ge=1)], connection=Depends(request_conn)):
    principal = request.state.principal
    model = own_model(connection, principal, company_id, section, expected_version)
    if Path(file.filename or '').suffix.lower() != '.xlsx':
        raise api_error(422, 'onboarding_requires_xlsx', 'اختار ملف Excel بصيغة XLSX.')
    tables = read_tables(file.filename, upload_bytes(file), selected=section, load_rows=True)
    rows, errors = ingest.table_rows(tables, section)
    records = []
    if not errors:
        records, errors = ingest.validate(rows, section, allow_template_blanks=True)
    return workflow.make_preview(connection, model, section, principal.id, records, errors)


@router.post(SECTION + '/extract/preview')
def extract_preview(company_id: CompanyID, section: str, request: Request, file: Annotated[UploadFile, File()],
                    expected_version: Annotated[int, Form(ge=1)],
                    remote: Annotated[bool, Form()] = False, connection=Depends(request_conn)):
    principal = request.state.principal
    model = own_model(connection, principal, company_id, section, expected_version)
    rows, _ = documents.read_documents([(file.filename or '', upload_bytes(file))], '')
    settings = request.app.state.settings
    if not remote or settings.llm_provider != 'codex':
        settings = replace(settings, llm_provider='rules')
    records, errors, evidence, issues = extract.extract(rows, model, section, settings)
    return workflow.make_preview(connection, model, section, principal.id, records, errors,
                                 evidence=evidence, issues=issues)


@router.post(SECTION + '/submit', status_code=201)
def submit(company_id: CompanyID, section: str, body: Commit, request: Request,
           connection=Depends(request_conn)):
    return workflow.submit(connection, company_id, section, request.state.principal.id, body.token)


@router.post(COMPANY + '/submissions/{submission_id}/decide')
def decide(company_id: CompanyID, submission_id: Id, body: Decision, request: Request,
           connection=Depends(request_conn)):
    return workflow.decide(connection, company_id, submission_id, body.decision, admin(request).id, body.comment)
