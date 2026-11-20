import json
import re

from pydantic import ValidationError

from app.decision_loop.answers import Answer, read_request
from app.decision_loop.approval_gate import effective_inputs, read_record
from app.decision_loop.conflict import diagnose
from app.decision_loop.eligibility import approved_inputs
from app.decision_loop.solution_record import stored_record
from app.db import audit, encode, utc_now
from app.delivery.approvals import latest_approved_plan
from app.llm.provider import RulesProvider, get_provider
from app.llm.rules_ar import normalize
from app.llm.schema import ChatContext, validate_changes
from app.routers.optimize import run_optimization
from app.routers.whatif import WhatIfRequest, run_whatif
from app.schemas import OptimizeRequest, api_error
from app.simulation.scenario import apply_changes

RUN_VIEW = '''
    SELECT runs.*, datasets.payload_json, datasets.validation_json, company_models.model_json, base.result_json AS base_result_json
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


def chat(connection, company, message, settings, *, selected_run_id=None, remote=True):
    cid, template = company['id'], company['template']
    pinned = read_record(connection, cid, selected_run_id) if selected_run_id is not None else None
    optimization = latest_plan(connection, cid, kind='optimize')
    base = pinned if pinned is not None else optimization
    active = connection.execute(
        'SELECT payload_json, validation_json FROM datasets WHERE id = ? AND company_id = ?',
        (company['active_dataset_id'], cid),
    ).fetchone()
    inputs = base if base is not None else active
    dataset = json.loads(inputs['payload_json']) if inputs else {}
    ctx = ChatContext(template, dataset)
    provider = get_provider(settings) if remote else RulesProvider()
    try:
        tool, local, use_parser = read_request(message, ctx)
        proposed = provider.parse(message, ctx) if use_parser and provider.name != 'rules' else local
        # A remote parser can suggest an interpretation. Only server-verified
        # intent, scope and arguments can authorize an execution.
        same_intent = (proposed.intent, proposed.changes, proposed.query_kpi, proposed.subject) == (
            local.intent, local.changes, local.query_kpi, local.subject,
        )
        scenario = proposed if same_intent and proposed.confidence >= 0.8 else local
    except ValidationError:
        raise api_error(422, 'invalid_scenario', 'قيم السيناريو خارج النطاق المدعوم أو صيغتها غير صالحة.') from None

    run_id, kpis, diagnosis, proposal = None, None, None, None
    evidence = []
    action = 'none'
    base_record = json.loads(base['result_json']).get('solution_record', {}) if base is not None else {}
    if tool == 'proposal':
        selection = {'الزامي': 'hard', 'مرن': 'soft'}.get(normalize(message).strip('؟? .').split()[-1])
        proposal = {'constraint_id': 'credit_limit', 'proposed_kind': selection,
                    'state': 'review_required', 'unit': 'ألف ريال' if template == 'contracting' else 'ريال',
                    'semantics': 'peak_overdraft <= credit_limit'}
        reply = 'دي مسودة لمراجعة سياسة حد الائتمان. تغيير السياسة بيتراجع في إعدادات نموذج الشركة.'
    elif scenario.intent == 'what_if':
        validate_changes(ctx, scenario.changes)
        if not inputs:
            reply = 'حمّل بيانات للشركة علشان أقدر أشغّل السيناريو.'
        elif not approved_inputs(json.loads(inputs['validation_json'])):
            reply = 'مدخلات اللقطة محتاجة اعتماد قبل إعادة الحل.'
        elif (base_record.get('snapshot', {}).get('time_limit_s') or 0) > settings.solver_time_limit:
            reply = 'مهلة التشغيل المحفوظة أكبر من حد المساعد. اختار تشغيل أساسي بمهلة مناسبة.'
        elif pinned is not None and pinned['kind'] != 'optimize':
            reply = 'اختار تشغيل التحسين الأساسي لإعادة الحل على نفس اللقطة.'
        else:
            base_id = base['id'] if base is not None else run_optimization(
                connection, cid, OptimizeRequest(dataset_id=company['active_dataset_id']), settings, RulesProvider(),
            )['run_id']
            result = run_whatif(
                connection, company, WhatIfRequest(base_run_id=base_id, changes=scenario.changes),
                settings, RulesProvider(),
            )
            action, run_id, kpis = 'whatif', result['run_id'], result['scenario']['kpis']
            answer = Answer(template, run_id, result['solution_record'])
            reply = answer.summary(result, 'scenario', compare=True, show_limit=True)
            evidence = answer.evidence
            scenario = scenario.model_copy(update={'echo_ar': '، '.join(result['changes_ar'])[:2000]})
    elif scenario.intent == 'optimize':
        result = run_optimization(connection, cid, OptimizeRequest(), settings, RulesProvider())
        action, run_id, kpis = 'optimize', result['run_id'], result['optimized']['kpis']
        answer = Answer(template, run_id, result['solution_record'])
        reply = answer.summary(result, 'optimized', compare=True)
        evidence = answer.evidence
    elif scenario.intent in ('explain', 'compare', 'query'):
        explicit_scenario = re.search(r'\b(?:[بلفو])?(?:ال)?سيناريو\b', normalize(message))
        approved = None
        if pinned is not None:
            target = pinned
        elif explicit_scenario:
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
            row = read_record(connection, cid, run_id)
            view_dataset, model = effective_inputs(row)
            result = json.loads(row['result_json'])
            record = stored_record(row, view_dataset, model, result)
            variant = 'scenario' if row['kind'] == 'whatif' else 'optimized'
            kpis = result[variant]['kpis']
            answer = Answer(template, run_id, record)
            if tool == 'diagnose':
                if not record['snapshot']['inputs_approved']:
                    reply = 'المدخلات محتاجة اعتماد قبل تشخيص التعارض.'
                else:
                    limit = min(record['snapshot']['time_limit_s'] or settings.solver_time_limit,
                                settings.solver_time_limit)
                    diagnosis = diagnose(template, view_dataset, model, record['policy'], limit)
                    ids = diagnosis['constraint_ids']
                    if ids:
                        labels = '، '.join(record['policy'][key]['label_ar'] for key in ids)
                        reply = f'السولفر أثبت إن المجموعة دي كافية لحدوث التعارض: {labels}.'
                    elif diagnosis['status'] == 'INFEASIBLE':
                        reply = 'السولفر أثبت تعارض في شروط بناء المسألة قبل القيود القابلة للتشخيص.'
                    elif diagnosis['status'] in ('OPTIMAL', 'FEASIBLE'):
                        reply = 'فيه حل ممكن للقيود الإلزامية في نفس اللقطة.'
                    else:
                        reply = 'التشخيص انتهى من غير حكم على التعارض.'
            elif tool == 'binding':
                reply = answer.constraints(all_checks=True) or 'سجل الحل مفيهوش قيود عند الحد أو فوقه.'
            elif not kpis:
                reply = answer.summary(result, variant)
            elif scenario.intent == 'query':
                reply = ' '.join((answer.reference(variant, approved, cid),
                                  answer.query(scenario.query_kpi, result, variant)))
            elif scenario.subject is not None:
                field = 'project' if template == 'contracting' else 'order'
                if scenario.subject not in ChatContext(template, view_dataset).entities()[field]:
                    raise api_error(422, 'unknown_entity', 'المشروع أو الطلب المطلوب غير موجود في بيانات التشغيل.')
                reply = answer.subject(scenario.subject, view_dataset, result, variant)
            else:
                reply = answer.summary(result, variant, compare=scenario.intent == 'compare')
            evidence = answer.evidence
            action = 'query' if scenario.intent == 'query' else 'explain'
    else:
        reply = ('جرّب تسأل عن شيء محدد\n'
                 '• كام إجمالي التكلفة؟\n• إيه القيود الخانقة؟\n• قارن الخطة بالأساس\n'
                 '• إيه تعارض القيود؟\n• اكتب فرضية واحدة بقيمة واضحة.')

    stored = scenario.model_dump(by_alias=True)
    now = utc_now()
    connection.executemany(
        '''INSERT INTO chat_messages (company_id, role, content, scenario_json, run_id, provider, ts)
           VALUES (?, ?, ?, ?, ?, ?, ?)''',
        [(cid, 'user', message, None, None, None, now),
         (cid, 'assistant', reply, encode(stored | {'evidence': evidence}), run_id, provider.name, now)],
    )
    audit(connection, cid, 'chat.run', 'run', run_id,
          {'provider': provider.name, 'action': action, 'tool': tool or action, 'diagnosis': diagnosis})
    return {'reply_ar': reply, 'intent': scenario.intent, 'scenario': stored, 'evidence': evidence,
            'action': action, 'run_id': run_id, 'kpis': kpis, 'provider': provider.name,
            'diagnosis': diagnosis, 'constraint_proposal': proposal}
