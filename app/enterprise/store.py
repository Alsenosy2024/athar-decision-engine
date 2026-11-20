from app.access import active_principal, company_visible, require_company
from app.db import audit, encode, utc_now
from app.enterprise.schema import EngineLink, Graph, ModelInput, ModelUpdate
from app.ingestion.loader import load_demo
from app.schemas import api_error

SELECT = """
SELECT m.id, m.template, m.company_id, v.name, v.version, v.created_at{graph}
FROM enterprise_models m JOIN enterprise_model_versions v ON m.id = v.model_id
WHERE {where}
"""

LATEST = 'v.version = (SELECT MAX(version) FROM enterprise_model_versions WHERE model_id = m.id)'


# Resolve only declared demo inputs, with no engine invocation during graph writes.
ENTITY_TABLES = {
    ('production', 'series'): 'skus',
    ('contracting', 'series'): 'activities',
    ('production', 'plan.schedule'): 'lines',
    ('production', 'plan.inventory'): 'skus',
    ('contracting', 'plan.activities'): 'projects',
}


def validate_link_entities(graph: Graph):
    datasets = {arm.sector: load_demo(arm.sector) for arm in graph.arms}
    for node in graph.nodes:
        link = node.definition
        if not isinstance(link, EngineLink) or link.entity_id is None:
            continue
        output = 'series' if link.output.startswith('series.') else link.output
        table = ENTITY_TABLES[link.sector, output]
        if not any(row['id'] == link.entity_id for row in datasets[link.sector][table]):
            raise api_error(422, 'unknown_enterprise_engine_entity',
                            f'العنصر المرتبط بالمحرك مش موجود في بيانات الديمو ({node.id}).')


def list_models(connection):
    query = SELECT.format(graph='', where=f'm.archived_at IS NULL AND {LATEST}')
    principal = active_principal.get()
    return [dict(row) for row in connection.execute(query + ' ORDER BY m.id')
            if company_visible(principal, row['company_id'])]


def read(connection, model_id, version=None):
    condition = f'm.archived_at IS NULL AND {LATEST}' if version is None else 'v.version = ?'
    params = (model_id,) if version is None else (model_id, version)
    query = SELECT.format(graph=', v.graph_json', where=f'm.id = ? AND {condition}')
    row = connection.execute(query, params).fetchone()
    if row is None or not company_visible(active_principal.get(), row['company_id']):
        raise api_error(404, 'enterprise_model_not_found', 'النموذج المؤسسي أو الإصدار مش موجود.')
    result = dict(row)
    result['graph'] = Graph.model_validate_json(result.pop('graph_json'))
    return result


def list_versions(connection, model_id):
    query = SELECT.format(graph='', where='m.id = ?')
    rows = connection.execute(query + ' ORDER BY v.version DESC', (model_id,)).fetchall()
    if not rows or not company_visible(active_principal.get(), rows[0]['company_id']):
        raise api_error(404, 'enterprise_model_not_found', 'النموذج المؤسسي مش موجود.')
    return [dict(row) for row in rows]


def write_version(connection, model_id, version, body: ModelInput):
    connection.execute(
        '''INSERT INTO enterprise_model_versions (model_id, version, name, graph_json, created_at)
           VALUES (?, ?, ?, ?, ?)''',
        (model_id, version, body.name, encode(body.graph.model_dump(mode='json')), utc_now()),
    )


def create(connection, body: ModelInput, company_id=None):
    require_company(active_principal.get(), company_id)
    if company_id is not None and not connection.execute(
            'SELECT 1 FROM companies WHERE id = ?', (company_id,)).fetchone():
        raise api_error(404, 'company_not_found', 'الشركة مش موجودة.')
    ensure_builder_scope(body.graph)
    validate_link_entities(body.graph)
    with connection:
        model_id = connection.execute(
            "INSERT INTO enterprise_models (template, company_id) VALUES ('enterprise', ?)", (company_id,),
        ).lastrowid
        write_version(connection, model_id, 1, body)
        audit(connection, company_id, 'enterprise.create', 'enterprise_model', model_id, {'version': 1})
        return read(connection, model_id)


def update(connection, model_id, body: ModelUpdate):
    with connection:
        connection.execute('BEGIN IMMEDIATE')
        previous = read(connection, model_id)
        if body.expected_version != previous['version']:
            raise api_error(409, 'enterprise_version_conflict', 'النموذج اتعدّل. حمّل الإصدار الحالي وحاول تاني.')
        ensure_builder_scope(previous['graph'], body.graph)
        validate_link_entities(body.graph)
        version = previous['version'] + 1
        write_version(connection, model_id, version, body)
        audit(connection, previous['company_id'], 'enterprise.update', 'enterprise_model', model_id, {'version': version})
        return read(connection, model_id)


def archive(connection, model_id):
    with connection:
        connection.execute('BEGIN IMMEDIATE')
        model = read(connection, model_id)
        ensure_builder_scope(model['graph'])
        updated = connection.execute(
            'UPDATE enterprise_models SET archived_at = ? WHERE id = ? AND archived_at IS NULL',
            (utc_now(), model_id),
        )
        if updated.rowcount != 1:
            raise api_error(404, 'enterprise_model_not_found', 'النموذج المؤسسي مش موجود.')
        audit(connection, model['company_id'], 'enterprise.archive', 'enterprise_model', model_id)


def ensure_builder_scope(*graphs):
    if any(graph.onboarding is not None for graph in graphs):
        raise api_error(422, 'onboarding_managed_model', 'عدّل النموذج ده من صفحة تجهيز الشركة.')
