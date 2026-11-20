import json
import sqlite3
from datetime import date
from io import BytesIO

import httpx
import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError

from app.config import Settings
from app.connectors import erp
from app.db import get_conn
from app.delivery.export import render_export
from app.ingestion.loader import load_demo
from app.main import create_app
from app.optimization import contracting
from app.templates_registry import load_templates
from conftest import sign_in

REST_PATH = '/api/connectors/erp'
P6_PATH = '/api/companies/1/p6-imports'
COLUMNS = {'node': 'key', 'value': 'amount', 'date': 'as_of'}
P6_XML = b'''<APIBusinessObjects xmlns="http://xmlns.oracle.com/Primavera/P6/V24.12/API/BusinessObjects">
    <Project><ObjectId>10</ObjectId><Id>P-1</Id><Name>Build</Name>
        <WBS><ObjectId>90</ObjectId><Code>WBS</Code></WBS>
        <Activity><ObjectId>101</ObjectId><Id>A</Id><WBSObjectId>90</WBSObjectId>
            <OriginalDuration>8.5</OriginalDuration><BudgetedTotalCost>300.75</BudgetedTotalCost>
            <ActualTotalCost>125.25</ActualTotalCost></Activity>
    </Project>
    <Project><ObjectId>11</ObjectId><Id>P-2</Id></Project>
    <Activity><ObjectId>102</ObjectId><Id>B</Id><ProjectObjectId>11</ProjectObjectId>
        <PlannedDuration>0</PlannedDuration><PlannedTotalCost>0</PlannedTotalCost></Activity>
    <Relationship><PredecessorActivityObjectId>101</PredecessorActivityObjectId>
        <SuccessorActivityObjectId>102</SuccessorActivityObjectId>
        <PredecessorProjectObjectId>10</PredecessorProjectObjectId><SuccessorProjectObjectId>11</SuccessorProjectObjectId>
        <RelationshipType>Start to Start</RelationshipType><Lag>-2.5</Lag></Relationship>
</APIBusinessObjects>'''


@pytest.fixture
def model_path(client):
    cases = [('cash', 'cash', {'kind': 'scalar', 'value': 50}),
             ('rate', 'pricing', {'kind': 'scalar', 'value': None}),
             ('orders', 'demand', {'kind': 'series', 'frequency': 'weekly',
                                    'points': [{'date': '2026-01-05', 'value': 90}]})]
    response = client.post('/api/enterprise/models', json={'name': 'شركة تجريبية', 'graph': {'nodes': [
        {'id': key, 'name_ar': key, 'type': kind, 'unit': 'SAR', 'baseline': baseline,
         'definition': {'kind': 'formula', 'expression': 'مدخل'}}
        for key, kind, baseline in cases
    ]}})
    assert response.status_code == 201
    return '/api/enterprise/models/' + str(response.json()['id'])


def preview(client, model_path, content, *, filename='import.csv', sheet='CSV', columns=COLUMNS):
    return client.post(model_path + '/imports/preview', data={'sheet': sheet, 'columns': json.dumps(columns)},
                       files={'file': (filename, content)})


def apply(client, model_path, draft):
    return client.post(model_path + '/imports/apply', json={
        'expected_version': draft['version'], 'updates': draft['updates'],
    })


def baselines(snapshot):
    return {node['id']: node['baseline'] for node in snapshot['graph']['nodes']}


def test_csv_preview_then_apply_feeds_impact_engine(client, model_path):
    data = 'key,amount,as_of,ignore\ncash,١٧٥٫٥,,note\nrate,0,,text\n'.encode('utf-8-sig')
    inspect = client.post('/api/enterprise/imports/inspect', files={'file': ('input.csv', data)})
    assert inspect.json()['sheets'] == [{'name': 'CSV', 'columns': ['key', 'amount', 'as_of', 'ignore']}]
    response = preview(client, model_path, data)
    assert response.status_code == 200
    draft = response.json()
    assert draft['validation'] == {'ok': True, 'errors': [], 'summary': {'rows': 2, 'nodes': 2}}
    assert client.get(model_path).json()['version'] == 1
    saved = apply(client, model_path, draft).json()
    assert saved['version'] == 2
    assert baselines(saved)['cash']['value'] == 175.5
    assert baselines(saved)['rate']['value'] == 0
    assert baselines(client.get(model_path + '/versions/1').json())['cash']['value'] == 50
    assert apply(client, model_path, draft).status_code == 409
    assert preview(client, model_path, data).json()['updates'] == []
    impact = client.post('/api/enterprise/whatif', json={'model_id': saved['id'], 'n': 1, 'assumptions': [
        {'node_id': 'cash', 'operation': 'add', 'value': 2},
    ]})
    assert impact.status_code == 200, impact.text
    cash = next(node for node in impact.json()['nodes'] if node['id'] == 'cash')
    assert (cash['before'], cash['after']) == (175.5, 177.5)


