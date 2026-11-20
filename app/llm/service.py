import json
import re

from pydantic import ValidationError

from app.db import audit, encode, utc_now
from app.delivery.approvals import latest_approved_plan
from app.llm.explain import (
    contracting_notes, optimization_explanation, order_answer, query_answer, scenario_explanation,
)
from app.llm.provider import RulesProvider, get_provider
from app.llm.rules_ar import help_text, normalize
from app.llm.schema import ChatContext, validate_changes
from app.routers.model import current_model
from app.routers.optimize import run_optimization
from app.routers.whatif import WhatIfRequest, run_whatif
from app.schemas import OptimizeRequest, api_error
from app.simulation.scenario import apply_changes

RUN_VIEW = '''
    SELECT runs.*, datasets.payload_json, company_models.model_json, base.result_json AS base_result_json
    FROM runs
    JOIN datasets ON datasets.id = runs.dataset_id AND datasets.company_id = runs.company_id
    JOIN company_models ON company_models.id = runs.model_id AND company_models.company_id = runs.company_id
    LEFT JOIN runs AS base ON base.id = runs.base_run_id AND base.company_id = runs.company_id
    WHERE runs.company_id = ? AND {}
    ORDER BY runs.id DESC LIMIT 1
'''


def latest_plan(connection, cid, *, kind=None, run_id=None):
    if run_id is not None:
        return connection.execute(RUN_VIEW.format('runs.id = ?'), (cid, run_id)).fetchone()
    if kind is not None:
        return connection.execute(RUN_VIEW.format('runs.kind = ?'), (cid, kind)).fetchone()
    return connection.execute(RUN_VIEW.format("runs.kind IN ('optimize', 'whatif')"), (cid,)).fetchone()


def view_inputs(template, row):
    dataset = json.loads(row['payload_json'])
    model = json.loads(row['model_json'])
    if row['kind'] == 'whatif':
        original = WhatIfRequest(base_run_id=row['base_run_id'], **json.loads(row['scenario_json']))
        base_result = json.loads(row['base_result_json'])
        model['weights'] = base_result.get('effective_weights', model['weights'])
        dataset, model, _ = apply_changes(dataset, model, original.changes, template_id=template)
    return dataset, model['parameters']


def respond_to_plan(scenario, run, template, dataset, params, source):
    result = json.loads(run['result_json'])
    is_whatif = run['kind'] == 'whatif'
    kpis = (result['scenario'] if is_whatif else result['optimized'])['kpis']
    if not kpis:
        return 'أحدث تشغيل ما رجّعش خطة قابلة للعرض.', kpis
    if scenario.intent == 'query':
        return query_answer(template, scenario.query_kpi, kpis, source), kpis
    if scenario.subject is not None:
        if template == 'contracting':
            return contracting_notes({}, kpis, dataset, project=scenario.subject), kpis
        return order_answer(scenario.subject, kpis, dataset), kpis
    if is_whatif:
        # Reconstruct the grounded explanation for previously stored remote replies.
        return scenario_explanation(template, result['base']['kpis'], kpis,
                                    result['changes_ar'], dataset, params), kpis
    return optimization_explanation(template, result, dataset, params), kpis


