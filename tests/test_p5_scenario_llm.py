import json

import httpx
import pytest
from fastapi.testclient import TestClient

from conftest import sign_in

from app.config import Settings
from app.db import get_conn
from app.ingestion.loader import load_demo
from app.llm.explain import number_guard
from app.llm.provider import OpenAICompatProvider, get_provider
from app.llm.rules_ar import parse
from app.llm.schema import ChatContext
from app.main import create_app
from app.simulation.scenario import apply_changes
from app.templates_registry import load_templates


@pytest.fixture
def contracting_ctx():
    return ChatContext('contracting', load_demo('contracting'))


@pytest.fixture
def production_ctx():
    return ChatContext('production', load_demo('production'))


@pytest.fixture(autouse=True)
def disallow_live_provider(monkeypatch):
    def fail(*args, **kwargs):
        pytest.fail('Unexpected live provider request')

    monkeypatch.setattr(httpx, 'post', fail)


@pytest.mark.parametrize('sector,message,expected', [
    ('contracting', 'لو الحد الائتماني قل 10% إيه اللي هيحصل؟', {'type': 'credit_limit_pct', 'value': -10}),
    ('contracting', 'نقص حد الائتمان ١٠٪', {'type': 'credit_limit_pct', 'value': -10}),
    ('contracting', 'خفض الائتمان بنسبة عشرة في المية', {'type': 'credit_limit_pct', 'value': -10}),
    ('production', 'زاد الحد الائتماني 20%', {'type': 'credit_limit_pct', 'value': 20}),
    ('contracting', 'الحد الائتماني يبقى 3500', {'type': 'credit_limit_abs', 'value': 3500}),
    ('contracting', 'حد ائتمان 3,500 ألف', {'type': 'credit_limit_abs', 'value': 3500}),
    ('contracting', 'فريق الهيكل مش موجود من الأسبوع 5 لـ 8',
     {'type': 'crew_unavailable', 'resource': 'R2', 'from': 5, 'to': 8, 'units': 1}),
    ('contracting', 'لو طاقم أساسات غاب أسبوعين',
     {'type': 'crew_unavailable', 'resource': 'R1', 'from': 0, 'to': 1}),
    ('contracting', 'نقص فريق من R2', {'type': 'crew_unavailable', 'resource': 'R2', 'from': 0, 'to': 0}),
    ('contracting', 'لو موعد مشروع B اتأخر أسبوعين', {'type': 'deadline_shift', 'project': 'B', 'weeks': 2}),
    ('contracting', 'قدّم تسليم C أسبوع', {'type': 'deadline_shift', 'project': 'C', 'weeks': -1}),
    ('contracting', 'غرامة B تبقى 30', {'type': 'ld_change', 'project': 'B', 'value': 30}),
    ('contracting', 'لو غرامة التأخير لمشروع D 80 في الأسبوع', {'type': 'ld_change', 'project': 'D', 'value': 80}),
    ('contracting', 'أجّل مشروع B أسبوعين', {'type': 'project_start_delay', 'project': 'B', 'weeks': 2}),
    ('contracting', 'ابدأ D بعد 3 أسابيع', {'type': 'project_start_delay', 'project': 'D', 'weeks': 3}),
    ('contracting', 'لو العميل بيدفع بعد 6 أسابيع بدل 4', {'type': 'payment_lag', 'value': 6, 'relative': False}),
    ('contracting', 'التحصيل اتأخر أسبوعين', {'type': 'payment_lag', 'value': 2, 'relative': True}),
    ('contracting', 'الهامش 10% بدل 15', {'type': 'margin_pct', 'value': 10}),
    ('production', 'لو الطلب زاد 20%', {'type': 'demand_pct', 'value': 20, 'sku': None}),
    ('production', 'الطلب على S3 نقص 15 بالمية', {'type': 'demand_pct', 'value': -15, 'sku': 'S3'}),
    ('production', 'الطلب على عصير برتقال 1ل نقص نصف بالمائة', {'type': 'demand_pct', 'value': -50, 'sku': 'S1'}),
    ('production', 'لو الخط 1 وقف يومين', {'type': 'line_down', 'line': 'L1', 'from_day': 0, 'days': 2}),
    ('production', 'خط L2 عطلان من يوم 3 لمدة 2 أيام', {'type': 'line_down', 'line': 'L2', 'from_day': 3, 'days': 2}),
    ('production', 'عطل في الخط الأول 48 ساعة', {'type': 'line_down', 'line': 'L1', 'from_day': 0, 'days': 2}),
    ('production', 'المخزن المبرد نقص 20%', {'type': 'storage_pct', 'value': -20}),
    ('production', 'سعة التبريد زادت 30%', {'type': 'storage_pct', 'value': 30}),
    ('production', 'لو شحنة O5 اتقدمت يومين', {'type': 'order_due_shift', 'order': 'O5', 'days': -2}),
    ('production', 'موعد الطلب O3 اتأخر 3 أيام', {'type': 'order_due_shift', 'order': 'O3', 'days': 3}),
    ('production', 'تكلفة التغيير زادت 50%', {'type': 'changeover_cost_pct', 'value': 50}),
    ('contracting', 'ركّز على الغرامات أكثر', {'type': 'weight_change', 'name': 'ld', 'value': 2.0, 'relative': True}),
    ('contracting', 'قلل أهمية التمويل', {'type': 'weight_change', 'name': 'financing', 'value': .5, 'relative': True}),
    ('production', 'وزن التخزين 2', {'type': 'weight_change', 'name': 'holding', 'value': 2}),
    ('contracting', 'أجّل مجمع النخيل اتنين أسبوع', {'type': 'project_start_delay', 'project': 'B', 'weeks': 2}),
])
def test_rule_changes(sector, message, expected):
    ctx = ChatContext(sector, load_demo(sector))
    scenario = parse(message, ctx)
    assert scenario.intent == 'what_if'
    assert len(scenario.changes) == 1
    change = scenario.changes[0].model_dump(by_alias=True)
    assert {key: change[key] for key in expected} == expected


