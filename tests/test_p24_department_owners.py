from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from io import BytesIO

import pytest
from fastapi.testclient import TestClient
from openpyxl import load_workbook

from app.db import get_conn
from app.department_owners import workflow
from app.intelligence import extraction
from app.onboarding import storage
from conftest import TEST_PASSWORD, sign_in

ROOT = '/api/department-owners'
SYNTHETIC_COMPANY = 'شركة الأفق للتوزيع'


def new_company(client, name=SYNTHETIC_COMPANY):
    response = client.post('/api/onboarding/companies', json={'name': name, 'sector': 'trading'})
    assert response.status_code == 201, response.text
    return response.json()['company_id']


def new_user(client, name, company, role='planner'):
    response = client.post('/api/users', json={
        'username': name, 'password': TEST_PASSWORD, 'role': role, 'company_ids': [company],
    })
    assert response.status_code == 201, response.text
    return response.json()['id']


def section(company, key):
    return f'{ROOT}/companies/{company}/sections/{key}'


def assign(client, company, key, user):
    return client.put(section(company, key) + '/assignment', json={'user_id': user})


@contextmanager
def session(client, name):
    # Reuse the running test app while keeping the cookie and CSRF jar independent.
    with TestClient(client.app) as user:
        sign_in(user, name)
        yield user


def manual(client, endpoint, version, *records):
    response = client.post(endpoint + '/manual/preview', json={
        'expected_version': version, 'records': list(records),
    })
    assert response.status_code == 200, response.text
    return response.json()


def submit(client, endpoint, token):
    return client.post(endpoint + '/submit', json={'token': token})


def decide(client, company, sid, decision='approved'):
    return client.post(f'{ROOT}/companies/{company}/submissions/{sid}/decide',
                       json={'decision': decision, 'comment': 'مراجعة القسم'})


def onboarding_section(company, key):
    return f'/api/companies/{company}/onboarding/sections/{key}'


def direct_commit(client, company, section_id, version, **record):
    diff = manual(client, onboarding_section(company, section_id), version, record)
    assert client.post(f'/api/companies/{company}/onboarding/commit',
                       json={'token': diff['token']}).status_code == 200


def test_owner_scope_assignment_csrf_and_revocation(client):
    cid = new_company(client)
    other = new_company(client, 'شركة تانية')
    user_id = new_user(client, 'sales-owner', cid)
    viewer_id = new_user(client, 'local-viewer', cid, 'viewer')
    assert assign(client, cid, 'sales', viewer_id).status_code == 422
    assert assign(client, other, 'sales', user_id).status_code == 422
    assert assign(client, cid, 'unknown', user_id).status_code == 404
    direct_commit(client, cid, 'finance', 1, key='cash.opening', value=654321)

    with session(client, 'sales-owner') as owner:
        assert owner.get(f'/api/companies/{cid}/onboarding').status_code == 200
        assert assign(owner, cid, 'sales', user_id).status_code == 403
        assert assign(client, cid, 'sales', user_id).status_code == 200
        assert owner.get('/', follow_redirects=False).headers['location'] == '/department-owners'
        assert owner.get('/ai', follow_redirects=False).headers['location'] == '/department-owners'
        assert owner.get('/department-owners').status_code == 200
        assert owner.get('/api/auth/me').json()['role'] == 'planner'
        work = owner.get(ROOT + '/workspace').json()
        assert work['companies'] == []
        assert [(row['company_id'], row['section']) for row in work['sections']] == [(cid, 'sales')]
        for path in ('/api/companies', f'/api/companies/{cid}/onboarding', '/api/enterprise/map',
                     f'/api/companies/{cid}/ai/reviews', f'/api/companies/{cid}/audit',
                     '/api/onboarding/templates/trading/workbook'):
            assert owner.get(path).status_code == 403, path
        assert owner.get(f'{ROOT}/companies/{cid}').status_code == 403
        assert owner.get(section(cid, 'finance')).status_code == 404
        assert owner.get(section(other, 'sales')).status_code == 404
        assert '654321' not in owner.get(section(cid, 'sales')).text
        valid = manual(owner, section(cid, 'sales'), 2, {'key': 'demand', 'value': 100})
        assert owner.post(f'/api/companies/{cid}/onboarding/commit',
                          json={'token': valid['token']}).status_code == 403
        headers = owner.headers.pop('X-CSRF-Token')
        assert submit(owner, section(cid, 'sales'), valid['token']).status_code == 403
        owner.headers['X-CSRF-Token'] = headers
        assert assign(client, cid, 'sales', None).status_code == 200
        assert owner.get(ROOT + '/workspace').json()['sections'] == []
        assert owner.get('/api/companies').status_code == 403
        assert assign(client, cid, 'sales', user_id).status_code == 200
        assert submit(owner, section(cid, 'sales'), valid['token']).status_code == 404
        assert owner.post('/api/auth/logout').status_code == 204