def chat(connection, company, message, settings):
    cid, template = company['id'], company['template']
    optimization = latest_plan(connection, cid, kind='optimize')
    active = connection.execute('SELECT payload_json FROM datasets WHERE id = ? AND company_id = ?',
                                (company['active_dataset_id'], cid)).fetchone()
    active_dataset = json.loads(active['payload_json']) if active else {}
    # Changes are grounded in the dataset of the last optimization.
    dataset = json.loads(optimization['payload_json']) if optimization else active_dataset
    ctx = ChatContext(template, dataset)
    provider = get_provider(settings)
    try:
        scenario = provider.parse(message, ctx)
    except ValidationError:
        raise api_error(422, 'invalid_scenario', 'قيم السيناريو خارج النطاق المدعوم أو صيغتها غير صالحة.') from None

    run_id, kpis = None, None
    action = 'none'
    if scenario.intent == 'what_if':
        validate_changes(ctx, scenario.changes)
        if scenario.confidence < 0.8:
            reply = 'حدّد التغيير والقيمة المطلوبة بوضوح علشان أقدر أشغّل السيناريو.'
        elif not optimization and not active:
            reply = 'حمّل بيانات للشركة علشان أقدر أشغّل السيناريو.'
        else:
            execution_provider = provider
            if optimization is None and provider.name == 'codex':
                # The two runs share a SQLite write transaction. Use grounded templates
                # here so that the second run doesn't wait for Codex while holding it.
                execution_provider = RulesProvider()
            base_id = optimization['id'] if optimization else run_optimization(
                connection, cid, OptimizeRequest(dataset_id=company['active_dataset_id']), settings, execution_provider,
            )['run_id']
            result = run_whatif(connection, company, WhatIfRequest(
                base_run_id=base_id, changes=scenario.changes,
            ), settings, execution_provider)
            action, run_id, kpis = 'whatif', result['run_id'], result['scenario']['kpis']
            reply = f"السيناريو رقم {run_id}. {result['explanation_ar']}"
            scenario = scenario.model_copy(update={'echo_ar': '، '.join(result['changes_ar'])[:2000]})
    elif scenario.intent == 'optimize':
        result = run_optimization(connection, cid, OptimizeRequest(), settings, provider)
        action, run_id, kpis = 'optimize', result['run_id'], result['optimized']['kpis']
        reply = result['explanation_ar']
    elif scenario.intent in ('explain', 'compare', 'query'):
        explicit_scenario = re.search(r'\b(?:[بلفو])?(?:ال)?سيناريو\b', normalize(message))
        approved = None
        if explicit_scenario:
            target = latest_plan(connection, cid, kind='whatif')
        elif scenario.intent == 'query' and scenario.query_kpi == 'total':
            approved = latest_approved_plan(connection, cid)
            target = latest_plan(connection, cid, run_id=approved['run_id']) if approved else optimization
        elif scenario.intent == 'compare':
            target = optimization
        else:
            target = latest_plan(connection, cid)
        if target is None:
            reply = ('مفيش سيناريو محفوظ للشركة.' if explicit_scenario
                     else 'شغّل التحسين الأول علشان أقدر أجاوب من نتائج شركتك.')
        else:
            run_id = target['id']
            is_scenario = target['kind'] == 'whatif'
            if approved:
                source = f"الخطة المعتمدة رقم {approved['id']}"
                if is_scenario:
                    source += f' (السيناريو رقم {run_id})'
            elif is_scenario:
                source = f'السيناريو رقم {run_id}'
            else:
                source = f'الخطة المحسّنة من التشغيل رقم {run_id}'
            view_dataset, params = view_inputs(template, target)
            view_ctx = ChatContext(template, view_dataset)
            field = 'project' if template == 'contracting' else 'order'
            if scenario.subject is not None and scenario.subject not in view_ctx.entities()[field]:
                raise api_error(422, 'unknown_entity', 'المشروع أو الطلب المطلوب غير موجود في بيانات التشغيل.')
            answer, kpis = respond_to_plan(scenario, target, template, view_dataset, params, source)
            if scenario.intent == 'query':
                reply = answer
            else:
                reply = provider.answer(message, view_ctx, {'answer_ar': answer})
                if is_scenario:
                    reply = f'{source}. {reply}'
            action = 'query' if scenario.intent == 'query' else 'explain'
    else:
        reply = help_text(template)

    stored = scenario.model_dump(by_alias=True)
    now = utc_now()
    connection.executemany(
        '''INSERT INTO chat_messages (company_id, role, content, scenario_json, run_id, provider, ts)
           VALUES (?, ?, ?, ?, ?, ?, ?)''',
        [(cid, 'user', message, None, None, None, now),
         (cid, 'assistant', reply, encode(stored), run_id, provider.name, now)],
    )
    audit(connection, cid, 'chat.run', 'run', run_id, {'provider': provider.name, 'action': action})
    return {'reply_ar': reply, 'intent': scenario.intent, 'scenario': stored,
            'action': action, 'run_id': run_id, 'kpis': kpis, 'provider': provider.name}
