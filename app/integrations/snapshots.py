"""Resolve an explicitly company-owned enterprise snapshot."""
from app.enterprise import store
from app.enterprise.living_map import map_payload
from app.enterprise.result_context import result_context
from app.schemas import api_error


def snapshot_for(connection, company_id, model_id=None, version=None):
    if version is not None and model_id is None:
        raise api_error(422, 'version_requires_model', 'اختار نموذج محفوظ عشان تحدد الإصدار.')
    if model_id is None:
        row = connection.execute(
            '''SELECT m.id FROM enterprise_models m
               LEFT JOIN onboarding_companies o ON o.model_id = m.id AND o.company_id = m.company_id
               WHERE m.company_id = ? AND m.archived_at IS NULL
               ORDER BY o.model_id IS NULL, m.id DESC LIMIT 1''', (company_id,),
        ).fetchone()
        if row is None:
            raise api_error(404, 'enterprise_model_not_found', 'الشركة لسه مفيهاش نموذج مؤسسي محفوظ.')
        model_id = row['id']
    if connection.execute(
        'SELECT 1 FROM enterprise_models WHERE id = ? AND company_id = ? AND archived_at IS NULL',
        (model_id, company_id),
    ).fetchone() is None:
        raise api_error(404, 'enterprise_model_not_found', 'النموذج المؤسسي مش موجود للشركة دي.')
    return store.read(connection, model_id, version)


def project_map(connection, company_id, snapshot):
    company = connection.execute('SELECT id, name, template FROM companies WHERE id = ?', (company_id,)).fetchone()
    return {**map_payload(snapshot, dict(company), []), 'context': result_context(connection, snapshot)}