def test_manual_submit_merge_versioning_and_authenticated_audit(client):
    cid = new_company(client)
    owner_id = new_user(client, 'scoped-planner', cid)
    reviewer_id = client.get('/api/auth/me').json()['id']
    for name in ('sales', 'finance'):
        assert assign(client, cid, name, owner_id).status_code == 200

    with session(client, 'scoped-planner') as owner:
        sales, finance = section(cid, 'sales'), section(cid, 'finance')
        invalid = manual(owner, sales, 1, {'key': 'credit', 'value': 100})
        assert invalid['token'] is None and invalid['validation']['errors'][0]['column'] == 'key'
        sales_diff = manual(owner, sales, 1, {'key': 'demand', 'value': 120, 'date': '2026-11-16'})
        assert sales_diff['diff'][0]['before'] is None
        assert submit(owner, finance, sales_diff['token']).status_code == 404
        sid = submit(owner, sales, sales_diff['token']).json()['id']
        assert submit(owner, sales, sales_diff['token']).status_code == 404
        duplicate = manual(owner, sales, 1, {'key': 'demand', 'value': 130})
        assert submit(owner, sales, duplicate['token']).status_code == 409
        finance_diff = manual(owner, finance, 1, {'key': 'cash.opening', 'value': 1000})
        fid = submit(owner, finance, finance_diff['token']).json()['id']
        assert decide(owner, cid, sid).status_code == 403
        assert owner.get(sales).json()['records'] == []
        assert owner.get(sales).json()['submissions'][0]['status'] == 'pending'
        assert client.get(f'/api/companies/{cid}/onboarding').json()['version'] == 1

        assert decide(client, cid, sid).json()['approved_version'] == 2
        assert decide(client, cid, fid).json()['approved_version'] == 3
        assert decide(client, cid, sid).status_code == 409
        current = owner.get(sales).json()
        assert current['records'] == [{'key': 'demand', 'value': 120.0, 'date': '2026-11-16', 'source': 'actual'}]
        assert current['readiness']['score'] == 100 and current['readiness']['reminder'] is None
        assert current['submissions'][0]['reviewer_id'] == reviewer_id
        assert current['submissions'][0]['comment'] == 'مراجعة القسم'
        unchanged = manual(owner, sales, 3, {'key': 'demand', 'value': 120, 'date': '2026-11-16'})
        assert unchanged['token'] is None and unchanged['diff'] == []
        newer = manual(owner, sales, 3, {'key': 'demand', 'value': 135, 'date': '2026-11-16'})
        assert newer['diff'][0]['before']['value'] == 120
        assert {row['key'] for row in client.get(f'/api/companies/{cid}/onboarding').json()['records']} == {
            'demand', 'cash.opening',
        }

    audit = client.get(f'/api/companies/{cid}/audit').json()
    department_events = [event for event in audit if event['action'].startswith('department.')]
    assert [(event['action'], event['user_id']) for event in reversed(department_events)] == [
        ('department.assign', reviewer_id), ('department.assign', reviewer_id),
        ('department.submit', owner_id), ('department.submit', owner_id),
        ('department.approved', reviewer_id), ('department.approved', reviewer_id),
    ]