def test_xlsx_workbook_selection_and_series_replacement(client, model_path):
    book = Workbook()
    book.active.title = 'ملحوظة'
    sheet = book.create_sheet('القياسات')
    for row in [('المعرّف', 'الرصيد', 'تاريخ'), ('orders', 66, date(2026, 3, 9)),
                ('orders', 32, date(2026, 3, 2)), ('cash', -10, None)]:
        sheet.append(row)
    stream = BytesIO()
    book.save(stream)
    content = stream.getvalue()
    inspect = client.post('/api/enterprise/imports/inspect', files={'file': ('demo.XLSX', content)})
    assert inspect.json()['sheets'][1] == {'name': 'القياسات', 'columns': ['المعرّف', 'الرصيد', 'تاريخ']}
    columns = {'node': 'المعرّف', 'value': 'الرصيد', 'date': 'تاريخ'}
    response = preview(client, model_path, content, filename='demo.XLSX', sheet='القياسات', columns=columns)
    saved = apply(client, model_path, response.json()).json()
    assert baselines(saved)['orders'] == {'kind': 'series', 'frequency': 'weekly', 'points': [
        {'date': '2026-03-02', 'value': 32}, {'date': '2026-03-09', 'value': 66},
    ]}
    assert baselines(saved)['cash']['value'] == -10


@pytest.mark.parametrize(('payload', 'row', 'column'), [
    ('cash,inf,', 2, 'amount'),
    ('missing,10,', 2, 'key'),
    ('orders,10,', 2, 'as_of'),
    ('orders,10,not-a-date', 2, 'as_of'),
    ('orders,10,2026-01-05\norders,12,2026-01-05', 3, 'as_of'),
    ('cash,2,2026-01-05', 2, 'as_of'),
    ('cash,10,\ncash,10,', 3, 'key'),
    ('', 0, ''),
])
def test_row_validation_blocks_apply(client, model_path, payload, row, column):
    draft = preview(client, model_path, ('key,amount,as_of\n' + payload).encode()).json()
    assert draft['updates'] == []
    assert draft['validation']['ok'] is False
    assert [(issue['row'], issue['column']) for issue in draft['validation']['errors']] == [(row, column)]
    assert apply(client, model_path, draft).status_code == 422
    assert client.get(model_path).json()['version'] == 1


def test_mapping_and_file_validation(client, model_path):
    data = b'key,amount,amount\ncash,42,44\n'
    result = preview(client, model_path, data, columns={'node': 'key', 'value': 'amount'}).json()
    assert [(error['row'], error['column']) for error in result['validation']['errors']] == [(1, 'amount')]
    result = preview(client, model_path, b'key,amount\ncash,42\n',
                     columns={'node': 'key', 'value': 'key'}).json()
    assert result['validation']['ok'] is False
    result = preview(client, model_path, b'key,amount\ncash,42\n', sheet='wrong')
    assert result.status_code == 422
    assert preview(client, model_path, b'not a workbook', filename='bad.xlsx').status_code == 422
    assert preview(client, model_path, b'key,amount\ncash,42,extra\n').status_code == 422
    assert preview(client, model_path, b'key,amount\n' + b'cash,12\n' * 5001).status_code == 422


@pytest.mark.parametrize('invalid', [
    [{'node_id': 'missing', 'baseline': {'kind': 'scalar', 'value': 1}}],
    [{'node_id': 'cash', 'baseline': {'kind': 'scalar', 'value': None}}],
    [{'node_id': 'cash', 'baseline': {'kind': 'scalar', 'value': 1}}] * 2,
    [{'node_id': 'orders', 'baseline': {'kind': 'series', 'frequency': 'daily',
                                       'points': [{'date': '2026-01-05', 'value': 1}]}}],
])
def test_apply_rechecks_contract(client, model_path, invalid):
    result = apply(client, model_path, {'version': 1, 'updates': invalid})
    assert result.status_code == 422
    assert client.get(model_path).json()['version'] == 1


