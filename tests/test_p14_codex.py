import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import subprocess
from threading import Event
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.enterprise.demo import demo_template
from app.enterprise.whatif import ImpactModel
from app.ingestion.loader import load_demo
from app.llm import codex_cli, service
from app.llm.enterprise import EXAMPLE
from app.llm.provider import get_provider
from app.llm.schema import ChatContext
from app.main import create_app
from conftest import sign_in


def event_stream(content):
    return '\n'.join(map(json.dumps, (
        {'type': 'thread.started', 'thread_id': 'fake'},
        {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': content}},
        {'type': 'turn.completed'},
    )))


def response(command, data):
    return subprocess.CompletedProcess(command, 0, event_stream(json.dumps(data, ensure_ascii=False)), '')


@pytest.fixture
def codex_settings(tmp_path, db_path, monkeypatch):
    monkeypatch.delenv('LLM_PROVIDER', raising=False)
    monkeypatch.setenv('CODEX_HOME', str(tmp_path / 'isolated'))
    monkeypatch.setattr(codex_cli, 'find_codex', lambda: tmp_path / 'bin/codex')
    return Settings(db_path=db_path, forecast_engine='stat')


@pytest.fixture
def mocked_cli(monkeypatch):
    def install(*outputs):
        pending = iter(outputs)

        def run(command, **kwargs):
            output = next(pending)
            if isinstance(output, Exception):
                raise output
            if isinstance(output, str):
                return subprocess.CompletedProcess(command, 0, event_stream(output), '')
            return response(command, output)

        monkeypatch.setattr(codex_cli, '_run', run)
    return install


def test_default_home_and_provider(monkeypatch):
    monkeypatch.delenv('LLM_PROVIDER', raising=False)
    monkeypatch.delenv('CODEX_HOME', raising=False)
    settings = Settings()
    assert settings.codex_home == Path.home() / '.codex'
    assert get_provider(settings).name == 'codex'
    monkeypatch.setenv('CODEX_HOME', '')
    assert Settings().codex_home == settings.codex_home


@pytest.mark.parametrize('path', ['.nvm/versions/node/v22/bin/codex', '.bun/bin/codex'])
def test_launchd_lookup(tmp_path, monkeypatch, path):
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('PATH', '/usr/bin:/bin')
    monkeypatch.setattr(codex_cli.shutil, 'which', lambda name: None)
    cli = tmp_path / path
    cli.parent.mkdir(parents=True)
    cli.touch(mode=0o700)
    assert codex_cli.find_codex() == cli


@pytest.mark.parametrize('schema_supported', [True, False])
def test_cli_flags_strict_schema_and_old_cli_retry(codex_settings, monkeypatch, schema_supported):
    calls = []

    def run(command, *, cwd, env, input, timeout, **kwargs):
        calls.append(command)
        assert command[1] == 'exec' and command[-1] == '-'
        assert '--json' in command and '--skip-git-repo-check' in command
        assert '--ignore-user-config' in command and '--ephemeral' in command
        assert '-m' not in command and 'shell_tool' in command
        assert env['CODEX_HOME'] == str(codex_settings.codex_home)
        assert env['PATH'].split(os.pathsep)[0] == str(Path(command[0]).parent)
        assert 0 < timeout <= 90
        assert 'مجمع النخيل' in input and 'كام التكلفة؟' not in ' '.join(command)
        if '--output-schema' in command:
            schema_path = Path(command[command.index('--output-schema') + 1])
            assert schema_path.parent == Path(cwd)
            schema = json.loads(schema_path.read_text())
            assert set(schema['required']) == schema['properties'].keys()
            assert schema['additionalProperties'] is False
            assert 'anyOf' in schema['properties']['changes']['items']
            if not schema_supported:
                return subprocess.CompletedProcess(command, 2, '', "error: unexpected argument '--output-schema' found")
        return response(command, {'template': 'contracting', 'intent': 'query', 'query_kpi': 'total'})

    monkeypatch.setattr(codex_cli, '_run', run)
    provider = get_provider(codex_settings)
    scenario = provider.parse('كام التكلفة؟', ChatContext('contracting', load_demo('contracting')))
    assert scenario.query_kpi == 'total'
    assert provider.name == 'codex'
    assert len(calls) == (1 if schema_supported else 2)


@pytest.mark.parametrize('bad_output', [
    subprocess.TimeoutExpired('codex', 90),
    subprocess.CalledProcessError(1, 'codex'),
    'تفسير ثم {"template":"contracting","intent":"help"}',
    '```json\n{"template":"contracting","intent":"help"}\n```',
    '{"template":"contracting","template":"contracting","intent":"help"}',
    '{"template":"contracting","intent":"help","confidence":NaN}',
    {'template': 'production', 'intent': 'help'},
    {'template': 'contracting', 'intent': 'explain', 'subject': 'Z'},
    {'template': 'contracting', 'intent': 'what_if',
     'changes': [{'type': 'deadline_shift', 'project': 'Z', 'weeks': 1}]},
    {'template': 'contracting', 'intent': 'what_if', 'changes': [{'type': 'line_down', 'line': 'L1', 'from_day': 0, 'days': 1}]},
])
def test_codex_errors_fall_back_for_whole_request(codex_settings, mocked_cli, bad_output):
    mocked_cli(bad_output)
    provider = get_provider(codex_settings)
    scenario = provider.parse('كام إجمالي التكلفة؟', ChatContext('contracting', load_demo('contracting')))
    assert scenario.query_kpi == 'total'
    assert provider.name == 'rules(fallback)'
    assert provider.answer('كام؟', None, {'answer_ar': 'التكلفة 100 ريال.'}) == 'التكلفة 100 ريال.'


def test_missing_executable(codex_settings, monkeypatch):
    monkeypatch.setattr(codex_cli, 'find_codex', lambda: None)
    provider = get_provider(codex_settings)
    assert provider.explain({'amount': 20}, 'التكلفة 20 ريال.') == 'التكلفة 20 ريال.'
    assert provider.name == 'rules(fallback)'


def test_completion_requires_successful_turn_event():
    with pytest.raises(ValueError):
        codex_cli.final_message('{"type":"item.completed","item":{"type":"agent_message","text":"{}"}}')


def test_process_timeout_terminates_the_launcher_group(monkeypatch):
    process = MagicMock(pid=123)
    process.__enter__.return_value = process
    process.communicate.side_effect = subprocess.TimeoutExpired('codex', 90)
    popen = MagicMock(return_value=process)
    kill_group = MagicMock()
    monkeypatch.setattr(codex_cli.subprocess, 'Popen', popen)
    monkeypatch.setattr(codex_cli.os, 'killpg', kill_group)
    with pytest.raises(subprocess.TimeoutExpired):
        codex_cli._run(['codex'], cwd='/tmp', env={}, input='fake', timeout=90)
    assert popen.call_args.kwargs['start_new_session']
    process.communicate.assert_called_once_with('fake', timeout=90)
    kill_group.assert_called_once_with(process.pid, codex_cli.signal.SIGKILL)
    process.wait.assert_called_once()


def test_calls_serialize_across_instances(codex_settings, monkeypatch):
    started, release, second_ready, second_entered = Event(), Event(), Event(), Event()

    def run(command, *, input, **kwargs):
        if input == 'first':
            started.set()
            assert release.wait(3)
        else:
            second_entered.set()
        return response(command, {'reply_ar': 'الرد'})

    def second():
        second_ready.set()
        return codex_cli.complete(codex_settings.codex_home, 'second', {})

    monkeypatch.setattr(codex_cli, '_run', run)
    with ThreadPoolExecutor(2) as executor:
        first_result = executor.submit(codex_cli.complete, codex_settings.codex_home, 'first', {})
        assert started.wait(3)
        second_result = executor.submit(second)
        try:
            assert second_ready.wait(3)
            assert not second_entered.wait(.05)
        finally:
            release.set()
        assert second_result.result(timeout=3) == first_result.result(timeout=3)
    assert second_entered.is_set()


@pytest.mark.parametrize('response_ar,valid', [
    ('التكلفة وصلت إلى ١٬٧٠٦٫٣ ريال مقابل ١٬٠٠٠ ريال.', True),
    ('التكلفة وصلت إلى 1706.3 ريال مقابل 998 ريال.', False),
    ('التكلفة وصلت إلى 1000 ريال مقابل 1706.3 ريال.', False),
    ('التكلفة وصلت إلى 1706.3 ريال.', False),
    ('التكلفة وصلت إلى 1706.3 مليون ريال مقابل 1000 ريال.', False),
])
def test_reply_guard(codex_settings, mocked_cli, response_ar, valid):
    mocked_cli({'reply_ar': response_ar})
    provider = get_provider(codex_settings)
    grounded = 'التكلفة 1,706.3 ريال مقابل 1,000 ريال.'
    actual = provider.answer('كام؟', None, {'answer_ar': grounded})
    assert actual == (response_ar if valid else grounded)
    assert provider.name == ('codex' if valid else 'rules(fallback)')


def test_enterprise_codex_multichange_uses_propagation(codex_settings, monkeypatch):
    def run(command, *, input, **kwargs):
        data = json.loads(input.split('\nInput: ', 1)[1])
        if 'drivers' in data:
            assert {'credit', 'supplier'} <= {row['id'] for row in data['drivers']}
            return response(command, {
                'template': 'enterprise', 'intent': 'what_if', 'assumptions': [
                    {'node_id': 'credit', 'operation': 'pct', 'value': -15, 'uncertainty': 0},
                    {'node_id': 'supplier', 'operation': 'add', 'value': 7, 'uncertainty': 0},
                ],
            })
        assert 'نتيجة' in data['grounded_ar']
        return response(command, {'reply_ar': data['grounded_ar']})

    monkeypatch.setattr(codex_cli, '_run', run)
    nodes = [
        {'id': key, 'name_ar': label, 'type': kind, 'unit': unit,
         'baseline': {'kind': 'scalar', 'value': value}, 'definition': {'kind': 'formula', 'expression': 'مرجع'}}
        for key, label, kind, unit, value in (
            ('credit', 'حد الائتمان', 'credit', 'SAR', 4000),
            ('supplier', 'مهلة المورد', 'supplier', 'days', 5),
            ('pnl.result', 'النتيجة بعد التمويل', 'profit_loss', 'SAR', 1000),
        )
    ]
    edges = [
        {'source': 'credit', 'target': 'pnl.result', 'name_ar': 'أثر التسهيل',
         'rule': {'multiplier': 0.5}},
        {'source': 'supplier', 'target': 'pnl.result', 'name_ar': 'أثر التوريد',
         'rule': {'multiplier': -10}},
    ]
    with TestClient(create_app(codex_settings)) as client:
        sign_in(client)
        saved = client.post('/api/enterprise/models', json={
            'name': 'شركة الاختبار', 'graph': {'nodes': nodes, 'edges': edges},
        })
        assert saved.status_code == 201, saved.text
        model_id = saved.json()['id']
        result = client.post('/api/enterprise/chat', json={
            'model_id': model_id, 'message': 'خفّض الائتمان 15% وأخّر التوريد أسبوع',
        }).json()
        assert result['provider'] == client.get('/api/health').json()['provider'] == 'codex'
        direct = client.post('/api/enterprise/whatif', json={
            'model_id': model_id, 'assumptions': result['scenario']['assumptions'],
        }).json()
    assert result['result']['nodes'] == direct['nodes']
    assert result['result']['assumptions'] == direct['assumptions']
    assert result['result']['simulation']['n'] == 200
    assert result['result']['simulation']['seed'] == 42
    assert '630 ريال' in result['reply_ar']
    assert result['result']['paths']
    assert result['version'] == 1


@pytest.mark.parametrize('bad_assumptions', [
    [{'node_id': 'unknown', 'value': 1}],
    [{'node_id': 'pnl.result', 'value': 10}],
    [{'node_id': 'credit', 'value': 1}, {'node_id': 'credit', 'value': 2}],
    [{'node_id': 'supplier', 'value': -5}],
])
def test_enterprise_bad_model_output_falls_back(codex_settings, mocked_cli, bad_assumptions):
    mocked_cli({'template': 'enterprise', 'intent': 'what_if', 'assumptions': bad_assumptions})
    provider = get_provider(codex_settings)
    model = ImpactModel(demo_template().graph, weeks=52)
    result = provider.parse_enterprise(EXAMPLE, model)
    assert provider.name == 'rules(fallback)'
    assert [(row.node_id, row.operation, row.value) for row in result.assumptions] == [
        ('credit', 'pct', -15), ('supplier', 'add', 14),
    ]


def test_rules_do_not_run_partially_parsed_enterprise_scenarios(client):
    for message in ('خفض حد الائتمان 15%، وبعدها اعمل حاجة تانية', 'خفض حد الائتمان ' + 400 * '9' + '%'):
        result = client.post('/api/enterprise/chat', json={'message': message})
        assert result.status_code == 200 and result.json()['result'] is None
        assert result.json()['scenario']['intent'] == 'help'


def test_enterprise_scopes_to_saved_version(client):
    saved = client.post('/api/enterprise/models/demo').json()
    stored = client.put(f"/api/enterprise/models/{saved['id']}", json={
        'expected_version': saved['version'], 'name': 'روافد الاختبار', 'graph': saved['graph'],
    }).json()
    assert stored['version'] == 2
    reply = client.post('/api/enterprise/chat', json={
        'message': 'ساعدني', 'model_id': saved['id'], 'version': 1,
    })
    assert reply.status_code == 200 and reply.json()['version'] == 1
    assert reply.json()['model_id'] == saved['id']
    assert client.post('/api/enterprise/chat', json={'message': 'ساعدني', 'version': 1}).status_code == 422


def test_first_sector_scenario_keeps_codex_out_of_the_write_transaction(codex_settings, mocked_cli, monkeypatch):
    mocked_cli({'template': 'contracting', 'intent': 'what_if', 'confidence': 0.9,
                'changes': [{'type': 'credit_limit_pct', 'value': -10}]})

    actual_optimize = service.run_optimization
    actual_whatif = service.run_whatif

    def baseline(connection, cid, body, settings, provider):
        assert provider.name == 'rules'
        return actual_optimize(connection, cid, body, settings, provider)

    def whatif(connection, company, body, settings, provider):
        assert provider.name == 'rules'
        assert body.changes[0].value == -10
        return actual_whatif(connection, company, body, settings, provider)

    monkeypatch.setattr(service, 'run_optimization', baseline)
    monkeypatch.setattr(service, 'run_whatif', whatif)
    with TestClient(create_app(codex_settings)) as client:
        sign_in(client)
        result = client.post('/api/companies/1/chat', json={'message': 'خفّض الائتمان 10%'}).json()
    assert result['provider'] == 'codex' and result['action'] == 'whatif' and result['run_id']
    assert 'الحد الائتماني اتغير' in result['reply_ar']
    assert {entry['metric'] for entry in result['evidence']} >= {'credit_limit', 'total_delta'}
