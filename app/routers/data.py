import json
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, File, UploadFile
from fastapi.responses import JSONResponse

from app.db import audit, encode, request_conn, utc_now
from app.ingestion.loader import load_upload
from app.ingestion.validators import validate_dataset, validate_demo
from app.routers.companies import get_company, get_template
from app.routers.model import current_model
from app.schemas import DatasetDetail, DatasetResult, DatasetSummary, FieldError, ValidationResult, api_error

router = APIRouter()
MAX_UPLOAD_BYTES = 10 * 1024 * 1024


def save_dataset(connection, company_id, name, source, payload, validation):
    connection.execute('UPDATE datasets SET is_active=0 WHERE company_id=? AND is_active=1', (company_id,))
    dataset_id = connection.execute(
        '''INSERT INTO datasets (company_id, name, source, payload_json, validation_json, created_at)
           VALUES (?, ?, ?, ?, ?, ?)''',
        (company_id, name, source, encode(payload), validation.model_dump_json(), utc_now()),
    ).lastrowid
    audit(connection, company_id, f'dataset.{source}', 'dataset', dataset_id, validation.summary)
    return DatasetResult(dataset_id=dataset_id, validation=validation)


def store_validated(connection, company_id, name, source, payload, validation):
    if validation.ok:
        return save_dataset(connection, company_id, name, source, payload, validation)
    return JSONResponse(
        status_code=422,
        content=DatasetResult(dataset_id=None, validation=validation).model_dump(),
    )


@router.post('/companies/{cid}/datasets/demo', response_model=DatasetResult)
def install_demo(cid: int, connection=Depends(request_conn)):
    with connection:
        connection.execute('BEGIN IMMEDIATE')
        company = get_company(connection, cid)
        template = get_template(company['template'])
        payload, validation = validate_demo(template, current_model(connection, company)['parameters'])
        return store_validated(connection, cid, 'البيانات التجريبية', 'demo', payload, validation)


@router.post('/companies/{cid}/datasets/upload', response_model=DatasetResult,
             responses={422: {'model': DatasetResult}})
def upload_dataset(cid: int, file: Annotated[UploadFile, File()], connection=Depends(request_conn)):
    company = get_company(connection, cid)
    template = get_template(company['template'])
    filename = Path((file.filename or '').replace(chr(92), '/')).name
    content = file.file.read(MAX_UPLOAD_BYTES + 1)
    if len(content) > MAX_UPLOAD_BYTES:
        sheets, issues = {}, [FieldError(sheet='الملف', row=0, column='', msg_ar='الحد الأقصى لحجم الملف 10 ميجابايت.')]
    else:
        sheets, issues = load_upload(filename, content, template)

    with connection:
        connection.execute('BEGIN IMMEDIATE')
        if issues and not sheets:
            payload, validation = {}, ValidationResult(ok=False, errors=issues, summary={})
        else:
            payload, validation = validate_dataset(template, sheets, current_model(connection, company)['parameters'])
            if issues:
                validation.errors = issues + validation.errors
                validation.ok = False
        return store_validated(connection, cid, filename, 'upload', payload, validation)


@router.get('/companies/{cid}/datasets', response_model=list[DatasetSummary])
def datasets(cid: int, connection=Depends(request_conn)):
    get_company(connection, cid)
    rows = connection.execute(
        'SELECT id, name, source, is_active, created_at, validation_json FROM datasets WHERE company_id=? ORDER BY id DESC',
        (cid,),
    )
    return [
        {'id': row['id'], 'name': row['name'], 'source': row['source'], 'is_active': row['is_active'],
         'created_at': row['created_at'], 'validation': json.loads(row['validation_json'])}
        for row in rows
    ]


@router.get('/datasets/{did}', response_model=DatasetDetail)
def dataset_detail(did: int, connection=Depends(request_conn)):
    row = connection.execute('SELECT * FROM datasets WHERE id=?', (did,)).fetchone()
    if row is None:
        raise api_error(404, 'dataset_not_found', 'مجموعة البيانات غير موجودة.')
    return {'id': did, 'payload': json.loads(row['payload_json']), 'validation': json.loads(row['validation_json'])}
