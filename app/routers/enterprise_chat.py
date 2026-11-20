from fastapi import APIRouter, Depends, Request
from pydantic import Field, model_validator

from app.access import require_company, require_role
from app.db import request_conn
from app.enterprise import store
from app.enterprise.result_context import result_context
from app.enterprise.impact_graph import WhatIfRequest
from app.llm import assistant_language as language
from app.llm.assistant_answers import citation, period_and_source, read_metrics, requested_keys, sector_answer
from app.llm.enterprise import HELP, EnterpriseScenario, _display, grounded_explanation, parse_rules, rule_draft
from app.llm.provider import get_enterprise_provider
from app.llm.rules_ar import normalize
from app.routers.chat import ChatRequest
from app.routers.companies import COMPANY_SELECT
from app.routers.enterprise import impact_errors, impact_model, whatif_snapshot
from app.schemas import api_error

router = APIRouter(prefix='/enterprise', tags=['enterprise'])


class EnterpriseChatRequest(ChatRequest):
    company_id: int | None = Field(default=None, ge=1, lt=2**63)
    model_id: int | None = Field(default=None, ge=1, lt=2**63)
    version: int | None = Field(default=None, ge=1, lt=2**63)
    weeks: int | None = Field(default=None, ge=1, le=104)
    parse_only: bool = False
    remote: bool | None = None
    focus_node: str | None = Field(default=None, max_length=80)

    @model_validator(mode='after')
    def valid_selection(self):
        if self.version is not None and self.model_id is None:
            raise ValueError('Version requires a saved model ID')
        return self


def select_snapshot(connection, body, principal):
    if body.company_id is None:
        require_role(principal, 'planner')
        return whatif_snapshot(connection, body.model_id, body.version), None

    require_company(principal, body.company_id)
    company = connection.execute(COMPANY_SELECT + ' WHERE c.id = ?', (body.company_id,)).fetchone()
    if company is None:
        raise api_error(404, 'company_not_found', 'الشركة مش موجودة.')
    model_id = body.model_id
    if model_id is None:
        models = [item['id'] for item in store.list_models(connection) if item['company_id'] == body.company_id]
        onboarding = connection.execute('SELECT model_id FROM onboarding_companies WHERE company_id = ?',
                                        (body.company_id,)).fetchone()
        if models:
            model_id = onboarding['model_id'] if onboarding and onboarding['model_id'] in models else models[-1]
    if model_id is None:
        return None, company
    snapshot = whatif_snapshot(connection, model_id, body.version)
    if snapshot['company_id'] != body.company_id:
        raise api_error(404, 'enterprise_model_not_found', 'النموذج المؤسسي مش موجود للشركة دي.')
    return snapshot, company


def scenario_reply(snapshot, result, weeks, metrics):
    chart = result['charts']['cash'] or {}
    period, source = period_and_source(snapshot, cash_dates=chart.get('dates'))
    if not snapshot['graph'].onboarding and not chart.get('dates'):
        period += f' · أفق المحاكاة {weeks} أسبوع'
    labels = {row['id']: row for row in result['nodes']}
    drivers = '، '.join(
        f"{labels[row['node_id']]['name_ar']} من {_display(row['before'], labels[row['node_id']]['unit'])}"
        f" إلى {_display(row['after'], labels[row['node_id']]['unit'])}"
        for row in result['assumptions']
    )
    return f'{grounded_explanation(result, metrics)} الفرضيات: {drivers}. {citation(period, source)}'


def draft_reply(snapshot, model, assumptions):
    period, source = period_and_source(snapshot)
    drivers = {row['id']: row for row in model.drivers()}
    centers, _, _, _ = model.assumption_values(assumptions)
    changes = '، '.join(
        f"{drivers[key]['name_ar']} من {_display(drivers[key]['value'], drivers[key]['unit'])}"
        f" إلى {_display(value, drivers[key]['unit'])}" for key, value in centers.items()
    )
    return f'راجع الفرضيات وأكّدها عشان تحسب الأثر. {changes}. {citation(period, source)}'