@pytest.fixture
def mock_erp(monkeypatch):
    """Serve the ERP protocol in-process so pytest does not need sockets."""
    state = {'rows': [{'key': 'cash', 'amount': 280}], 'calls': [], 'status': '200 OK'}

    def mock_server(environ, respond):
        state['calls'].append((environ['REQUEST_METHOD'], environ['PATH_INFO'], environ.get('HTTP_AUTHORIZATION')))
        respond(state['status'], [('Content-Type', 'application/json'), ('Location', 'https://example.test/redirect')])
        return [json.dumps({'data': {'rows': state['rows']}}).encode()]

    real_client = httpx.Client
    monkeypatch.setattr(erp.httpx, 'Client', lambda **kwargs: real_client(
        transport=httpx.WSGITransport(app=mock_server), **kwargs))
    monkeypatch.setenv('ATHAR_ERP_REST', 'https://example.test/metrics?key=secret-url-marker')
    monkeypatch.setenv('ATHAR_ERP_TOKEN', 'secret-token-marker')
    return state


@pytest.fixture
def rest_config():
    return {'kind': 'rest', 'name': 'ERP REST', 'url_env': 'ATHAR_ERP_REST', 'token_env': 'ATHAR_ERP_TOKEN',
            'rows_path': 'data.rows', 'mapping': {'node': 'key', 'value': 'amount'}}


def test_rest_server_config_persists_and_apply_uses_reviewed_values(client, db_path, model_path, mock_erp, rest_config):
    response = client.post(REST_PATH, json=rest_config)
    assert response.status_code == 201
    saved = response.json()
    path = REST_PATH + '/' + str(saved['id'])
    assert client.put(path, json={**rest_config, 'name': 'ERP مرجع'}).status_code == 200
    with TestClient(create_app(Settings(db_path=db_path, forecast_engine='stat'))) as restarted:
        sign_in(restarted)
        config = restarted.get(REST_PATH).json()[0]
        assert config == {**saved, 'name': 'ERP مرجع'}
        draft = restarted.post(model_path + f'/imports/erp/{saved["id"]}/preview').json()
        assert draft['validation']['ok']
        mock_erp['rows'][0]['amount'] = 770
        assert baselines(apply(restarted, model_path, draft).json())['cash']['value'] == 280
    assert mock_erp['calls'] == [('GET', '/metrics', 'Bearer secret-token-marker')]
    for file in db_path.parent.glob(db_path.name + '*'):
        assert b'secret-token-marker' not in file.read_bytes()
        assert b'secret-url-marker' not in file.read_bytes()


def test_rest_failures_do_not_leak_secrets_or_follow_redirects(client, model_path, mock_erp, rest_config, monkeypatch):
    connector_id = client.post(REST_PATH, json=rest_config).json()['id']
    path = model_path + f'/imports/erp/{connector_id}/preview'
    mock_erp['status'] = '302 Found'
    response = client.post(path)
    assert response.status_code == 502 and 'secret-' not in response.text
    assert len(mock_erp['calls']) == 1
    mock_erp['status'] = '200 OK'
    mock_erp['rows'] = ['wrong shape']
    assert client.post(path).status_code == 502
    monkeypatch.delenv('ATHAR_ERP_TOKEN')
    assert client.post(path).json()['detail']['code'] == 'erp_env_missing'
    assert client.post(REST_PATH, json={**rest_config, 'token': 'secret-token-marker'}).status_code == 422
    assert client.post(REST_PATH, json={**rest_config, 'url_env': 'https://plain.test/key'}).status_code == 422


def test_sqlalchemy_reads_sqlite_view_without_write_access(client, tmp_path, model_path, monkeypatch):
    source = tmp_path / 'ERP source.sqlite'
    with sqlite3.connect(source) as database:
        database.executescript("""CREATE TABLE source (identifier TEXT, amount NUMERIC, irrelevant TEXT);
                INSERT INTO source VALUES ('cash', 550, 'private-only');
                CREATE VIEW source_view AS SELECT * FROM source;""")
    url = 'sqlite:///' + str(source)
    monkeypatch.setenv('ATHAR_ERP_SQL', url)
    config = {'kind': 'sql', 'name': 'ERP SQL', 'url_env': 'ATHAR_ERP_SQL', 'table': 'source_view',
              'mapping': {'node': 'identifier', 'value': 'amount'}}
    connector = client.post(REST_PATH, json=config).json()['id']
    path = model_path + f'/imports/erp/{connector}/preview'
    result = client.post(path)
    assert result.status_code == 200, result.text
    assert 'private-only' not in result.text
    assert baselines(apply(client, model_path, result.json()).json())['cash']['value'] == 550
    engine = create_engine(erp.sqlite_read_only(make_url(url)))
    try:
        with engine.connect() as read_only, pytest.raises(OperationalError, match='readonly'):
            read_only.exec_driver_sql('UPDATE source SET amount=0')
    finally:
        engine.dispose()
    with sqlite3.connect(source) as database:
        assert database.execute('SELECT amount FROM source').fetchone() == (550,)
    assert client.post(REST_PATH, json={**config, 'table': 'source; DELETE FROM source'}).status_code == 422
    assert client.put(REST_PATH + f'/{connector}', json={
        **config, 'mapping': {'node': 'identifier', 'value': 'missing_secret_column'},
    }).status_code == 200
    assert client.post(path).status_code == 502
    missing_source = tmp_path / 'not-created.sqlite'
    monkeypatch.setenv('ATHAR_ERP_SQL', 'sqlite:///' + str(missing_source))
    assert client.post(path).status_code == 502
    assert not missing_source.exists()