@pytest.mark.parametrize('message,intent,subject,kpi', [
    ('ليه أخّرت B؟', 'explain', 'B', None),
    ('لماذا تأخر مشروع D', 'explain', 'D', None),
    ('ليه الخطة دي أحسن', 'explain', None, None),
    ('اشرح النتيجة', 'explain', None, None),
    ('كام إجمالي التكلفة؟', 'query', None, 'total'),
    ('أعلى عجز نقدي قد إيه؟', 'query', None, 'peak_od'),
    ('كام مشروع متأخر', 'query', None, 'late'),
    ('إيه الغرامات', 'query', None, 'ld'),
    ('كام تغيير خط', 'query', None, 'changeovers'),
    ('قارن الخطة بالأساس', 'compare', None, None),
    ('الفرق بين الحالي والمحسّن', 'compare', None, None),
    ('شغّل التحسين', 'optimize', None, None),
    ('اعمل خطة جديدة', 'optimize', None, None),
    ('الجو عامل إيه؟', 'help', None, None),
])
def test_rule_intents(contracting_ctx, message, intent, subject, kpi):
    scenario = parse(message, contracting_ctx)
    assert (scenario.intent, scenario.subject, scenario.query_kpi) == (intent, subject, kpi)
    if intent == 'help':
        assert scenario.echo_ar.count('•') == 5


@pytest.mark.parametrize('joiner', ['، ', ', ', ' ثم ', ' و', ' و '])
def test_multi_change(production_ctx, joiner):
    scenario = parse(f'الحد الائتماني يبقى 150,000{joiner}الطلب زاد ٢٠٪', production_ctx)
    assert [(change.type, change.value) for change in scenario.changes] == [
        ('credit_limit_abs', 150000), ('demand_pct', 20),
    ]