def test_legacy_update_conflicts_rejection_and_reassignment(client):
    cid = new_company(client)
    owner_id = new_user(client, 'first-planner', cid)
    second = new_user(client, 'second-planner', cid)
    assert assign(client, cid, 'sales', owner_id).is_success
    sales = section(cid, 'sales')
    with session(client, 'first-planner') as owner, session(client, 'second-planner') as other:
        preview = manual(owner, sales, 1, {'key': 'demand', 'value': 90})
        assert submit(other, sales, preview['token']).status_code == 404
        direct_commit(client, cid, 'sales', 1, key='demand', value=95)
        assert submit(owner, sales, preview['token']).status_code == 409
        preview = manual(owner, sales, 2, {'key': 'demand', 'value': 100})
        sid = submit(owner, sales, preview['token']).json()['id']
        direct_commit(client, cid, 'sales', 2, key='demand', value=105)
        assert decide(client, cid, sid).status_code == 409
        assert decide(client, cid, sid, 'rejected').json()['approved_version'] is None
        assert owner.get(sales).json()['records'][0]['value'] == 105

        preview = manual(owner, sales, 3, {'key': 'demand', 'value': 112})
        sid = submit(owner, sales, preview['token']).json()['id']
        assert assign(client, cid, 'sales', second).is_success
        assert owner.get(sales).status_code == 404
        assert other.get(sales).json()['submissions'] == []
        assert decide(client, cid, sid).status_code == 409
        admin = client.get(f'{ROOT}/companies/{cid}').json()
        assert [item['status'] for item in admin['submissions']] == ['superseded', 'rejected']
        assert admin['version'] == 3


def test_readiness_and_reminders_use_section_update_from_existing_history(client, db_path):
    cid = new_company(client)
    planner = new_user(client, 'warehouse-planner', cid)
    assert assign(client, cid, 'inventory', planner).is_success
    direct_commit(client, cid, 'inventory', 1, key='inventory', value=6, date='2026-09-01')
    direct_commit(client, cid, 'sales', 2, key='demand', value=10)
    direct_commit(client, cid, 'finance', 3, key='credit', value=30)
    with get_conn(db_path) as connection:
        model = storage.snapshot(connection, cid)
        for version, date in enumerate(('2026-09-01', '2026-09-03', '2026-10-19', '2026-10-20'), 1):
            connection.execute('UPDATE enterprise_model_versions SET created_at=? WHERE model_id=? AND version=?',
                               (date + 'T00:00:00+00:00', model['id'], version))
        model = storage.snapshot(connection, cid)
        status = workflow.section_status(connection, model, now=datetime(2026, 11, 18, tzinfo=UTC))
    rows = {row['id']: row for row in status}
    assert rows['inventory']['last_updated'] == '2026-09-03T00:00:00+00:00'
    assert rows['inventory']['score'] == 100 and rows['inventory']['reminder'] == 'stale'
    assert rows['sales']['reminder'] == 'stale'
    assert rows['finance']['reminder'] is None
    assert rows['operations']['reminder'] == 'never_updated'
    assert workflow.email_reminder_hook(cid, 'inventory', planner, 'stale') == {
        'company_id': cid, 'section': 'inventory', 'user_id': planner, 'reason': 'stale',
    }
    with session(client, 'warehouse-planner') as owner:
        state = owner.get(section(cid, 'inventory')).json()['readiness']
        assert state['reminder'] == 'stale'
        assert state['last_updated'] == rows['inventory']['last_updated']


