"""Company import adapter for the standard version-pinned review ledger."""
from fastapi.encoders import jsonable_encoder

from app.company_data import finance, ingest, projection
from app.company_data.schema import COMPANY_TEMPLATE
from app.enterprise import store
from app.enterprise.schema import ModelInput
from app.intelligence import reviews
from app.schemas import api_error


def snapshot(connection, company_id, model_id=None):
    owner = connection.execute('SELECT model_id FROM onboarding_companies WHERE company_id=?',
                               (company_id,)).fetchone()
    if owner is None or model_id is not None and model_id != owner['model_id']:
        raise api_error(404, 'enterprise_model_not_found', 'نموذج الإدخال مش موجود للشركة دي.')
    model = store.read(connection, owner['model_id'])
    if model['graph'].company_data is None:
        raise api_error(422, 'company_import_requires_raw_setup', 'اختار شركة مجهزة للإدخال من البيانات.')
    return model


def is_raw(company):
    return company['template'] == COMPANY_TEMPLATE


def propose(connection, company_id, user, current, contents, raw_mapping, sheet=None):
    data = current['graph'].company_data
    updated, sources, errors, changed = ingest.prepare(
        data, contents, ingest.parse_mappings(raw_mapping), selected_sheet=sheet)
    graph, pending = None, []
    if updated is not None:
        candidate = projection.project(updated)
        existing = {(edge.source, edge.target): edge for edge in current['graph'].edges}
        edges = []
        for edge in candidate.edges:
            prior = existing.get((edge.source, edge.target))
            fields = {'status', 'unit', 'provenance'}
            if prior and prior.model_dump(exclude=fields) == edge.model_dump(exclude=fields):
                edges.append(prior)
            else:
                edge = edge.model_copy(update={'status': 'draft'})
                pending.append(edge)
                edges.append(edge)
        candidate = candidate.model_copy(update={'edges': edges})
        if candidate != current['graph']:
            graph = candidate
    model = ModelInput(name=current['name'], graph=graph) if graph else None
    prior = {ingest.fact_key(item): item.value for item in data.observations}
    measures = {item.key: item for mapping in updated.mappings.values() for item in mapping.measures} if updated else {}
    entities = [entity for identity, entity in updated.entities.items()
                if data.entities.get(identity) != entity] if updated else []
    return reviews.propose(connection, company_id, user, 'entry', current, jsonable_encoder({
        'company_import': True, 'provider': 'rules', 'sources': sources,
        'validation': {'ok': not errors, 'errors': errors, 'summary': {
            'observations': len(changed), 'entities': len(entities),
            'nodes': len(graph.nodes) if graph else 0, 'edges': len(graph.edges) if graph else 0,
        }},
        'model': model, 'entities': entities,
        'diff': [{
            'node_id': item.measure, 'name_ar': measures[item.measure].name, 'unit': measures[item.measure].unit,
            'date': item.day, 'before': prior.get(ingest.fact_key(item)), 'value': item.value,
            'source': f'{item.source.file} / {item.source.sheet}', 'evidence': item.source,
            'entities': [updated.entities[key].name for key in item.entities],
        } for item in changed],
        'relations': [{'edge': edge, 'kind': 'aggregation' if edge.evidence == finance.ROLLED_UP else 'accounting',
                       'source': edge.evidence, 'confidence': 1} for edge in pending],
        'estimates': [], 'schedules': [], 'duplicates': [], 'issues': [], 'unmapped': [],
    }))


def approve(connection, company_id, review, current, estimates, reviewer):
    payload = review['data']
    if not payload['model']:
        raise api_error(422, 'ai_empty_diff', 'مفيش تغييرات صالحة للاعتماد.')
    if estimates or current is None or current['graph'].company_data is None:
        raise api_error(422, 'invalid_company_import', 'اعتمد القيم المراجعة على نموذج إدخال الشركة.')
    model = ModelInput.model_validate(payload['model'])
    graph = model.graph.model_copy(update={'edges': [
        edge.model_copy(update={'status': 'approved'}) for edge in model.graph.edges
    ]})
    return reviews.write_model(connection, company_id, current, model.model_copy(update={'graph': graph}),
                               review, reviewer)