def test_apply_contracting_changes(contracting_ctx):
    ctx = contracting_ctx
    model = load_templates()[ctx.template].model_defaults()
    changes = parse('نقص حد الائتمان ١٠٪ ثم التحصيل اتأخر أسبوعين ثم الهامش 10% بدل 15 ثم '
                    'فريق الهيكل مش موجود من الأسبوع 5 لـ 8 ثم غرامة B تبقى 30 ثم '
                    'لو موعد مشروع B اتأخر أسبوعين ثم أجّل مشروع B أسبوعين ثم قلل أهمية التمويل', ctx).changes
    data, revised, labels = apply_changes(ctx.dataset, model, changes, template_id=ctx.template)
    assert len(labels) == 8
    assert data['projects'][1] == ctx.dataset['projects'][1] | {'ld_per_week': 30, 'deadline_week': 32, 'nta_week': 4}
    assert revised['parameters'] == model['parameters'] | {
        'credit_limit': 3600, 'payment_lag_weeks': 6, 'margin': 1.1,
        'capacity_override': {'R2': {str(week): 2 for week in range(5, 9)}},
    }
    assert revised['weights']['financing'] == .5
    assert model['parameters']['capacity_override'] == {}
    assert ctx.dataset['projects'][1]['ld_per_week'] == 60


def test_apply_production_changes(production_ctx):
    ctx = production_ctx
    model = load_templates()[ctx.template].model_defaults()
    initial = ctx.dataset['orders'][0]['qty']
    changes = parse('الطلب على S1 زاد 20% ثم سعة التبريد نقصت 20% ثم تكلفة التغيير زادت 50% ثم '
                    'خط L1 عطلان من يوم 3 لمدة 2 أيام ثم موعد الطلب O1 اتأخر يومين ثم وزن التخزين 2', ctx).changes
    data, revised, labels = apply_changes(ctx.dataset, model, changes, template_id=ctx.template)
    assert len(labels) == 6
    assert data['orders'][0]['qty'] == initial * 1.2
    assert data['orders'][0]['due_day'] == ctx.dataset['orders'][0]['due_day'] + 2
    assert data['orders'][1]['qty'] == ctx.dataset['orders'][1]['qty']
    assert [line['changeover_cost'] for line in data['lines']] == [2250, 3000]
    assert revised['parameters']['cold_capacity'] == 20000
    assert revised['parameters']['line_down'] == {'L1': [3, 4]}
    assert revised['weights']['holding'] == 2
    assert model['parameters']['line_down'] == {}


def optimize(client, cid, **body):
    response = client.post(f'/api/companies/{cid}/optimize', json=body)
    assert response.status_code == 200, response.text
    return response.json()


def whatif(client, cid, base_run_id, *changes):
    return client.post(f'/api/companies/{cid}/whatif', json={'base_run_id': base_run_id, 'changes': changes})


def chat(client, cid, message):
    response = client.post(f'/api/companies/{cid}/chat', json={'message': message})
    assert response.status_code == 200, response.text
    return response.json()


def test_contracting_whatif_and_optimize_explanation(client, db_path):
    base = optimize(client, 1, weights={'financing': 0.75})
    for key in ('baseline', 'optimized'):
        assert str(base[key]['kpis']['total']) in base['explanation_ar'].replace(',', '')
    base_id = base['run_id']
    assert client.put('/api/companies/1/model', json={'parameters': {'credit_limit': 9999}}).status_code == 200
    response = whatif(client, 1, base_id, {'type': 'credit_limit_pct', 'value': -10})
    assert response.status_code == 200, response.text
    result = response.json()
    kpis = result['scenario']['kpis']
    assert kpis['peak_od'] <= 3600 or kpis['breach'] > 0
    assert result['diff']['total']['base'] == base['optimized']['kpis']['total']
    assert result['diff']['peak_od']['scenario'] == kpis['peak_od']
    assert '3,600' in result['explanation_ar']
    run = client.get(f'/api/runs/{result["run_id"]}').json()
    original = client.get(f'/api/runs/{base_id}').json()
    with get_conn(db_path) as connection:
        stored = connection.execute('SELECT result_json FROM runs WHERE id = ?', (base_id,)).fetchone()
        assert json.loads(stored['result_json'])['effective_weights']['financing'] == .75
    assert run['base_run_id'] == base_id and run['model_id'] == original['model_id']
    assert run['scenario']['changes'][0]['value'] == -10
    assert client.get('/api/companies/1/model').json()['parameters']['credit_limit'] == 9999


