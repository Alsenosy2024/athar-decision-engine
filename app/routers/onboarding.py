"""Progressive company setup and review-before-save endpoints."""
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from pydantic import Field, StringConstraints

from app.access import require_role
from app.connectors import erp
from app.connectors.files import read_tables, upload_bytes
from app.db import request_conn
from app.enterprise.edge_workflow import DEMO_STAMP, materialize
from app.enterprise.schema import Number, StrictFields
from app.onboarding import ingest, storage
from app.onboarding.presets import SECTIONS, SECTORS, graph_for, section_specs
from app.onboarding.records import CashTerms, Sector
from app.routers.connectors import saved_connector
from app.routers.enterprise import excel_response
from app.schemas import CompanyName, api_error

router = APIRouter(tags=['onboarding'])
BASE = '/companies/{company_id}/onboarding'
CompanyID = Annotated[int, Field(ge=1, lt=2**63)]
Section = Annotated[str | None, Query(max_length=30)]


class Create(StrictFields):
    name: CompanyName
    sector: Sector


class ManualRow(StrictFields):
    key: Annotated[str, StringConstraints(max_length=80)]
    value: Number | Annotated[str, StringConstraints(max_length=160)]
    date: Annotated[str, StringConstraints(max_length=30)] | None = None
    source: Annotated[str, StringConstraints(max_length=12)] = 'actual'


class Version(StrictFields):
    expected_version: int = Field(ge=1, strict=True)


class Manual(Version):
    records: list[ManualRow] = Field(min_length=1, max_length=ingest.MAX_ROWS)


class Rules(Version):
    multipliers: dict[str, Annotated[Number, Field(ge=-10, le=10)]] = Field(min_length=1, max_length=20)


class Commit(StrictFields):
    token: Annotated[str, StringConstraints(min_length=43, max_length=43)]


def check_section(section):
    if section is not None and section not in SECTIONS:
        raise api_error(404, 'onboarding_section_not_found', 'القسم ده مش موجود في التجهيز.')
    return section


def review(connection, model, request, records, errors):
    validation = {'ok': not errors, 'errors': errors, 'summary': {'records': len(records)}}
    if errors:
        return {'validation': validation, 'token': None, 'version': model['version'],
                'diff': [], 'rule_diff': []}
    return {'validation': validation, **storage.preview(connection, model, request.state.principal.id, records)}


@router.get('/onboarding/templates')
def templates():
    return [{'id': key, 'name': title} for key, title in SECTORS.items()]


@router.get('/onboarding/templates/{sector}')
def template(sector: Sector):
    return {'id': sector, 'name': SECTORS[sector],
            'graph': materialize(graph_for(sector), DEMO_STAMP), 'sections': section_specs(sector)}


@router.get('/onboarding/templates/{sector}/workbook')
def workbook(sector: Sector, section: Section = None):
    return excel_response(ingest.workbook(sector, check_section(section)),
                          f'athar-{sector}-{section or "company"}.xlsx')


@router.post('/onboarding/companies', status_code=201)
def create(body: Create, request: Request, connection=Depends(request_conn)):
    return storage.create(connection, body, request.state.principal)


@router.get(BASE)
def company(company_id: CompanyID, connection=Depends(request_conn)):
    return storage.describe(storage.snapshot(connection, company_id))


@router.post(BASE + '/sections/{section}/manual/preview')
def manual(company_id: CompanyID, section: str, body: Manual, request: Request, connection=Depends(request_conn)):
    model = storage.snapshot(connection, company_id)
    storage.check_version(model, body.expected_version)
    check_section(section)
    rows = ((section, number, record.model_dump()) for number, record in enumerate(body.records, 1))
    records, errors = ingest.validate(rows, section)
    return review(connection, model, request, records, errors)


@router.post(BASE + '/excel/preview')
def excel(company_id: CompanyID, request: Request, file: Annotated[UploadFile, File()],
          expected_version: Annotated[int, Form(ge=1)], section: Annotated[str | None, Form()] = None,
          connection=Depends(request_conn)):
    model = storage.snapshot(connection, company_id)
    storage.check_version(model, expected_version)
    check_section(section)
    if Path(file.filename or '').suffix.lower() != '.xlsx':
        raise api_error(422, 'onboarding_requires_xlsx', 'اختار ملف Excel بصيغة XLSX.')
    tables = read_tables(file.filename, upload_bytes(file), selected=section, load_rows=True)
    rows, errors = ingest.table_rows(tables, section)
    if errors:
        return review(connection, model, request, [], errors)
    records, errors = ingest.validate(rows, section, allow_template_blanks=True)
    return review(connection, model, request, records, errors)


@router.post(BASE + '/sections/{section}/erp/{connector_id}/preview')
def connector(company_id: CompanyID, section: str, connector_id: int, body: Version, request: Request,
              connection=Depends(request_conn)):
    require_role(request.state.principal)
    model = storage.snapshot(connection, company_id)
    storage.check_version(model, body.expected_version)
    check_section(section)
    saved = saved_connector(connection, company_id, connector_id)
    saved.pop('id')
    config = (erp.RestConfig if saved['kind'] == 'rest' else erp.SqlConfig).model_validate(saved)
    table = erp.pull(config)
    columns = config.mapping
    if len(columns.columns()) != len(set(columns.columns())):
        raise api_error(422, 'invalid_column_mapping', 'اختار عمود مختلف لكل حقل في الموصل.')
    rows = ((section, number, {'key': row.get(columns.node), 'value': row.get(columns.value),
                                'date': row.get(columns.date)})
             for number, values in table.rows if (row := dict(zip(table.headers, values))))
    records, errors = ingest.validate(rows, section, filter_sections=True)
    return review(connection, model, request, records, errors)


@router.post(BASE + '/rules/preview')
def rules(company_id: CompanyID, body: Rules, request: Request, connection=Depends(request_conn)):
    model = storage.snapshot(connection, company_id)
    storage.check_version(model, body.expected_version)
    graph = model['graph']
    allowed = {f'{edge.source}:{edge.target}' for edge in graph.edges if edge.rule}
    if graph.onboarding.sector != 'custom' or not body.multipliers.keys() <= allowed:
        raise api_error(422, 'onboarding_invalid_rules', 'اختار علاقات موجودة في القالب المخصص.')
    return {'validation': {'ok': True, 'errors': [], 'summary': {}},
            **storage.preview(connection, model, request.state.principal.id, rules=body.multipliers)}


@router.post(BASE + '/commit')
def commit(company_id: CompanyID, body: Commit, request: Request, connection=Depends(request_conn)):
    return storage.commit(connection, company_id, request.state.principal.id, body.token)


class CashTermsUpdate(Version):
    terms: CashTerms


@router.put(BASE + '/cash-terms')
def cash_terms(company_id: CompanyID, body: CashTermsUpdate, request: Request, connection=Depends(request_conn)):
    require_role(request.state.principal)
    return storage.update_cash_terms(connection, company_id, body.expected_version, body.terms)
