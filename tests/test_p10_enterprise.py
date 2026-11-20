from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy

import pytest
from fastapi.testclient import TestClient

from conftest import sign_in

from app.config import Settings
from app.db import get_conn
from app.enterprise.schema import NodeType
from app.forecasting.service import run_forecast
from app.ingestion.loader import load_demo
from app.main import create_app
from app.optimization import contracting, production
from app.templates_registry import load_templates

BASE = '/api/enterprise'
MODELS = BASE + '/models'


@pytest.fixture
def demo(client):
    response = client.get(BASE + '/templates/enterprise')
    assert response.status_code == 200
    return response.json()


@pytest.fixture
def model_body(demo):
    return {key: demo[key] for key in ('name', 'graph')}


def test_demo_reuses_both_arms_and_one_financial_pool(demo):
    graph = demo['graph']
    nodes = {node['id']: node for node in graph['nodes']}
    edges = {(edge['source'], edge['target']) for edge in graph['edges']}
    assert demo['template'] == 'enterprise'
    assert graph['arms'] == [
        {'sector': 'production', 'input_source': 'v1_demo'},
        {'sector': 'contracting', 'input_source': 'v1_demo'},
    ]
    assert {node['type'] for node in nodes.values()} == set(NodeType)
    assert [node['id'] for node in nodes.values() if node['type'] == 'cash'] == ['cash']
    assert [node['id'] for node in nodes.values() if node['type'] == 'credit'] == ['credit']
    params = {sector: template.model_defaults()['parameters'] for sector, template in load_templates().items()}
    assert nodes['cash']['baseline']['value'] == params['production']['opening_cash']
    assert nodes['credit']['baseline']['value'] == 1000 * params['contracting']['credit_limit']
    assert nodes['credit']['unit'] == 'SAR'
    assert nodes['pnl.result']['baseline']['value'] is None
    assert ('financing.draw', 'cash') in edges and ('cash', 'financing.draw') in edges

    for row in load_demo('production')['lines'] + load_demo('contracting')['projects']:
        key = ('line.' if row['id'].startswith('L') else 'project.') + row['id']
        assert nodes[key]['name_ar'] == row['name_ar']
        assert ('cash', key) in edges and ('credit', key) in edges

    data = load_demo('production')
    prices = {row['id']: row['unit_price'] for row in data['skus']}
    assert nodes['sales.production']['baseline']['value'] == sum(row['qty'] * prices[row['sku_id']] for row in data['orders'])
    for sku in prices:
        assert nodes[f'price.{sku}']['baseline']['value'] == prices[sku]
        points = nodes[f'demand.{sku}']['baseline']['points']
        assert [point['value'] for point in points] == [
            row['qty'] for row in data['demand_history'] if row['sku_id'] == sku
        ]


def test_demo_engine_links_point_to_existing_output_fields(demo):
    datasets = {sector: load_demo(sector) for sector in ('production', 'contracting')}
    params = {sector: template.model_defaults()['parameters'] for sector, template in load_templates().items()}
    prod = production.prepare(datasets['production'], params['production'])
    cont = contracting.prepare(datasets['contracting'], params['contracting'])
    outputs = {
        'production_optimizer': production.evaluate(prod, production.baseline(prod)),
        'contracting_optimizer': contracting.evaluate(cont, contracting.baseline(cont)),
        'forecasting': run_forecast('production', datasets['production'], engine='stat'),
    }
    for node in demo['graph']['nodes']:
        link = node['definition']
        if link['kind'] != 'engine':
            continue
        parent, leaf = link['output'].split('.')
        result = outputs[link['engine']][parent]
        key = link['entity_id']
        if parent == 'series':
            assert next(series for series in result if series['key'] == key)[leaf]
        elif link['output'] == 'plan.inventory':
            assert result[leaf][key]
        elif link['output'] in ('plan.schedule', 'plan.activities'):
            selector = 'line' if link['sector'] == 'production' else 'project'
            assert any(row[selector] == key for row in result[leaf])
        else:
            assert isinstance(result[leaf], (float, int))


def test_model_crud_versions_and_audit(client, db_path, demo, model_body):
    assert client.get(MODELS).json() == []
    response = client.post(MODELS + '/demo')
    assert response.status_code == 201
    initial = response.json()
    assert initial['graph'] == demo['graph']
    path = f"{MODELS}/{initial['id']}"
    assert client.get(path).json() == initial
    body = deepcopy(model_body)
    body['name'] = '  شركة روافد · نسخة معدّلة  '
    next(node for node in body['graph']['nodes'] if node['id'] == 'cash')['baseline']['value'] = 75_000
    result = client.put(path, json={**body, 'expected_version': 1})
    assert result.status_code == 200
    assert result.json()['version'] == 2
    assert result.json()['name'] == body['name'].strip()
    assert result.json()['graph'] == body['graph']
    assert client.get(path + '/versions/1').json() == initial
    assert [row['version'] for row in client.get(path + '/versions').json()] == [2, 1]
    assert client.put(path, json={**body, 'expected_version': 1}).status_code == 409
    assert [row['version'] for row in client.get(MODELS).json()] == [2]
    assert client.delete(path).status_code == 204
    assert client.get(MODELS).json() == []
    assert client.get(path).status_code == client.delete(path).status_code == 404
    assert client.put(path, json={**body, 'expected_version': 2}).status_code == 404
    assert client.get(path + '/versions/1').json() == initial
    with get_conn(db_path) as conn:
        assert [row['action'] for row in conn.execute(
            "SELECT action FROM audit_log WHERE entity='enterprise_model' ORDER BY id"
        )] == ['enterprise.create', 'enterprise.update', 'enterprise.archive']