def upload_p6(client, content, *, commit=False, path=P6_PATH):
    return client.post(path, files={'file': ('project.xml', content)}, data={'commit': str(commit)})


def test_namespaced_p6_normalizes_preserves_and_round_trips(client, db_path):
    validation = upload_p6(client, P6_XML).json()
    assert validation['id'] is None
    assert validation['validation'] == {'ok': True, 'errors': [],
                                        'summary': {'projects': 2, 'wbs': 1, 'activities': 2, 'relationships': 1}}
    assert client.get(P6_PATH).json() == []
    saved = upload_p6(client, P6_XML, commit=True).json()
    path = P6_PATH + '/' + str(saved['id'])
    with TestClient(create_app(Settings(db_path=db_path, forecast_engine='stat'))) as restarted:
        sign_in(restarted)
        assert restarted.get(P6_PATH).json()[0]['id'] == saved['id']
        detail = restarted.get(path).json()['schedule']
        assert detail == validation['schedule']
        assert detail['activities'][1]['actual_cost'] == 125.25
        assert detail['activities'][1]['duration_hours'] == 8.5
        assert detail['activities'][1]['planned_cost'] == 300.75
        assert detail['activities'][0]['duration_hours'] == 0
        assert detail['relationships'][0]['lag_hours'] == -2.5
        assert detail['relationships'][0]['type'] == 'Start to Start'
        output = restarted.get(path + '/export.p6.xml')
        assert output.headers['content-disposition'].startswith('attachment;')
        assert output.content == P6_XML
        assert upload_p6(restarted, output.content).json()['schedule'] == detail
        assert restarted.get(path.replace('/1/', '/2/')).status_code == 404
    with get_conn(db_path) as db:
        assert db.execute('SELECT COUNT(*) FROM audit_log WHERE action=?', ('p6.import',)).fetchone()[0] == 1


def test_existing_plan_p6_export_round_trip_with_costs(client):
    dataset = load_demo('contracting')
    defaults = load_templates()['contracting'].model_defaults()['parameters']
    problem = contracting.prepare(dataset, defaults)
    solution = contracting.evaluate(problem, contracting.baseline(problem))
    content = render_export('p6.xml', {'id': 1}, 'contracting', solution['plan'], solution['kpis'], dataset, defaults)
    result = upload_p6(client, content).json()
    assert result['validation']['ok'], result
    activities = {activity['id']: activity for activity in result['schedule']['activities']}
    assert len(activities) == 15 and len(result['schedule']['relationships']) == 10
    for row in dataset['activities']:
        activity = activities[row['id']]
        assert activity['duration_hours'] == row['duration_weeks'] * 7 * 8
        assert activity['planned_cost'] == row['duration_weeks'] * row['cost_per_week'] * 1000
    assert result['schedule']['relationships'][0]['predecessor_object_id'] == activities['A0']['object_id']
    assert upload_p6(client, content, path='/api/companies/2/p6-imports').status_code == 422


def test_p6_invalid_references_and_forbidden_dtd_never_write(client):
    wrong = P6_XML.replace(b'>102</SuccessorActivityObjectId>', b'>999</SuccessorActivityObjectId>')
    result = upload_p6(client, wrong, commit=True).json()
    assert result['validation']['ok'] is False and result['id'] is None
    assert result['validation']['errors'][0]['column'] == 'successor'
    doctype = b'<!DOCTYPE APIBusinessObjects [<!ENTITY test SYSTEM "file:///etc/passwd">]>'
    assert upload_p6(client, doctype + P6_XML, commit=True).status_code == 422
    assert client.get(P6_PATH).json() == []
