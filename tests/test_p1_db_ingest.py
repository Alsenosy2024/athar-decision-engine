import csv
import io
import json
import re

import pytest
from fastapi.testclient import TestClient

from conftest import sign_in
from openpyxl import load_workbook

from app.config import Settings
from app.db import get_conn
from app.ingestion.sample_templates import write_sample_templates
from app.main import create_app
from app.templates_registry import load_templates


@pytest.fixture
def samples(tmp_path):
    write_sample_templates(tmp_path)
    return tmp_path


def upload(client, cid, filename, data):
    return client.post(f'/api/companies/{cid}/datasets/upload', files={'file': (filename, data)})


def change_cell(path, sheet, cell, value):
    workbook = load_workbook(path)
    workbook[sheet][cell] = value
    stream = io.BytesIO()
    workbook.save(stream)
    workbook.close()
    return stream.getvalue()


def assert_arabic(text):
    assert re.search(r'[\u0600-\u06ff]', text)


def test_ddl(client, db_path):
    with get_conn(db_path) as connection:
        tables = {row['name'] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT GLOB 'sqlite_*'"
        )}
        assert tables == {
            'integration_api_keys', 'integration_webhook_routes', 'integration_webhook_outbox', 'integration_alert_state',
            'companies', 'datasets', 'company_models', 'forecasts', 'runs', 'plans', 'audit_log',
            'actuals', 'calibrations', 'chat_messages', 'enterprise_models', 'enterprise_model_versions',
            'enterprise_scenarios', 'erp_connectors', 'p6_imports', 'users', 'user_companies', 'auth_sessions',
            'onboarding_companies', 'onboarding_previews',
            'enterprise_edge_calibrations', 'enterprise_kpi_thresholds', 'enterprise_decisions',
            'enterprise_decision_actuals',
            'ai_reviews', 'ai_review_p6', 'ai_estimated_points',
            'company_process_mining',
            'department_assignments', 'department_previews', 'department_submissions',
        }
        assert connection.execute('PRAGMA foreign_keys').fetchone()[0] == 1
        assert connection.execute('PRAGMA journal_mode').fetchone()[0] == 'wal'


def test_seeds_once_on_restart(client, db_path):
    expected = [(1, 'contracting', 1, 1), (2, 'production', 2, 1)]
    with TestClient(create_app(Settings(db_path=db_path))) as second:
        sign_in(second)
        companies = second.get('/api/companies').json()
        assert [(c['id'], c['template'], c['active_dataset_id'], c['model_version']) for c in companies] == expected


def test_health(client):
    response = client.get('/api/health')
    assert response.status_code == 200
    assert response.json() == {'status': 'ok', 'version': '0.1.0', 'solver': 'ortools 9.15.6755', 'provider': 'rules'}


def test_templates(client):
    items = client.get('/api/templates')
    assert items.status_code == 200
    assert len(items.json()) == 2
    for template_id, template in load_templates().items():
        response = client.get(f'/api/templates/{template_id}')
        assert response.json() == template.model_dump(by_alias=True)
        assert template.entities
        assert template.model_defaults()['parameters'][template.horizon_key] in (44, 14)


def test_demo_load_and_activate(client, demo_dataset):
    template_id, payload = demo_dataset
    cid = 1 if template_id == 'contracting' else 2
    result = client.post(f'/api/companies/{cid}/datasets/demo')
    assert result.status_code == 200
    result = result.json()
    assert result['validation'] == {'ok': True, 'errors': [], 'summary': {key: len(rows) for key, rows in payload.items()}}
    datasets = client.get(f'/api/companies/{cid}/datasets').json()
    assert sum(row['is_active'] for row in datasets) == 1
    assert datasets[0]['id'] == result['dataset_id']
    assert client.get(f"/api/datasets/{result['dataset_id']}").json()['payload'] == payload


@pytest.mark.parametrize(('cid', 'template_id'), [(1, 'contracting'), (2, 'production')])
def test_upload_sample_excel(client, samples, cid, template_id):
    path = samples / f'{template_id}_template.xlsx'
    response = upload(client, cid, path.name, path.read_bytes())
    assert response.status_code == 200, response.text
    result = response.json()
    assert result['validation']['ok']
    assert set(result['validation']['summary'].values()) == {3}
    assert client.get(f'/api/companies/{cid}/datasets').json()[0]['source'] == 'upload'


@pytest.mark.parametrize(('sheet', 'cell', 'value', 'column', 'row'), [
    ('projects', 'D1', 'unknown', 'deadline_week', 1),
    ('activities', 'B2', 'missing', 'project_id', 2),
    ('activities', 'F2', 'missing', 'resource', 2),
    ('activities', 'E2', 'abc', 'duration_weeks', 2),
    ('activities', 'E2', 0, 'duration_weeks', 2),
    ('projects', 'A3', 'A', 'id', 3),
    ('projects', 'D3', 1, 'deadline_week', 3),
])
def test_contracting_validation(client, samples, sheet, cell, value, column, row):
    content = change_cell(samples / 'contracting_template.xlsx', sheet, cell, value)
    response = upload(client, 1, 'bad.xlsx', content)
    assert response.status_code == 422
    data = response.json()
    assert data['dataset_id'] is None
    assert not data['validation']['ok']
    issue = next(issue for issue in data['validation']['errors'] if (
        issue['sheet'], issue['row'], issue['column']
    ) == (sheet, row, column))
    assert_arabic(issue['msg_ar'])
    assert len(client.get('/api/companies/1/datasets').json()) == 1