def test_template_upload_and_scoped_ai_extraction(client):
    cid = new_company(client)
    planner = new_user(client, 'docs-owner', cid)
    for key in ('sales', 'structure'):
        assert assign(client, cid, key, planner).is_success
    with session(client, 'docs-owner') as owner:
        sales = section(cid, 'sales')
        workbook = owner.get(sales + '/workbook')
        assert workbook.status_code == 200
        book = load_workbook(BytesIO(workbook.content))
        assert book.sheetnames == ['sales'] and book['sales'].sheet_view.rightToLeft
        book['sales']['B2'] = 108
        book.create_sheet('finance').append(['cash.opening', 2000])
        output = BytesIO()
        book.save(output)
        book.close()
        result = owner.post(sales + '/excel/preview', data={'expected_version': 1},
                            files={'file': ('sales.xlsx', output.getvalue())}).json()
        assert [(row['after']['key'], row['after']['value']) for row in result['diff']] == [('demand', 108)]
        assert result['token']

        result = owner.post(sales + '/extract/preview', data={'expected_version': 1}, files={
            'file': ('sales.txt', 'الطلب الأسبوعي = 120\nحد الائتمان = 9000'.encode()),
        }).json()
        assert [(row['after']['key'], row['after']['value']) for row in result['diff']] == [('demand', 120)]
        assert result['diff'][0]['evidence']['provider'] == 'rules'
        assert '9000' not in str(result)
        sid = submit(owner, sales, result['token']).json()['id']
        assert decide(client, cid, sid).json()['approved_version'] == 2
        structural = owner.post(section(cid, 'structure') + '/extract/preview', data={'expected_version': 2},
                                files={'file': ('structure.txt', 'department.sales = المبيعات'.encode())}).json()
        assert structural['diff'][0]['after']['value'] == 'المبيعات'
        assert structural['token']


def test_codex_extract_is_opt_in_and_sources_numbers(client, monkeypatch):
    cid = new_company(client)
    planner = new_user(client, 'supplier-owner', cid)
    assert assign(client, cid, 'suppliers', planner).is_success
    monkeypatch.setattr(client.app.state, 'settings', replace(client.app.state.settings, llm_provider='codex'))

    calls = []

    def mock_provider(settings, schema, prompt, payload):
        calls.append(payload)
        assert [node['id'] for node in payload['nodes']] == ['supplier.delay']
        index = payload['rows'][0]['row']
        return extraction.MappingBatch(mappings=[
            extraction.Mapping(row=index, node_id='supplier.delay', literal='4', confidence=.8),
        ])

    monkeypatch.setattr(extraction, 'select', mock_provider)
    with session(client, 'supplier-owner') as owner:
        path = section(cid, 'suppliers') + '/extract/preview'
        files = {'file': ('suppliers.txt', b'waiting = 4 days')}
        assert owner.post(path, files=files, data={'expected_version': 1}).json()['token'] is None
        assert not calls
        response = owner.post(path, files=files, data={'expected_version': 1, 'remote': 'true'})
        assert response.is_success, response.text
        diff = response.json()['diff'][0]
        assert diff['after']['value'] == 4
        assert diff['evidence']['provider'] == 'codex'
        assert len(calls) == 1


def test_revoked_company_grant_blocks_submissions_and_approval(client):
    cid = new_company(client)
    planner = new_user(client, 'risk-owner', cid)
    assert assign(client, cid, 'risk', planner).is_success
    with session(client, 'risk-owner') as owner:
        risk = section(cid, 'risk')
        diff = manual(owner, risk, 1, {'key': 'risk.exposure', 'value': 15})
        sid = submit(owner, risk, diff['token']).json()['id']
        assert client.put(f'/api/users/{planner}', json={'role': 'planner', 'company_ids': []}).is_success
        assert owner.get(ROOT + '/workspace').json()['sections'] == []
        assert owner.get(risk).status_code == 404
        assert decide(client, cid, sid).status_code == 404
        assert decide(client, cid, sid, 'rejected').is_success
