import json
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, UploadFile
from fastapi.responses import Response
from pydantic import ValidationError

from app.connectors import erp, mapping, p6
from app.connectors.files import read_tables, upload_bytes
from app.db import audit, encode, request_conn, utc_now
from app.enterprise import store
from app.enterprise.schema import ModelSnapshot
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


def saved_connector(connection, connector_id):
    row = connection.execute('SELECT id, config_json FROM erp_connectors WHERE id=?', (connector_id,)).fetchone()
    if row is None:
        raise api_error(404, 'erp_connector_not_found', 'موصل ERP مش موجود.')
    return {'id': row['id'], **json.loads(row['config_json'])}


@router.get('/connectors/erp')
def connectors(connection=Depends(request_conn)):
    return [{'id': row['id'], **json.loads(row['config_json'])}
            for row in connection.execute('SELECT id, config_json FROM erp_connectors ORDER BY id')]


@router.post('/connectors/erp', status_code=201)
def add_connector(body: erp.ConnectorConfig, connection=Depends(request_conn)):
    with connection:
        connector_id = connection.execute('INSERT INTO erp_connectors (config_json, created_at) VALUES (?, ?)',
                                          (body.model_dump_json(), utc_now())).lastrowid
        audit(connection, None, 'erp.create', 'erp_connector', connector_id)
    return saved_connector(connection, connector_id)


@router.put('/connectors/erp/{connector_id}')
def update_connector(connector_id: int, body: erp.ConnectorConfig, connection=Depends(request_conn)):
    with connection:
        updated = connection.execute('UPDATE erp_connectors SET config_json=? WHERE id=?',
                                     (body.model_dump_json(), connector_id))
        if updated.rowcount != 1:
            raise api_error(404, 'erp_connector_not_found', 'موصل ERP مش موجود.')
        audit(connection, None, 'erp.update', 'erp_connector', connector_id)
    return {'id': connector_id, **body.model_dump()}


@router.post('/enterprise/models/{model_id}/imports/erp/{connector_id}/preview')
def preview_erp(model_id: int, connector_id: int, connection=Depends(request_conn)):
    snapshot = store.read(connection, model_id)
    saved = saved_connector(connection, connector_id)
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
