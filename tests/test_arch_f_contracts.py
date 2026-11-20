"""Architecture F contracts exercised through versioned API entry points."""
from fastapi.testclient import TestClient
import pytest

from app.db import encode, get_conn, utc_now
from conftest import sign_in
from test_p11_enterprise_impact import assume, edge, run, scalar
from test_p18_prescriptive import PASSWORD, endpoint, save_model, submit_draft

MODELS = '/api/enterprise/models'
AI = '/api/companies/1/ai'


def graph_body(source='SAR', target='SAR', **contract):
    return {'name': 'عقد العلاقة', 'graph': {
        'nodes': [scalar('driver', 14, 'demand') | {'unit': source},
                  scalar('pnl.result', 10) | {'unit': target}],
        'edges': [edge('driver', 'pnl.result') | contract],
    }}


def create(client, body):
    response = client.post(MODELS + '?company_id=1', json=body)
    assert response.status_code == 201, response.text
    return response.json()


@pytest.mark.parametrize(('source', 'target', 'shock', 'change'), [
    ('days', 'weeks', 21, 1), ('weeks', 'days', 16, 14),
])
def test_edge_rejects_wrong_unit_and_converts_time(client, source, target, shock, change):
    invalid = graph_body(source, target, unit='SAR', status='approved')
    response = client.post(MODELS, json=invalid)
    assert response.status_code == 422 and response.json()['detail']['code'] == 'invalid_edge_contract'
    snapshot = create(client, graph_body(source, target, unit='1', status='approved'))
    link, = snapshot['graph']['edges']
    assert link['unit'] == '1' and link['status'] == 'approved'
    result, values = run(client, assume('driver', shock), model_id=snapshot['id'], weeks=3)
    assert values['pnl.result']['delta'] == pytest.approx(change)
    assert result['paths'][0]['provenance'] == link['provenance']


def test_dimension_changing_coefficient_and_legacy_incompatible_edge(client):
    invalid = client.post(MODELS, json=graph_body('units', 'SAR', unit='1', rule={'multiplier': 4}))
    assert invalid.status_code == 422
    snapshot = create(client, graph_body('units', 'SAR', unit='SAR/units', status='approved',
                                         rule={'multiplier': 4}))
    _, values = run(client, assume('driver', 16), model_id=snapshot['id'])
    assert values['pnl.result']['delta'] == 8
    legacy = create(client, graph_body('units', 'SAR'))
    assert legacy['graph']['edges'][0]['status'] == 'draft'
    _, values = run(client, assume('driver', 16), model_id=legacy['id'])
    assert values['pnl.result']['delta'] == 0


def test_cycle_rejected_at_create_and_revision_write(client):
    body = graph_body(unit='1', status='draft')
    existing = create(client, body)
    body['graph']['edges'].append(edge('pnl.result', 'driver', lag_weeks=2) | {'unit': '1', 'status': 'draft'})
    for response in (client.post(MODELS, json=body),
                     client.put(f'{MODELS}/{existing["id"]}', json=body | {'expected_version': 1})):
        assert response.status_code == 422 and 'دورة' in response.json()['detail']['msg_ar']
    assert len(client.get(f'{MODELS}/{existing["id"]}/versions').json()) == 1