def test_line_down_and_explanation(client):
    base = optimize(client, 2)
    baseline = base['baseline']['kpis']
    optimized = base['optimized']['kpis']
    assert baseline['total_cost'] == 22254.29
    assert base['solver']['status'] in ('OPTIMAL', 'FEASIBLE')
    assert optimized['feasible'] and optimized['credit_ok']
    assert optimized['total_cost'] < baseline['total_cost']
    assert optimized['late_orders'] <= baseline['late_orders']
    result = whatif(client, 2, base['run_id'], {'type': 'line_down', 'line': 'L1', 'from_day': 3, 'days': 2})
    assert result.status_code == 200, result.text
    result = result.json()
    kpis = result['scenario']['kpis']
    assert result['base']['kpis']['total_cost'] == optimized['total_cost']
    assert result['diff']['total_cost']['base'] == optimized['total_cost']
    assert result['diff']['total_cost']['scenario'] == kpis['total_cost']
    assert kpis['schedule']
    assert all(row['line'] != 'L1' or row['day'] not in (3, 4) for row in kpis['schedule'])
    assert 'L1' in result['explanation_ar']
    assert 'ذروة إشغال التبريد' in result['explanation_ar']


def test_whatif_validation(client):
    base = optimize(client, 1)['run_id']
    invalid = whatif(client, 1, base, {'type': 'deadline_shift', 'project': 'Z', 'weeks': 1})
    assert invalid.status_code == 422
    assert invalid.json()['detail']['code'] == 'unknown_entity'
    assert 'المشروع' in invalid.json()['detail']['msg_ar']
    invalid = whatif(client, 1, base, {'type': 'line_down', 'line': 'L1', 'from_day': 3, 'days': 2})
    assert invalid.status_code == 422
    assert whatif(client, 2, base, {'type': 'credit_limit_pct', 'value': -10}).status_code == 404
    assert whatif(client, 1, base, {'type': 'credit_limit_pct', 'value': '-10'}).status_code == 422
    assert client.get('/api/companies/1/runs/latest?kind=whatif').status_code == 404


def test_chat_whatif_explain_query_and_history(client):
    optimized = optimize(client, 1)
    base = optimized['run_id']
    changed = chat(client, 1, 'لو الحد الائتماني قل 10% إيه اللي هيحصل؟')
    assert changed['action'] == 'whatif' and changed['run_id'] > base
    assert 'الحد الائتماني' in changed['reply_ar']
    assert changed['provider'] == 'rules'
    assert changed['scenario']['changes'][0]['value'] == -10
    explained = chat(client, 1, 'ليه أخّرت B؟')
    assert explained['action'] == 'explain' and 'مجمع النخيل' in explained['reply_ar']
    answered = chat(client, 1, 'كام إجمالي التكلفة؟')
    assert answered['action'] == 'query' and answered['run_id'] == base
    assert str(optimized['optimized']['kpis']['total']) in answered['reply_ar'].replace(',', '')
    comparison = chat(client, 1, 'قارن الخطة بالأساس')
    assert comparison['intent'] == 'compare' and comparison['run_id'] == base
    assert [r['role'] for r in client.get('/api/companies/1/chat/history').json()] == ['user', 'assistant'] * 4
    assert client.get('/api/companies/2/chat/history').json() == []


def test_chat_optimize_and_help(client):
    response = chat(client, 1, 'شغّل التحسين')
    assert response['action'] == 'optimize' and response['run_id']
    response = chat(client, 1, 'الجو عامل إيه؟')
    assert response['intent'] == 'help' and response['action'] == 'none'
    assert response['reply_ar'].count('•') == 5


def test_template_exposes_discriminated_scenario_schema(client):
    payload = client.get('/api/templates/production').json()
    types = payload['scenario_schema']['properties']['changes']['items']['discriminator']['mapping']
    assert len(types) == 14 and 'line_down' in types