def test_custom_graph_persists_and_v1_survives_schema_upgrade(client, db_path):
    v1 = client.get('/api/companies').json()
    # Simulate a V1 DB with its existing rows and no enterprise tables.
    with get_conn(db_path) as conn:
        conn.executescript('DROP TABLE enterprise_scenarios; DROP TABLE enterprise_model_versions; DROP TABLE enterprise_models;')
    with TestClient(create_app(Settings(db_path=db_path, forecast_engine='stat'))) as upgraded:
        sign_in(upgraded)
        assert upgraded.get('/api/companies').json() == v1
        response = upgraded.post(MODELS, json={
            'name': 'نموذج مستقل',
            'graph': {'nodes': [{
                'id': 'cash', 'type': 'cash', 'name_ar': 'السيولة', 'unit': 'SAR',
                'baseline': {'kind': 'scalar', 'value': 10},
                'definition': {'kind': 'formula', 'expression': 'رصيد أول المدة'},
            }]},
        })
        assert response.status_code == 201
        model = response.json()
    with TestClient(create_app(Settings(db_path=db_path, forecast_engine='stat'))) as restarted:
        sign_in(restarted)
        assert restarted.get(f"{MODELS}/{model['id']}").json() == model
        assert restarted.get('/api/companies').json() == v1


@pytest.mark.parametrize('problem', [
    'duplicate_node', 'duplicate_edge', 'missing_target', 'self_edge', 'invalid_type',
    'invalid_unit', 'invalid_baseline', 'boolean_baseline', 'unordered_series', 'wrong_engine', 'missing_arm',
    'unknown_output', 'missing_entity_selector', 'duplicate_arm', 'missing_definition',
])
def test_invalid_graph_writes_nothing(client, model_body, problem):
    graph = model_body['graph']
    nodes, edges, arms = graph['nodes'], graph['edges'], graph['arms']
    link = next(node['definition'] for node in nodes if node['definition']['kind'] == 'engine')
    match problem:
        case 'duplicate_node': nodes.append(nodes[0])
        case 'duplicate_edge': edges.append(edges[0])
        case 'missing_target': edges[0]['target'] = 'missing'
        case 'self_edge': edges[0]['target'] = edges[0]['source']
        case 'invalid_type': nodes[0]['type'] = 'other'
        case 'invalid_unit': nodes[0]['unit'] = 'USD'
        case 'invalid_baseline': nodes[0]['baseline']['value'] = 'Infinity'
        case 'boolean_baseline': nodes[0]['baseline']['value'] = True
        case 'unordered_series':
            point = next(node['baseline']['points'] for node in nodes if node['baseline']['kind'] == 'series')
            point[1]['date'] = point[0]['date']
        case 'wrong_engine': link.update(engine='contracting_optimizer')
        case 'missing_arm': arms.pop(0)
        case 'unknown_output': link.update(output='series.invalid')
        case 'missing_entity_selector': link.update(entity_id=None)
        case 'duplicate_arm': arms[1] = arms[0]
        case 'missing_definition': nodes[0].pop('definition')
    response = client.post(MODELS, json=model_body)
    assert response.status_code == 422
    assert response.json()['detail']['code'] == 'invalid_request'
    assert client.get(MODELS).json() == []


def test_link_to_unknown_demo_entity_is_rejected(client, model_body):
    link = next(node['definition'] for node in model_body['graph']['nodes'] if node['definition']['kind'] == 'engine')
    link['entity_id'] = 'unused_sku'
    response = client.post(MODELS, json=model_body)
    assert response.status_code == 422
    assert response.json()['detail']['code'] == 'unknown_enterprise_engine_entity'
    assert client.get(MODELS).json() == []


def test_concurrent_updates_commit_only_one_version(client, model_body):
    initial = client.post(MODELS, json=model_body).json()
    path = f"{MODELS}/{initial['id']}"

    def update(name):
        return client.put(path, json={**model_body, 'name': name, 'expected_version': 1}).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(update, ['نسخة أولى', 'نسخة تانية'])) == [200, 409]
    assert [row['version'] for row in client.get(path + '/versions').json()] == [2, 1]


def test_enterprise_page_and_asset_are_served(client):
    page = client.get('/')
    assert 'data-testid="page-enterprise"' in page.text
    assert 'النموذج المؤسسي' in page.text
    assert 'lang="ar" dir="rtl"' in page.text
    assert client.get('/static/js/pages/enterprise.js').status_code == 200
