from fastapi import APIRouter, Depends, Request

from app.access import company_visible

from app.db import audit, encode, request_conn, utc_now
from app.schemas import Company, CompanyCreate, api_error
from app.templates_registry import SectorTemplate, TemplateSummary, load_templates

router = APIRouter()
COMPANY_SELECT = '''
SELECT c.id, c.name, c.template,
       (SELECT id FROM datasets WHERE company_id=c.id AND is_active=1) AS active_dataset_id,
       COALESCE((SELECT MAX(version) FROM company_models WHERE company_id=c.id),
                (SELECT MAX(v.version) FROM enterprise_model_versions v
                 JOIN onboarding_companies o ON o.model_id=v.model_id WHERE o.company_id=c.id)) AS model_version
FROM companies c
'''


def get_template(template_id):
    template = load_templates().get(template_id)
    if template is None:
        raise api_error(404, 'template_not_found', 'قالب القطاع غير موجود.')
    return template


def get_company(connection, company_id):
    company = connection.execute(COMPANY_SELECT + ' WHERE c.id = ?', (company_id,)).fetchone()
    if company is None:
        raise api_error(404, 'company_not_found', 'الشركة غير موجودة.')
    if company['template'] not in load_templates():
        raise api_error(422, 'enterprise_company', 'استخدم تجهيز الشركة والتحليلات المؤسسية للشركة دي.')
    return dict(company)


def insert_company(connection, name, template):
    now = utc_now()
    company_id = connection.execute(
        'INSERT INTO companies (name, template, created_at) VALUES (?, ?, ?)',
        (name, template.template_id, now),
    ).lastrowid
    connection.execute(
        'INSERT INTO company_models (company_id, version, model_json, created_at) VALUES (?, 1, ?, ?)',
        (company_id, encode(template.model_defaults()), now),
    )
    return company_id


@router.get('/templates', response_model=list[TemplateSummary])
def list_templates():
    return list(load_templates().values())


@router.get('/templates/{tid}', response_model=SectorTemplate)
def template_detail(tid: str):
    return get_template(tid)


@router.get('/companies', response_model=list[Company])
def list_companies(request: Request, connection=Depends(request_conn)):
    return [dict(row) for row in connection.execute(COMPANY_SELECT + ' ORDER BY c.id')
            if company_visible(request.state.principal, row['id'])]


@router.post('/companies', response_model=Company, status_code=201)
def create_company(body: CompanyCreate, connection=Depends(request_conn)):
    with connection:
        company_id = insert_company(connection, body.name, get_template(body.template))
        audit(connection, company_id, "company.create", "company", company_id)
        return get_company(connection, company_id)