def test_number_guard():
    facts = {'total': 1706.3, 'delta': -10, 'other': ['L1', 25.0]}
    assert number_guard('الكلفة ١٬٧٠٦٫٣ والانخفاض −10٪، والخط L1 عند 25.00.', facts)
    assert not number_guard('الكلفة 1,706.3 ومعاها 98,765.', facts)
    assert not number_guard('الزيادة +10%.', facts)
    assert not number_guard('الرقم 2.5e9.', facts)
    assert not number_guard('الكلفة مليون ريال.', facts)


@pytest.mark.parametrize('message', ['قل الحد الائتماني 10', 'الطلب على المنتج المجهول زاد 20%'])
def test_ambiguous_change_does_not_get_applied(production_ctx, message):
    assert parse(message, production_ctx).intent == 'help'


@pytest.fixture
def remote_settings(db_path):
    return Settings(db_path=db_path, llm_provider='openai_compat', openrouter_api_key='mock-key',
                    llm_base_url='http://127.0.0.1:1/v1')


def test_provider_connect_failure_falls_back(monkeypatch, remote_settings):
    def unreachable(url, **kwargs):
        assert url == 'http://127.0.0.1:1/v1/chat/completions'
        assert kwargs['timeout'] == 20
        raise httpx.ConnectError('unreachable')

    monkeypatch.setattr(httpx, 'post', unreachable)
    with TestClient(create_app(remote_settings)) as client:
        sign_in(client)
        response = chat(client, 1, 'اشرح النتيجة')
        assert response['provider'] == 'rules(fallback)' and response['intent'] == 'explain'


@pytest.mark.parametrize('content', ['not json', '{"intent":"query","template":"wrong"}',
                                   '{"intent":"what_if","template":"contracting","changes":'
                                   '[{"type":"deadline_shift","project":"Z","weeks":1}]}'])
def test_provider_validation_failure_falls_back(monkeypatch, remote_settings, contracting_ctx, content):
    def mock_completion(url, **kwargs):
        assert kwargs['json']['response_format'] == {'type': 'json_object'}
        return httpx.Response(200, request=httpx.Request('POST', url),
                              json={'choices': [{'message': {'content': content}}]})

    monkeypatch.setattr(httpx, 'post', mock_completion)
    provider = get_provider(remote_settings)
    scenario = provider.parse('كام إجمالي التكلفة؟', contracting_ctx)
    assert scenario.query_kpi == 'total'
    assert provider.name == 'rules(fallback)'


def test_provider_success_and_guard_fallback(monkeypatch, remote_settings, contracting_ctx):
    outputs = iter([
        json.dumps({'template': 'contracting', 'intent': 'query', 'query_kpi': 'total', 'confidence': .9}),
        'إجمالي التكلفة بلغ ١٬٧٠٦٫٣ ألف ريال.',
        'إجمالي التكلفة 1,706.3 ألف ريال، والغرامة 99,999.',
    ])

    def mock_completion(url, **kwargs):
        payload = kwargs['json']
        assert payload['model'] == remote_settings.llm_model
        if 'response_format' in payload:
            assert 'مجمع النخيل' in payload['messages'][0]['content']
            assert 'discriminator' in payload['messages'][0]['content']
        assert kwargs['headers']['Authorization'] == 'Bearer mock-key'
        return httpx.Response(200, request=httpx.Request('POST', url),
                              json={'choices': [{'message': {'content': next(outputs)}}]})

    monkeypatch.setattr(httpx, 'post', mock_completion)
    provider = OpenAICompatProvider(remote_settings)
    assert provider.parse('كام إجمالي التكلفة؟', contracting_ctx).query_kpi == 'total'
    answer = 'الكلفة 1,706.3 ألف ريال.'
    facts = {'answer_ar': answer}
    assert '١٬٧٠٦٫٣' in provider.answer('كام؟', contracting_ctx, facts)
    assert provider.explain(facts, answer) == answer
    assert provider.name == 'rules(fallback)'


def test_provider_requires_opt_in_and_key():
    assert get_provider(Settings(llm_provider='openai_compat', openrouter_api_key=None)).name == 'rules'
    assert get_provider(Settings(llm_provider='rules', openrouter_api_key='unused')).name == 'rules'
