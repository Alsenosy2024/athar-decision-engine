import json
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import Response
from pydantic import Field, ValidationError

from app.access import active_principal, require_company, require_role
from app.company_data import workflow as company_import
from app.connectors import erp, mapping, ownership, p6
from app.connectors.files import read_tables, upload_bytes
from app.db import audit, encode, request_conn, utc_now
from app.enterprise import store
from app.enterprise.schema import ModelSnapshot, StrictFields
from app.routers.companies import get_company
from app.schemas import api_error

router = APIRouter()
Upload = Annotated[UploadFile, File()]


@router.post('/enterprise/imports/inspect')
def inspect_file(file: Upload):
    tables = read_tables(file.filename, upload_bytes(file))
    return {'sheets': [{'name': name, 'columns': list(dict.fromkeys(
        header for header in sheet.headers if header))} for name, sheet in tables.items()]}


@router.post('/enterprise/models/{model_id}/imports/preview')
def preview_file(model_id: int, file: Upload, sheet: Annotated[str, Form()],
                 columns: Annotated[str, Form()], connection=Depends(request_conn)):
    snapshot = store.read(connection, model_id)
    try:
        column_mapping = mapping.ColumnMapping.model_validate_json(columns)
    except ValidationError:
        raise api_error(422, 'invalid_column_mapping', 'راجع ربط الأعمدة بالحقول المطلوبة.') from None
    tables = read_tables(file.filename, upload_bytes(file), selected=sheet)
    if sheet not in tables:
        raise api_error(422, 'unknown_sheet', 'الورقة المختارة مش موجودة في الملف.')
    return mapping.preview(snapshot, sheet, tables[sheet], column_mapping)


@router.post('/enterprise/models/{model_id}/imports/apply', response_model=ModelSnapshot)
def apply_import(model_id: int, body: mapping.ApplyRequest, connection=Depends(request_conn)):
    return mapping.apply(connection, model_id, store.read(connection, model_id), body)


def saved_connector(connection, company_id, connector_id):
    if company_id is None:
        raise api_error(404, 'erp_connector_not_found', 'موصل ERP محتاج نموذج مرتبط بالشركة.')
    require_company(active_principal.get(), company_id)
    row = connection.execute('SELECT id, config_json FROM erp_connectors WHERE company_id=? AND id=?',
                             (company_id, connector_id)).fetchone()
    if row is None:
        raise api_error(404, 'erp_connector_not_found', 'موصل ERP مش موجود للشركة دي.')
    return {'id': row['id'], **json.loads(row['config_json'])}


def require_existing_company(connection, company_id):
    if not connection.execute('SELECT 1 FROM companies WHERE id=?', (company_id,)).fetchone():
        raise api_error(404, 'company_not_found', 'الشركة غير موجودة.')


ERP_PATH = '/companies/{company_id}/connectors/erp'


@router.get(ERP_PATH)
def connectors(company_id: int, connection=Depends(request_conn)):
    require_existing_company(connection, company_id)
    return [{'id': row['id'], **json.loads(row['config_json'])}
            for row in connection.execute(
                'SELECT id, config_json FROM erp_connectors WHERE company_id=? ORDER BY id', (company_id,))]


@router.get('/erp/legacy-connectors')
def legacy_connectors(request: Request, connection=Depends(request_conn)):
    require_role(request.state.principal)
    return ownership.legacy_configs(connection)


@router.post(ERP_PATH + '/{connector_id}/claim')
def claim_connector(company_id: int, connector_id: int, request: Request, connection=Depends(request_conn)):
    require_role(request.state.principal)
    require_existing_company(connection, company_id)
    return ownership.claim(connection, company_id, connector_id)


@router.post(ERP_PATH, status_code=201)
def add_connector(company_id: int, body: erp.ConnectorConfig, request: Request, connection=Depends(request_conn)):
    require_role(request.state.principal)
    require_existing_company(connection, company_id)
    with connection:
        connector_id = connection.execute(
            'INSERT INTO erp_connectors (company_id, config_json, created_at) VALUES (?, ?, ?)',
            (company_id, body.model_dump_json(), utc_now())).lastrowid
        audit(connection, company_id, 'erp.create', 'erp_connector', connector_id)
    return saved_connector(connection, company_id, connector_id)