def test_draft_is_inert_until_scoped_approval_and_stamps_are_server_owned(client):
    for user, role, companies in [('editor', 'planner', [1]), ('reviewer', 'approver', [1]),
                                   ('outsider-f', 'approver', [2])]:
        response = client.post('/api/users', json={'username': user, 'role': role,
                                                  'password': PASSWORD, 'company_ids': companies})
        assert response.status_code == 201, response.text
    with TestClient(client.app) as other:
        sign_in(other, 'editor', PASSWORD)
        body = graph_body(unit='1', evidence='كشف المصدر', provenance={
            'actor': 'spoofed', 'source': 'spoofed', 'recorded_at': '2000-01-01T00:00:00Z',
        })
        assert other.post(MODELS + '?company_id=1', json=graph_body(unit='1', status='approved')).status_code == 403
        snapshot = create(other, body)
        model_id = snapshot['id']
        path = f'{MODELS}/{model_id}/edges/driver/pnl.result/approve'
        assert other.post(path, json={'expected_version': 1}).status_code == 403
        result, values = run(other, assume('driver', 16), model_id=model_id)
        assert values['pnl.result']['delta'] == 0 and not result['paths']
        sign_in(other, 'outsider-f', PASSWORD)
        assert other.post(path, json={'expected_version': 1}).status_code == 404
        sign_in(other, 'reviewer', PASSWORD)
        response = other.post(path, json={'expected_version': 1})
        assert response.status_code == 200, response.text
        approved, = response.json()['graph']['edges']
        assert approved['provenance']['actor'] == 'editor'
        assert approved['provenance']['approved_by'] == 'reviewer'
        assert approved['provenance']['source'] == 'builder'
        assert approved['provenance']['evidence'] == 'كشف المصدر'
        assert approved['provenance']['approved_at'] and approved['provenance']['recorded_at']
        assert other.post(path, json={'expected_version': 1}).status_code == 409
    result, values = run(client, assume('driver', 16), model_id=model_id)
    assert values['pnl.result']['delta'] == 2 and result['version'] == 2
    _, values = run(client, assume('driver', 16), model_id=model_id, version=1)
    assert values['pnl.result']['delta'] == 0


def test_legacy_edges_have_stable_provenance_and_demo_handles_financing(client, db_path):
    body = graph_body()
    snapshot = create(client, body)
    with get_conn(db_path) as connection:
        connection.execute('UPDATE enterprise_model_versions SET graph_json = ? WHERE model_id = ?',
                           (encode(body['graph']), snapshot['id']))
    restored = client.get(f'{MODELS}/{snapshot["id"]}').json()
    assert restored['graph']['edges'][0]['provenance']['source'] == 'legacy'
    assert restored == client.get(f'{MODELS}/{snapshot["id"]}/versions/1').json()
    response = client.post(MODELS + '/demo')
    assert response.status_code == 201, response.text
    demo = response.json()
    assert all(edge['status'] == 'approved' and edge['unit'] and edge['provenance'] for edge in demo['graph']['edges'])
    assert client.get('/api/enterprise/whatif/drivers', params={'model_id': demo['id']}).status_code == 200
    template = client.get('/api/onboarding/templates/retail').json()['graph']
    assert all(edge['status'] == 'approved' and edge['unit'] and edge['provenance'] for edge in template['edges'])


def test_pinned_outcome_closes_with_measurements_and_map_compares(client):
    snapshot = save_model(client)
    base = endpoint(snapshot)
    decision_id = submit_draft(client, base)
    decision = f'{base}/decisions/{decision_id}'
    assert client.post(decision + '/decide', json={'decision': 'approved'}).status_code == 200
    actual = {'observed_on': utc_now()[:10], 'metrics': {'profit': 100}}
    closing = actual | {'close': True, 'evidence': 'دفتر الحسابات'}
    assert client.post(decision + '/actuals', json=closing).status_code == 422
    assert client.post(decision + '/actuals', json=actual).status_code == 201
    assert client.get(base + '/decisions').json()['decisions'][0]['outcome_status'] == 'tracking'
    assert client.post(decision + '/actuals', json=closing | {'metrics': {'cash_low': 50}}).status_code == 201
    row, = client.get(base + '/decisions').json()['decisions']
    assert row['status'] == 'approved' and row['outcome_status'] == 'closed'
    spec = row['outcome_contract']['indicators']['profit']
    assert (spec['expected'], spec['unit'], spec['weeks'], spec['required']) == (108, 'SAR', 52, True)
    assert row['actuals']['recorded_by'] == 'qa-admin' and row['actuals']['evidence'] == 'دفتر الحسابات'
    assert next(item for item in row['accuracy'] if item['kpi'] == 'profit')['error'] == -8
    assert client.post(decision + '/actuals', json=actual).status_code == 409
    assert client.put(f'{MODELS}/{snapshot["id"]}', json={
        'name': snapshot['name'], 'graph': snapshot['graph'], 'expected_version': 1,
    }).status_code == 200
    path = '/api/enterprise/map'
    assert client.get(path, params={'model_id': snapshot['id']}).json()['decision_outcomes'] == []
    outcome, = client.get(path, params={'model_id': snapshot['id'], 'version': 1}).json()['decision_outcomes']
    assert outcome['id'] == decision_id and outcome['version'] == 1 and outcome['accuracy'] == row['accuracy']


