"""Exercise the HTTP contracts the nine Phase 7 pages consume."""
import re
from io import BytesIO

import pytest
from openpyxl import load_workbook

from app.config import STATIC_DIR

PAGES = {'dashboard', 'data', 'model', 'forecast', 'optimize', 'whatif', 'chat', 'approvals', 'actuals'}
SECTORS = [(1, 'contracting'), (2, 'production')]


@pytest.fixture(autouse=True)
def rules_provider(monkeypatch):
    monkeypatch.setenv('LLM_PROVIDER', 'rules')


@pytest.mark.parametrize('page', sorted(PAGES))
def test_page_is_mounted(client, page):
    response = client.get('/')
    assert response.status_code == 200
    mounted = set(re.findall(r'data-testid="page-([a-z]+)"', response.text))
    assert mounted == PAGES | {
        'enterprise', 'impact', 'scenarios', 'connectors', 'onboarding', 'prescriptive', 'map',
    }
    assert 'lang="ar" dir="rtl"' in response.text
    assert f'/static/js/pages/{page}.js' in response.text
    assert client.get(f'/static/js/pages/{page}.js').status_code == 200


@pytest.mark.parametrize('path', [
    'vendor/alpine.min.js', 'vendor/chart.umd.js', 'css/app.css',
    'fonts/ThmanyahSerifText-Bold.otf', 'fonts/ThmanyahSerifText-Black.otf',
    'fonts/ThmanyahSerifDisplay-Bold.otf', 'fonts/ThmanyahSerifDisplay-Black.otf',
    'templates/contracting_template.xlsx', 'templates/production_template.xlsx',
    'templates/actuals_contracting_template.csv', 'templates/actuals_production_template.csv',
])
def test_local_assets(client, path):
    response = client.get('/static/' + path)
    assert response.status_code == 200 and response.content


def json_response(response, status=200):
    assert response.status_code == status, response.text
    return response.json()


@pytest.mark.parametrize(('path', 'error'), [
    ('/runs/latest', 'run_not_found'),
    ('/forecast/latest', 'forecast_not_found'),
    ('/actuals/latest', 'actuals_not_found'),
])
def test_latest_page_requests_distinguish_empty_state_from_unknown_company(client, path, error):
    root = '/api/companies/1' + path
    assert json_response(client.get(root), 404)['detail']['code'] == error
    assert json_response(client.get(root, params={'allow_missing': True})) is None
    unknown = client.get('/api/companies/999' + path, params={'allow_missing': True})
    assert json_response(unknown, 404)['detail']['code'] == 'company_not_found'


@pytest.mark.parametrize(('cid', 'sector'), SECTORS)
def test_sector_http_workflow(client, cid, sector):
    root = f'/api/companies/{cid}'
    assert json_response(client.get('/api/health'))['provider'] == 'rules'
    assert {row['template_id'] for row in json_response(client.get('/api/templates'))} == {'contracting', 'production'}
    definition = json_response(client.get('/api/templates/' + sector))
    assert definition['whatif_changes']
    companies = json_response(client.get('/api/companies'))
    assert next(row for row in companies if row['id'] == cid)['template'] == sector

    created = json_response(client.post('/api/companies', json={'name': 'شركة اختبار', 'template': sector}), 201)
    assert created['template'] == sector
    sample = STATIC_DIR / 'templates' / f'{sector}_template.xlsx'
    upload = json_response(client.post(root + '/datasets/upload', files={'file': (sample.name, sample.read_bytes())}))
    assert upload['validation']['ok']

    demo = json_response(client.post(root + '/datasets/demo'))
    dataset_id = demo['dataset_id']
    assert json_response(client.get(root + '/datasets'))[0]['id'] == dataset_id
    assert json_response(client.get(f'/api/datasets/{dataset_id}'))['validation']['ok']

    model = json_response(client.get(root + '/model'))
    updated = json_response(client.put(root + '/model', json={'weights': model['weights']}))
    assert updated['version'] == model['version'] + 1

    forecast = json_response(client.post(root + '/forecast', json={}))
    assert forecast['series'] and forecast == json_response(client.get(root + '/forecast/latest'))
    result = json_response(client.post(root + '/optimize', json={'time_limit': 2}))
    run_id = result['run_id']
    run = json_response(client.get(f'/api/runs/{run_id}'))
    assert json_response(client.get(root + '/runs/latest?kind=optimize'))['id'] == run['id']
    assert run['dataset_id'] == dataset_id and result['optimized']['plan']

    scenario = json_response(client.post(root + '/whatif', json={
        'base_run_id': run_id, 'changes': [{'type': 'credit_limit_pct', 'value': -10}],
    }))
    assert json_response(client.get(root + '/runs/latest?kind=whatif'))['id'] == scenario['run_id']
    risk = json_response(client.post(root + '/montecarlo', json={'run_id': run_id, 'n': 20, 'seed': 42}))
    assert risk['n'] == 20
    assert json_response(client.get(root + '/runs/latest?kind=montecarlo'))['base_run_id'] == run_id

    reply = json_response(client.post(root + '/chat', json={'message': 'لو الحد الائتماني قل 10% إيه اللي هيحصل؟'}))
    assert reply['action'] == 'whatif' and reply['provider'] == 'rules'
    assert [row['role'] for row in json_response(client.get(root + '/chat/history'))] == ['user', 'assistant']

    plan = json_response(client.post(root + '/plans', json={'run_id': run_id, 'variant': 'optimized'}))
    plan_url = f"/api/plans/{plan['id']}"
    assert json_response(client.get(plan_url + '/export.xlsx'), 409)['detail']['code'] == 'plan_not_approved'
    decision = json_response(client.post(plan_url + '/decide', json={
        'decision': 'approved', 'comment': 'تمت المراجعة',
    }))
    assert json_response(client.get(root + '/plans')) == [decision]
    formats = ['xlsx', 'csv', 'json'] + (['p6.csv', 'p6.xml'] if sector == 'contracting' else [])
    for format_name in formats:
        file = client.get(f'{plan_url}/export.{format_name}')
        assert file.status_code == 200 and 'attachment;' in file.headers['content-disposition']
        if format_name == 'xlsx':
            with BytesIO(file.content) as stream:
                workbook = load_workbook(stream, read_only=True)
                assert 'KPIs' in workbook.sheetnames
                workbook.close()
    assert any(row['action'] == 'plan.approve' for row in json_response(client.get(root + '/audit')))

    sample = STATIC_DIR / 'templates' / f'actuals_{sector}_template.csv'
    actuals = json_response(client.post(root + '/actuals/upload', data={'plan_id': plan['id']},
                                        files={'file': (sample.name, sample.read_bytes())}))
    assert actuals['comparison']['rows'] and json_response(client.get(root + '/actuals/latest')) == actuals
    calibration = json_response(client.post(root + '/recalibrate'))
    assert calibration['calibrations'] and json_response(client.get(root + '/calibrations'))
