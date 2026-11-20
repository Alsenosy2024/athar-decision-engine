import json

from fastapi import APIRouter, Depends

from app.db import audit, encode, request_conn, utc_now
from app.routers.companies import get_company, get_template
from app.schemas import CompanyModel, ModelPatch, api_error

router = APIRouter()


def current_model(connection, company):
    row = connection.execute(
        'SELECT id, version, model_json FROM company_models WHERE company_id=? ORDER BY version DESC LIMIT 1',
        (company['id'],),
    ).fetchone()
    return {'id': row['id'], 'version': row['version'], 'template': company['template'], **json.loads(row['model_json'])}


@router.get('/companies/{cid}/model', response_model=CompanyModel)
def read_model(cid: int, connection=Depends(request_conn)):
    return current_model(connection, get_company(connection, cid))


@router.put('/companies/{cid}/model', response_model=CompanyModel)
def update_model(cid: int, patch: ModelPatch, connection=Depends(request_conn)):
    with connection:
        connection.execute('BEGIN IMMEDIATE')
        company = get_company(connection, cid)
        template = get_template(company['template'])
        model = current_model(connection, company)
        data = {section: model[section] for section in ('parameters', 'constraints', 'weights')}

        params = {item.id: item for item in template.parameters}
        for key, value in (patch.parameters or {}).items():
            if key not in params or not params[key].accepts(value):
                raise api_error(422, 'invalid_parameter', f'قيمة المعامل غير صالحة ({key}).')
            data['parameters'][key] = value

        constraints = {item.id: item for item in template.constraints}
        for key, value in (patch.constraints or {}).items():
            if key not in constraints or not constraints[key].editable:
                raise api_error(422, 'invalid_constraint', f'القيد غير قابل للتعديل ({key}).')
            if constraints[key].kind not in (value, 'hard_soft'):
                raise api_error(422, 'invalid_constraint', f'نوع القيد غير صالح ({key}).')
            data['constraints'][key] = value

        weights = {item.id: item for item in template.objective_weights}
        for key, value in (patch.weights or {}).items():
            if key not in weights or not weights[key].min <= value <= weights[key].max:
                raise api_error(422, 'invalid_weight', f'الوزن خارج النطاق المسموح ({key}).')
            data['weights'][key] = value

        if template.template_id == 'production' and company['active_dataset_id'] is not None:
            horizon = data['parameters'][template.horizon_key]
            dataset = connection.execute('SELECT payload_json FROM datasets WHERE id=?',
                                         (company['active_dataset_id'],)).fetchone()
            if any(order['due_day'] >= horizon for order in json.loads(dataset['payload_json'])['orders']):
                raise api_error(422, 'invalid_horizon', 'أفق التخطيط أقصر من موعد تسليم في البيانات النشطة.')

        version = model['version'] + 1
        try:
            payload = encode(data)
        except ValueError:
            raise api_error(422, 'invalid_parameter', 'تأكد أن القيم الرقمية محدودة.') from None
        model_id = connection.execute(
            'INSERT INTO company_models (company_id, version, model_json, created_at) VALUES (?, ?, ?, ?)',
            (cid, version, payload, utc_now()),
        ).lastrowid
        audit(connection, cid, 'model.update', 'company_model', model_id, {'version': version})
        return {'id': model_id, 'version': version, 'template': template.template_id, **data}
