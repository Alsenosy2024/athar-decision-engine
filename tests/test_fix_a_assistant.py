from pathlib import Path
from subprocess import TimeoutExpired

from fastapi.testclient import TestClient
import pytest

from app.config import Settings
from app.llm import codex_cli
from app.main import create_app
from conftest import sign_in
from test_p15_access import USER_PASSWORD, new_account
from test_p17_onboarding import commit, create, ok, workbook

CHAT = '/api/enterprise/chat'


def seeded_company(client):
    model, path = create(client)
    review = workbook(client, path, {
        'sales': [('demand', 100, '2026-10-08')],
        'catalog': [('price', 25), ('unit_cost', 10)],
        'operations': [('capacity', 120)],
        'suppliers': [('supplier.delay', 5)],
        'finance': [('cash.opening', 1000), ('ar', 0), ('ap', 800), ('credit', 500),
                    ('financing', 10), ('pnl.expenses', 100)],
    }, 1)
    return commit(client, path, review)


@pytest.fixture
def company(client):
    return seeded_company(client)


@pytest.mark.parametrize(('question', 'intent', 'figures'), [
    ('الكاش كام دلوقتي، وإيه الرقم اللي اتحسب منه؟', 'query', ('1,590', '1,000', '800')),
    ('كم تبلغ السيولة؟', 'query', ('1,590',)),
    ('وش وضع الكاش الحين؟', 'query', ('1,590',)),
    ('شنو المبيعات هالأسبوع؟', 'query', ('2,500',)),
    ('كم عدد الوحدات المباعة؟', 'query', ('100',)),
    ('قد إيه التكاليف؟', 'query', ('1,110',)),
    ('كم تبلغ التكاليف المباشرة؟', 'query', ('1,000',)),
    ('ليه الربح نزل؟', 'explain', ('1,390', 'قراءتين')),
    ('ليش طلعت الإيرادات؟', 'explain', ('2,500', 'قراءتين')),
    ('لو الطلب قل 20% إيه اللي يحصل للربح والكاش؟', 'what_if', ('1,090', '1,290')),
    ('لو قلّلنا الطلب ٢٠ بالمية وش يصير؟', 'what_if', ('1,090', '1,290')),
    ('إذا زدنا الطلب 10%، ماذا يحدث للربح؟', 'what_if', ('1,540', '1,740')),
    ('لو المورد اتأخر أسبوعين إيه اللي يحصل؟', 'what_if', ('5 يوم', '19 يوم')),
    ('لو زودنا السعر 10% وخفضنا حد الائتمان 15%، الأرباح والسيولة هيتغيروا إزاي؟',
     'what_if', ('27.5', '425')),
])
def test_rules_answer_selected_company(client, company, question, intent, figures):
    answer = ok(client.post(CHAT, json={'company_id': company['company_id'], 'message': question}))
    assert answer['provider'] == 'rules'
    assert answer['intent'] == answer['scenario']['intent'] == intent
    assert answer['version'] == 2 and answer['model_id'] == company['model_id']
    reply = answer['reply_ar']
    for part in ('شركة تجربة', 'الفترة', 'أسبوع', '2026-10-08', 'المصدر', 'حساب نموذج الشركة', *figures):
        assert part in reply
    if intent == 'what_if':
        assert answer['result']['assumptions']


def test_explains_observed_profit_with_matched_period_components(client):
    def series(key, name, before, after):
        return {'id': key, 'type': 'profit_loss', 'name_ar': name, 'unit': 'SAR',
                'definition': {'kind': 'formula', 'expression': 'سجل مالي'},
                'baseline': {'kind': 'series', 'frequency': 'weekly', 'points': [
                    {'date': '2026-10-01', 'value': before}, {'date': '2026-10-08', 'value': after},
                ]}}

    nodes = [series('pnl.result', 'الربح', 100, 75), series('pnl.revenue', 'الإيراد', 200, 180),
             series('pnl.direct_cost', 'التكاليف المباشرة', 70, 75),
             series('pnl.expenses', 'مصاريف التشغيل', 20, 20), series('financing', 'التمويل', 10, 10)]
    ok(client.post('/api/enterprise/models?company_id=1', json={
        'name': 'نموذج السجل المالي', 'graph': {'nodes': nodes, 'edges': []},
    }), 201)
    reply = ok(client.post(CHAT, json={'company_id': 1, 'message': 'وش سبب انخفاض الربح؟'}))['reply_ar']
    for part in ('75 ريال', '100 ريال', '2026-10-01', '2026-10-08', 'سجل المؤشر', 'مساهمة بنود قائمة الدخل', '-20', '-5'):
        assert part in reply