def clarify(message, model, snapshot):
    partial, unresolved = rule_draft(message, model)
    prices = [row['name_ar'] for row in model.drivers() if row['id'].startswith('price.')]
    if len(prices) > 1 and any(language.alias(clause, ('السعر', 'سعر البيع', 'سعر')) for clause in unresolved):
        reply = 'تقصد سعر أنهي منتج؟ اختار من ' + '، '.join(prices) + '.'
    else:
        reply = HELP
    if partial and unresolved:
        try:
            return reply + ' الجزء الواضح لسه للمراجعة. ' + draft_reply(snapshot, model, partial)
        except (ValueError, ArithmeticError):
            pass
    return reply


@router.post('/chat')
def enterprise_chat(body: EnterpriseChatRequest, request: Request, connection=Depends(request_conn)):
    principal = request.state.principal
    snapshot, company = select_snapshot(connection, body, principal)
    text = normalize(body.message)
    intent = language.intent(text)
    if intent == 'what_if':
        require_role(principal, 'planner')
    scenario = EnterpriseScenario(intent=intent if intent != 'what_if' else 'help')
    result, provider_name, focus = None, 'rules', None

    if snapshot is None:
        reply = sector_answer(connection, company, language.metrics(text), intent)
        if reply is None:
            reply = 'الشركة دي محتاجة بيانات للمؤشر ده. اربط نموذج من تجهيز الشركة أو حمّل بيانات التخطيط المتاحة.'
    else:
        requested_weeks = body.weeks or WhatIfRequest.model_fields['weeks'].default
        with impact_errors():
            model = impact_model(snapshot, request, requested_weeks)
            weeks = model.weeks
            if intent == 'what_if':
                remote = body.remote if body.remote is not None else body.parse_only or company is None
                provider = get_enterprise_provider(request.app.state.settings, remote=remote)
                scenario = provider.parse_enterprise(body.message, model)
                if scenario.intent != 'what_if':
                    scenario = parse_rules(body.message, model)
                    if provider.name == 'codex':
                        provider.name = 'rules(fallback)'
                provider_name = provider.name
                if scenario.intent != 'what_if':
                    reply = clarify(body.message, model, snapshot)
                elif body.parse_only:
                    reply = draft_reply(snapshot, model, scenario.assumptions)
                else:
                    result = model.scenario(WhatIfRequest(assumptions=scenario.assumptions, weeks=weeks))
                    reply = scenario_reply(snapshot, result, weeks, requested_keys(text, model, body.focus_node))
                    if company is None and body.remote is None:
                        reply = provider.explain({'grounded_ar': reply}, reply)
                        provider_name = provider.name
            else:
                keys = requested_keys(text, model, body.focus_node)
                reply = read_metrics(connection, snapshot, model, text, keys, intent == 'explain')
                if len(keys) == 1:
                    focus = keys[0]
                if reply is None:
                    scenario = EnterpriseScenario(intent='help')
                    reply = 'اسأل عن السيولة أو الربح أو المبيعات أو التكاليف. ' + HELP

    context = result_context(connection, snapshot, weeks) if snapshot else None
    if result is not None:
        result['context'] = context
    name = company['name'] if company is not None else snapshot['name']
    if snapshot and snapshot['version'] is not None and company is not None:
        if snapshot['name'] != name:
            name += f" · {snapshot['name']}"
        name += f" · إصدار {snapshot['version']}"
    if snapshot and 'المصدر' not in reply:
        reply += ' ' + citation(*period_and_source(snapshot))
    if company is not None or snapshot['id'] is None:
        reply = f'{name}. {reply}'
    return {'reply_ar': reply, 'scenario': scenario, 'result': result, 'intent': intent,
            'model_id': snapshot['id'] if snapshot else None, 'version': snapshot['version'] if snapshot else None,
            'name': snapshot['name'] if snapshot else company['name'],
            'company_id': company['id'] if company is not None else snapshot.get('company_id'),
            'provider': provider_name, 'follow_up_metric': focus, 'context': context}