@router.put(ERP_PATH + '/{connector_id}')
def update_connector(company_id: int, connector_id: int, body: erp.ConnectorConfig, request: Request,
                     connection=Depends(request_conn)):
    require_role(request.state.principal)
    with connection:
        updated = connection.execute('UPDATE erp_connectors SET config_json=? WHERE company_id=? AND id=?',
                                     (body.model_dump_json(), company_id, connector_id))
        if updated.rowcount != 1:
            raise api_error(404, 'erp_connector_not_found', 'موصل ERP مش موجود للشركة دي.')
        audit(connection, company_id, 'erp.update', 'erp_connector', connector_id)
    return {'id': connector_id, **body.model_dump()}


class ErpCompanyEntry(StrictFields):
    model_id: int = Field(ge=1, lt=2**63)
    expected_version: int = Field(ge=1, strict=True)
    company_mapping: str = Field(default='{}', max_length=64000)


@router.post(ERP_PATH + '/{connector_id}/entry', status_code=201)
def company_entry(company_id: int, connector_id: int, body: ErpCompanyEntry, request: Request,
                  connection=Depends(request_conn)):
    current = company_import.snapshot(connection, company_id, body.model_id)
    if current['version'] != body.expected_version:
        raise api_error(409, 'ai_stale_review', 'إصدار النموذج اتغيّر. اعمل معاينة جديدة.')
    saved = saved_connector(connection, company_id, connector_id)
    saved.pop('id')
    config = ownership.config_adapter.validate_python(saved)
    source = ownership.entry_file(connector_id, config)
    return company_import.propose(connection, company_id, request.state.principal, current, [source],
                                  body.company_mapping)


@router.post('/enterprise/models/{model_id}/imports/erp/{connector_id}/preview')
def preview_erp(model_id: int, connector_id: int, connection=Depends(request_conn)):
    snapshot = store.read(connection, model_id)
    saved = saved_connector(connection, snapshot['company_id'], connector_id)
    if snapshot['graph'].company_data is not None:
        store.ensure_builder_scope(snapshot['graph'])
    saved.pop('id')
    config = (erp.RestConfig if saved['kind'] == 'rest' else erp.SqlConfig).model_validate(saved)
    return mapping.preview(snapshot, config.name, erp.pull(config), config.mapping)


def contracting_company(connection, company_id):
    if get_company(connection, company_id)['template'] != 'contracting':
        raise api_error(422, 'p6_requires_contracting', 'ملفات P6 متاحة لشركات المقاولات.')


@router.get('/companies/{company_id}/p6-imports')
def p6_imports(company_id: int, connection=Depends(request_conn)):
    contracting_company(connection, company_id)
    return [dict(row) for row in connection.execute(
        'SELECT id, filename, created_at FROM p6_imports WHERE company_id=? ORDER BY id DESC', (company_id,))]


@router.post('/companies/{company_id}/p6-imports')
def import_p6(company_id: int, file: Upload, commit: Annotated[bool, Form()] = False,
              connection=Depends(request_conn)):
    contracting_company(connection, company_id)
    contents = upload_bytes(file)
    schedule, validation = p6.read_p6(contents)
    import_id = None
    if commit and validation.ok:
        filename = Path((file.filename or '').replace(chr(92), '/')).name[:200] or 'primavera.xml'
        with connection:
            import_id = connection.execute(
                'INSERT INTO p6_imports (company_id, filename, schedule_json, xml, created_at) VALUES (?, ?, ?, ?, ?)',
                (company_id, filename, encode(schedule), contents, utc_now()),
            ).lastrowid
            audit(connection, company_id, 'p6.import', 'p6_import', import_id, validation.summary)
    return {'id': import_id, 'validation': validation, 'schedule': schedule}


def p6_record(connection, company_id, import_id, columns):
    row = connection.execute(f'SELECT {columns} FROM p6_imports WHERE company_id=? AND id=?',
                             (company_id, import_id)).fetchone()
    if row is None:
        raise api_error(404, 'p6_import_not_found', 'ملف P6 المحفوظ مش موجود.')
    return row


@router.get('/companies/{company_id}/p6-imports/{import_id}')
def p6_detail(company_id: int, import_id: int, connection=Depends(request_conn)):
    row = p6_record(connection, company_id, import_id, 'id, filename, schedule_json')
    return {'id': row['id'], 'filename': row['filename'], 'schedule': json.loads(row['schedule_json'])}


@router.get('/companies/{company_id}/p6-imports/{import_id}/export.p6.xml')
def p6_export(company_id: int, import_id: int, connection=Depends(request_conn)):
    row = p6_record(connection, company_id, import_id, 'xml')
    return Response(row['xml'], media_type='application/xml', headers={
        'Content-Disposition': f'attachment; filename="primavera-{import_id}.xml"',
        'X-Content-Type-Options': 'nosniff',
    })