@pytest.mark.parametrize(('sheet', 'cell', 'value', 'column'), [
    ('rates', 'A2', 'L99', 'line_id'),
    ('rates', 'B2', 'S99', 'sku_id'),
    ('rates', 'C2', -1, 'units_per_day'),
    ('orders', 'C2', 'S99', 'sku_id'),
    ('orders', 'D2', 0, 'qty'),
    ('orders', 'E2', 14, 'due_day'),
])
def test_production_validation(client, samples, sheet, cell, value, column):
    content = change_cell(samples / 'production_template.xlsx', sheet, cell, value)
    response = upload(client, 2, 'bad.xlsx', content)
    assert response.status_code == 422
    assert any((e['sheet'], e['row'], e['column']) == (sheet, 2, column)
               for e in response.json()['validation']['errors'])


def test_upload_missing_sheet(client, samples):
    book = load_workbook(samples / 'contracting_template.xlsx')
    book.remove(book['history'])
    stream = io.BytesIO()
    book.save(stream)
    book.close()
    response = upload(client, 1, 'missing.xlsx', stream.getvalue())
    assert response.status_code == 422
    assert response.json()['validation']['errors'][0]['sheet'] == 'history'


def test_csv_upload(client, demo_dataset):
    template_id, payload = demo_dataset
    columns = list(dict.fromkeys(column for rows in payload.values() for column in rows[0]))
    text = io.StringIO()
    writer = csv.DictWriter(text, fieldnames=['sheet', *columns])
    writer.writeheader()
    for sheet, rows in payload.items():
        writer.writerows({'sheet': sheet, **row} for row in rows)
    cid = 1 if template_id == 'contracting' else 2
    result = upload(client, cid, 'بيانات.csv', text.getvalue().encode('utf-8-sig'))
    assert result.status_code == 200, result.text
    assert client.get(f"/api/datasets/{result.json()['dataset_id']}").json()['payload'] == payload


def test_bad_file(client):
    result = upload(client, 1, 'bad.xlsx', b'garbage')
    assert result.status_code == 422
    assert_arabic(result.json()['validation']['errors'][0]['msg_ar'])


def test_create_company(client):
    result = client.post('/api/companies', json={'name': '  شركة جديدة  ', 'template': 'production'})
    assert result.status_code == 201
    assert result.json() == dict(id=3, name='شركة جديدة', template='production', active_dataset_id=None, model_version=1)
    assert client.get('/api/companies/3/model').json()['version'] == 1


def test_model_update_increments_version_and_audits(client, db_path):
    old = client.get('/api/companies/1/model').json()
    result = client.put('/api/companies/1/model', json={
        'parameters': {'credit_limit': 3500}, 'constraints': {'credit_limit': 'soft'}, 'weights': {'ld': 2},
    })
    assert result.status_code == 200
    model = result.json()
    assert model['version'] == 2
    assert model['parameters']['horizon_weeks'] == old['parameters']['horizon_weeks']
    assert model['parameters']['credit_limit'] == 3500
    assert model['constraints']['credit_limit'] == 'soft'
    assert model['weights']['ld'] == 2
    assert client.get('/api/companies/1/model').json() == model
    assert client.get('/api/companies').json()[0]['model_version'] == 2
    with get_conn(db_path) as connection:
        entry = connection.execute("SELECT * FROM audit_log WHERE action='model.update'").fetchone()
        assert entry['entity_id'] == model['id']
        assert json.loads(entry['details_json']) == {'version': 2}


@pytest.mark.parametrize('patch', [
    {'parameters': {'credit_limit': -1}}, {'weights': {'ld': 5.1}}, {'constraints': {'precedence': 'soft'}},
])
def test_invalid_model_does_not_increment(client, patch):
    response = client.put('/api/companies/1/model', json=patch)
    assert response.status_code == 422
    assert_arabic(response.json()['detail']['msg_ar'])
    assert client.get('/api/companies/1/model').json()['version'] == 1


def test_upload_uses_company_horizon(client, samples):
    assert client.put('/api/companies/2/model', json={'parameters': {'horizon_days': 15}}).status_code == 200
    content = change_cell(samples / 'production_template.xlsx', 'orders', 'E2', 14)
    assert upload(client, 2, 'new.xlsx', content).status_code == 200
    assert client.put('/api/companies/2/model', json={'parameters': {'horizon_days': 14}}).status_code == 422


def test_missing_company_uses_arabic_error_contract(client):
    response = client.get('/api/companies/99/datasets')
    assert response.status_code == 404
    assert response.json()['detail']['code'] == 'company_not_found'
    assert_arabic(response.json()['detail']['msg_ar'])


def test_static_shell(client):
    index = client.get('/')
    assert index.status_code == 200
    assert 'lang="ar" dir="rtl"' in index.text
    assert len(set(re.findall(r'data-testid="page-[a-z]+"', index.text))) == 16
    assert client.get('/static/vendor/alpine.min.js').status_code == 200
    assert client.get('/static/fonts/ThmanyahSerifText-Bold.otf').status_code == 200
    assert client.get('/static/templates/contracting_template.xlsx').status_code == 200
