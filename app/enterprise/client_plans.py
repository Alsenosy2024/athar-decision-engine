"""Resolve immutable, company-scoped approved operational plan inputs."""
import json
from dataclasses import dataclass

from app.schemas import api_error


@dataclass(frozen=True)
class ClientPlan:
    dataset: dict
    parameters: dict
    weights: dict
    schedule: dict
    status: str
    identity: dict


def resolve_client_plans(connection, graph, company_id):
    sources = {}
    for arm in graph.arms:
        if arm.input_source != 'client_plan':
            continue
        row = connection.execute("""
            SELECT p.id AS plan_id, r.id AS run_id, r.status, d.id AS dataset_id,
                   m.id AS model_id, m.version, d.payload_json, m.model_json, r.result_json
            FROM plans p
            JOIN runs r ON r.id = p.run_id AND r.company_id = p.company_id
            JOIN datasets d ON d.id = r.dataset_id AND d.company_id = p.company_id
            JOIN company_models m ON m.id = r.model_id AND m.company_id = p.company_id
            JOIN companies c ON c.id = p.company_id
            WHERE p.id = ? AND p.company_id = ? AND c.template = ? AND p.status = 'approved'
                AND p.variant = 'optimized' AND r.kind = 'optimize' AND d.source = 'upload'
        """, (arm.approved_plan_id, company_id, arm.sector)).fetchone()
        if row is None:
            raise api_error(422, 'invalid_client_plan',
                            'اختار خطة إنتاج معتمدة من بيانات مرفوعة لنفس الشركة.')
        model, result = json.loads(row['model_json']), json.loads(row['result_json'])
        identity = {'company_id': company_id, 'dataset_id': row['dataset_id'],
                    'plan_id': row['plan_id'], 'run_id': row['run_id'],
                    'operating_model_id': row['model_id'], 'operating_model_version': row['version']}
        sources[arm.sector] = ClientPlan(json.loads(row['payload_json']), model['parameters'],
                                         result.get('effective_weights', model['weights']),
                                         result['optimized']['plan'], row['status'], identity)
    return sources

def ledger_edges(graph, links, drivers):
    inputs = {
        'ledger.revenue': ('demand.', 'price.', 'downtime.', 'storage', 'supplier'),
        'ledger.direct_cost': ('demand.', 'downtime.', 'storage', 'supplier'),
        'ledger.expenses': ('demand.', 'downtime.', 'storage', 'supplier'),
        'ledger.profit': ('demand.', 'price.', 'downtime.', 'storage', 'credit', 'cash.opening', 'supplier'),
        'ledger.cash': ('demand.', 'price.', 'downtime.', 'storage', 'credit', 'cash.opening', 'supplier'),
    }
    outputs = {'plan.schedule', 'plan.inventory', 'ledger.revenue', 'ledger.direct_cost', 'ledger.expenses'}
    methods = {}
    for edge in graph.edges:
        target = links.get(edge.target)
        if target is None or target.output not in inputs:
            continue
        source = links.get(edge.source)
        if (edge.source in drivers and edge.source.startswith(inputs[target.output])
                or source is not None and source.output in outputs and source.output != target.output):
            methods[edge.source, edge.target] = 'operating_ledger'
    return methods