@pytest.mark.parametrize(('target', 'disposition'), [(-4, 'eligible_decision'), (999, 'scenario_only')])
def test_lab_recommendation_policy_and_outcome_context(client, target, disposition):
    body = graph_body(unit='1', status='approved')
    body['graph']['nodes'].append(scalar('custom.margin', -10) | {'unit': '%'})
    body['graph']['edges'].append(edge('driver', 'custom.margin', multiplier=1) | {'unit': '%/SAR', 'status': 'approved'})
    snapshot = create(client, body)
    model_id = snapshot['id']
    response = client.post(AI + '/recommend', json={'model_id': model_id, 'goal': {
        'objective': 'custom.margin', 'target': target, 'weeks': 4, 'trials': 4,
        'controls': [{'node_id': 'driver', 'low': 14, 'high': 21}],
    }})
    assert response.status_code == 201, response.text
    review = response.json()
    best = review['data']['recommendations'][0]
    assert best['disposition'] == disposition
    assert best['outcome_context']['indicators']['custom.margin'] == {'unit': '%', 'weeks': 4, 'basis': 'final_value'}
    assert best['outcome_context']['assumptions'][0]['source'] == 'goal_search_controls'
    assert best['outcome_context']['risk'] is None
    accepted = client.post(f'{AI}/reviews/{review["id"]}/decide', json={'status': 'accepted'})
    assert accepted.status_code == 200, accepted.text
    outcome = accepted.json()['outcome']
    assert outcome['scenario_id']
    if disposition == 'scenario_only':
        assert 'decision_id' not in outcome
    else:
        decision = f'{endpoint(snapshot)}/decisions/{outcome["decision_id"]}'
        assert client.post(decision + '/decide', json={'decision': 'approved'}).status_code == 200
        actual = {'observed_on': utc_now()[:10], 'metrics': {'custom.margin': -4}, 'close': True}
        assert client.post(decision + '/actuals', json=actual).status_code == 201
        item, = client.get(endpoint(snapshot) + '/decisions').json()['decisions']
        assert item['outcome_status'] == 'closed' and item['accuracy'][0]['error'] == -1


def test_lab_edge_submission_needs_reviewer_and_binds_evidence(client):
    for name, role in [('lab-editor', 'planner'), ('lab-approver', 'approver')]:
        response = client.post('/api/users', json={'username': name, 'role': role,
                                                  'password': PASSWORD, 'company_ids': [1]})
        assert response.status_code == 201, response.text
    with TestClient(client.app) as other:
        sign_in(other, 'lab-editor', PASSWORD)
        response = other.post(AI + '/entry', data={
            'description': 'الطلب 100 وحدة\nالإيرادات 2500 ريال\nالطلب -> الإيرادات = 25', 'remote': 'false',
        })
        assert response.status_code == 201, response.text
        review = response.json()
        proposed = review['data']['relations'][0]['edge']
        assert proposed['status'] == 'draft' and proposed['unit'] == 'SAR/units'
        assert proposed['provenance']['actor'] == 'lab-editor'
        path = f'{AI}/reviews/{review["id"]}/decide'
        assert other.post(path, json={'status': 'accepted'}).status_code == 403
        sign_in(other, 'lab-approver', PASSWORD)
        accepted = other.post(path, json={'status': 'accepted'})
        assert accepted.status_code == 200, accepted.text
        snapshot = other.get(f'{MODELS}/{accepted.json()["outcome"]["model_id"]}').json()
        saved, = snapshot['graph']['edges']
        assert saved['status'] == 'approved'
        assert saved['provenance']['approved_by'] == 'lab-approver'
        assert saved['provenance']['review_id'] == review['id']
        _, values = run(client, assume('demand', 110), model_id=snapshot['id'])
        assert values['pnl.revenue']['delta'] == 250
