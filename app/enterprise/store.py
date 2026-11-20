from app.access import active_principal, company_visible, require_company, require_role
from app.db import audit, encode, utc_now
from app.enterprise.client_plans import resolve_client_plans
from app.enterprise.edge_workflow import materialize, prepare_write
from app.enterprise.schema import EngineLink, Graph, ModelInput, ModelUpdate
from app.ingestion.loader import load_demo
from app.schemas import api_error

SELECT = """
SELECT m.id, m.template, m.company_id, v.name, v.version, v.created_at{graph}
FROM enterprise_models m JOIN enterprise_model_versions v ON m.id = v.model_id
WHERE {where}
"""

LATEST = 'v.version = (SELECT MAX(version) FROM enterprise_model_versions WHERE model_id = m.id)'


# Resolve declared inputs without invoking the solver during graph writes.
ENTITY_TABLES = {
    ('production', 'series'): 'skus',
    ('contracting', 'series'): 'activities',
    ('production', 'plan.schedule'): 'lines',
    ('production', 'plan.inventory'): 'skus',
    ('contracting', 'plan.activities'): 'projects',
}


def validate_link_entities(graph: Graph):
    datasets = {arm.sector: (graph._client_sources[arm.sector].dataset
                            if arm.input_source == 'client_plan' else load_demo(arm.sector)) for arm in graph.arms}
    for node in graph.nodes:
        link = node.definition
        if not isinstance(link, EngineLink) or link.entity_id is None:
            continue
        output = 'series' if link.output.startswith('series.') else link.output
        table = ENTITY_TABLES[link.sector, output]
        if not any(row['id'] == link.entity_id for row in datasets[link.sector][table]):
            raise api_error(422, 'unknown_enterprise_engine_entity',
                            f'العنصر المرتبط بالمحرك مش موجود في بيانات الخطة ({node.id}).')


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
    graph = materialize(Graph.model_validate_json(result.pop('graph_json')), result['created_at'])
    graph._client_sources = resolve_client_plans(connection, graph, result['company_id'])
    result['graph'] = graph
    return result


def list_versions(connection, model_id):
    query = SELECT.format(graph='', where='m.id = ?')
    rows = connection.execute(query + ' ORDER BY v.version DESC', (model_id,)).fetchall()
    if not rows or not company_visible(active_principal.get(), rows[0]['company_id']):
        raise api_error(404, 'enterprise_model_not_found', 'النموذج المؤسسي مش موجود.')
    return [dict(row) for row in rows]


def write_version(connection, model_id, version, body: ModelInput, *, origin='system', review=None, reviewer=None):
    previous = read(connection, model_id, version - 1)['graph'] if version > 1 else None
    timestamp = utc_now()
    graph = prepare_write(body.graph, timestamp, previous=previous, origin=origin, review=review, reviewer=reviewer)
    connection.execute(
        '''INSERT INTO enterprise_model_versions (model_id, version, name, graph_json, created_at)
           VALUES (?, ?, ?, ?, ?)''',
        (model_id, version, body.name, encode(graph.model_dump(mode='json')), timestamp),
    )


def create(connection, body: ModelInput, company_id=None, *, origin='builder'):
    require_company(active_principal.get(), company_id)
    if company_id is not None and not connection.execute(
            'SELECT 1 FROM companies WHERE id = ?', (company_id,)).fetchone():
        raise api_error(404, 'company_not_found', 'الشركة مش موجودة.')
    ensure_builder_scope(body.graph)
    with connection:
        bind_sources(connection, body.graph, company_id)
        validate_link_entities(body.graph)
        model_id = connection.execute(
            "INSERT INTO enterprise_models (template, company_id) VALUES ('enterprise', ?)", (company_id,),
        ).lastrowid
        write_version(connection, model_id, 1, body, origin=origin)
        audit(connection, company_id, 'enterprise.create', 'enterprise_model', model_id, {'version': 1})
        return read(connection, model_id)


def update(connection, model_id, body: ModelUpdate):
    with connection:
        connection.execute('BEGIN IMMEDIATE')
        previous = read(connection, model_id)
        if body.expected_version != previous['version']:
            raise api_error(409, 'enterprise_version_conflict', 'النموذج اتعدّل. حمّل الإصدار الحالي وحاول تاني.')
        ensure_builder_scope(previous['graph'], body.graph)
        bind_sources(connection, body.graph, previous['company_id'], previous['graph'])
        validate_link_entities(body.graph)
        version = previous['version'] + 1
        write_version(connection, model_id, version, body, origin='builder')
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
    if any(graph.company_data is not None for graph in graphs):
        raise api_error(422, 'company_data_managed_model', 'عدّل النموذج من خلال مراجعة بيانات الشركة.')
    if any(graph.onboarding is not None for graph in graphs):
        raise api_error(422, 'onboarding_managed_model', 'عدّل النموذج ده من صفحة تجهيز الشركة.')


def bind_sources(connection, graph, company_id, previous=None):
    pinned = {arm.approved_plan_id for arm in graph.arms if arm.input_source == 'client_plan'}
    old = {arm.approved_plan_id for arm in previous.arms if arm.input_source == 'client_plan'} if previous else set()
    principal = active_principal.get()
    if pinned != old and principal is not None:
        require_role(principal)
    graph._client_sources = resolve_client_plans(connection, graph, company_id)


def approve_edge(connection, model_id, version, source, target, principal):
    require_role(principal, 'approver')
    with connection:
        connection.execute('BEGIN IMMEDIATE')
        snapshot = read(connection, model_id)
        if snapshot['version'] != version:
            raise api_error(409, 'enterprise_version_conflict', 'النموذج اتعدّل. حمّل الإصدار الحالي وحاول تاني.')
        graph = snapshot['graph']
        edges = []
        found = False
        for edge in graph.edges:
            if (edge.source, edge.target) == (source, target):
                if edge.status != 'draft':
                    raise api_error(409, 'edge_already_approved', 'العلاقة معتمدة بالفعل.')
                found = True
                edge = edge.model_copy(update={'status': 'approved'})
            edges.append(edge)
        if not found:
            raise api_error(404, 'edge_not_found', 'العلاقة مش موجودة في الإصدار ده.')
        body = ModelInput(name=snapshot['name'], graph=graph.model_copy(update={'edges': edges}))
        write_version(connection, model_id, version + 1, body, origin='edge_approval', reviewer=principal.username)
        audit(connection, snapshot['company_id'], 'enterprise.edge.approve', 'enterprise_model', model_id,
              {'version': version + 1, 'source': source, 'target': target})
        return read(connection, model_id)