def test_lab_catalog_accepts_onboarding_company(client, company):
    cid, mid = company['company_id'], company['model_id']
    data = ok(client.get(f'/api/companies/{cid}/ai/catalog', params={'model_id': mid}))
    assert data['version'] == company['version']
    assert any(node['id'] == 'cash' for node in data['nodes'])
    assert client.get('/api/companies/1/ai/catalog', params={'model_id': mid}).status_code == 404


def test_reader_can_ask_but_cannot_run_impact_or_access_another_company(client, company):
    cid, mid = company['company_id'], company['model_id']
    new_account(client, 'viewer', companies=(cid,))
    with TestClient(client.app) as viewer:
        sign_in(viewer, 'viewer', USER_PASSWORD)
        assert viewer.post(CHAT, json={'company_id': cid, 'message': 'الكاش كام؟'}).status_code == 200
        assert viewer.post(CHAT, json={'company_id': cid, 'message': 'لو الطلب قل 20%؟'}).status_code == 403
        assert viewer.post(CHAT, json={'company_id': 1, 'model_id': mid, 'message': 'الكاش كام؟'}).status_code == 404


def test_codex_opt_in_short_timeout_reuses_the_local_result(db_path, monkeypatch):
    monkeypatch.setattr(codex_cli, 'find_codex', lambda: Path('/fixture/codex'))
    attempts = []

    def timeout(command, *, timeout, **kwargs):
        attempts.append(timeout)
        raise TimeoutExpired(command, timeout)

    monkeypatch.setattr(codex_cli, '_run', timeout)
    settings = Settings(db_path=db_path, forecast_engine='stat', llm_provider='codex')
    with TestClient(create_app(settings)) as client:
        sign_in(client)
        company = seeded_company(client)
        question = {'company_id': company['company_id'], 'message': 'لو الطلب قل 20%؟'}
        local = ok(client.post(CHAT, json=question))
        assert local['provider'] == 'rules' and not attempts
        fallback = ok(client.post(CHAT, json=question | {'remote': True}))
    assert len(attempts) == 1 and 0 < attempts[0] <= 3
    assert fallback['provider'] == 'rules(fallback)'
    assert fallback['reply_ar'] == local['reply_ar']
    assert fallback['result'] == local['result']


def test_no_model_does_not_borrow_demo_values(client):
    result = ok(client.post(CHAT, json={'company_id': 1, 'message': 'الكاش كام؟'}))
    assert result['model_id'] is None and result['result'] is None
    assert 'محتاجة بيانات' in result['reply_ar']
    assert 'روافد' not in result['reply_ar']


def test_follow_up_uses_last_metric_only_when_the_new_question_leaves_it_implicit(client, company):
    scoped = {'company_id': company['company_id']}
    first = ok(client.post(CHAT, json=scoped | {'message': 'كم تبلغ السيولة؟'}))
    assert first['follow_up_metric'] == 'cash'
    context = scoped | {'focus_node': first['follow_up_metric']}
    follow_up = ok(client.post(CHAT, json=context | {'message': 'طيب ليه؟'}))
    assert follow_up['scenario']['intent'] == 'explain'
    assert all(part in follow_up['reply_ar'] for part in ('1,590', 'قراءتين', 'المصدر'))
    another = ok(client.post(CHAT, json=context | {'message': 'ليش نزل الربح؟'}))
    assert another['follow_up_metric'] == 'pnl.result'
    assert '1,390' in another['reply_ar']


def test_scenario_keeps_numbers_in_driver_names_separate_from_the_amount(client):
    ok(client.post('/api/enterprise/models?company_id=1', json={
        'name': 'نموذج السعر', 'graph': {'edges': [], 'nodes': [{
            'id': 'price.line1', 'type': 'pricing', 'name_ar': 'سعر الخط 1', 'unit': 'SAR/unit',
            'definition': {'kind': 'formula', 'expression': 'السعر في الكتالوج'},
            'baseline': {'kind': 'scalar', 'value': 100},
        }]},
    }), 201)
    reply = ok(client.post(CHAT, json={'company_id': 1, 'message': 'لو قلّلنا سعر الخط 1 بنسبة 20%؟'}))
    assert reply['scenario']['intent'] == 'what_if'
    assert reply['result']['assumptions'][0]['after'] == 80
    assert all(part in reply['reply_ar'] for part in ('80 ريال', 'الفترة', 'المصدر'))
