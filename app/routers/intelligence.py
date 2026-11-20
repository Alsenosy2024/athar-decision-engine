"""AI review workspace. All data writes require company scope and CSRF."""
from collections import defaultdict
from dataclasses import replace
from datetime import date, timedelta
import json
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, File, Form, Path, Query, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import Field, ValidationError

from app.access import request_principal
from app.config import STATIC_DIR
from app.connectors.files import MAX_UPLOAD_BYTES, upload_bytes
from app.db import request_conn
from app.enterprise import store
from app.enterprise.impact_graph import Assumption, ImpactError, WhatIfRequest
from app.enterprise.schema import Graph, ModelInput, Node, NodeId, StrictFields
from app.intelligence import analysis, calibration, decision, documents, extraction, forecasting, reviews
from app.llm.enterprise import parse_rules
from app.routers.companies import get_company
from app.routers.enterprise import impact_errors, impact_model
from app.schemas import api_error

router = APIRouter(prefix='/companies/{cid}/ai', tags=['ai'])
page = APIRouter()
Id = Annotated[int, Path(ge=1, lt=2**63)]
ModelId = Annotated[int, Field(ge=1, lt=2**63)]


@page.get('/ai', include_in_schema=False)
def workspace(request: Request):
    headers = {'Cache-Control': 'no-store'}
    principal = request_principal(request)
    if principal is None:
        return RedirectResponse('/login', headers=headers)
    if principal.department_scoped:
        return RedirectResponse('/department-owners', headers=headers)
    return FileResponse(STATIC_DIR / 'ai.html', headers=headers)


class ModelSelection(StrictFields):
    model_id: ModelId


class Refresh(ModelSelection):
    remote: bool = True


class Question(ModelSelection):
    question: str = Field(min_length=1, max_length=1600)
    proposal_id: ModelId | None = None
    rank: int = Field(default=0, ge=0, le=2)
    target_node: NodeId | None = None
    assumptions: list[Assumption] = Field(default_factory=list, max_length=10)
    remote: bool = True


class Forecast(StrictFields):
    model_id: ModelId | None = None
    node_id: NodeId
    horizon: int = Field(default=4, ge=1, le=4)


class GoalText(ModelSelection):
    text: str = Field(min_length=1, max_length=1600)


class GoalRequest(ModelSelection):
    goal: decision.Goal


class Resolution(StrictFields):
    status: Literal['accepted', 'rejected']
    estimates: list[int] = Field(default_factory=list, max_length=1200)
    rank: int = Field(default=0, ge=0, le=2)


def company_snapshot(connection, cid, model_id):
    snapshot = store.read(connection, model_id)
    if snapshot['company_id'] != cid:
        raise api_error(404, 'enterprise_model_not_found', 'النموذج المؤسسي مش موجود للشركة دي.')
    return snapshot


def operational_series(connection, company):
    if company['template'] != 'production' or company['active_dataset_id'] is None:
        return {}
    row = connection.execute('SELECT payload_json FROM datasets WHERE id = ?', (company['active_dataset_id'],)).fetchone()
    dataset = json.loads(row['payload_json'])
    history = defaultdict(dict)
    for point in dataset.get('demand_history', []):
        history[point['sku_id']][point['week']] = point['qty']
    # Production history is indexed by week. Dates are shown as ordinal on
    # the interface so a fabricated calendar start cannot be mistaken for data.
    anchor = date(2000, 1, 3)
    nodes = {}
    for sku in dataset['skus']:
        if series := history[sku['id']]:
            try:
                points = [{'date': (anchor + timedelta(weeks=week)).isoformat(), 'value': value}
                          for week, value in sorted(series.items())]
            except (OverflowError, ValueError):
                continue
            node = Node.model_validate({
                'id': 'demand.' + sku['id'], 'type': 'demand', 'name_ar': sku['name_ar'], 'unit': 'units',
                'baseline': {'kind': 'series', 'frequency': 'weekly', 'points': points},
                'definition': {'kind': 'formula', 'expression': 'active production demand history'},
            })
            nodes[node.id] = node
    return nodes


@router.get('/catalog')
def catalog(cid: Id, request: Request, model_id: Annotated[int | None, Query(ge=1, lt=2**63)] = None,
            connection=Depends(request_conn)):
    company = get_company(connection, cid)
    models = [item for item in store.list_models(connection) if item['company_id'] == cid]
    series = []
    drivers = []
    nodes = []
    version = None
    max_controls = 5
    signals = []
    if model_id:
        snapshot = company_snapshot(connection, cid, model_id)
        version = snapshot['version']
        graph = snapshot['graph']
        max_controls = 2 if graph.arms else 5
        nodes = [{'id': node.id, 'name_ar': node.name_ar, 'unit': node.unit} for node in graph.nodes]
        series = [node.id for node in analysis.history(graph).values()]
        observed, _ = reviews.observed_history(connection, snapshot['id'], graph)
        signals = analysis.insight_facts(analysis.anomalies(observed))[:3]
        with impact_errors():
            drivers = decision.controllable(impact_model(snapshot, request, 52))
    operations = [{'id': node.id, 'name_ar': node.name_ar, 'unit': node.unit}
                  for node in operational_series(connection, company).values()]
    return {'company': company['name'], 'models': models, 'nodes': nodes, 'series': series,
            'drivers': drivers, 'operations': operations, 'version': version, 'max_controls': max_controls,
            'signals': signals}


@router.post('/entry', status_code=201)
def entry(cid: Id, request: Request, files: Annotated[list[UploadFile] | None, File()] = None,
          description: Annotated[str, Form(max_length=8000)] = '',
          name: Annotated[str, Form(max_length=160)] = '',
          model_id: Annotated[int | None, Form(ge=1, lt=2**63)] = None,
          remote: Annotated[bool, Form()] = True,
          fx_rates: Annotated[str, Form(max_length=1000)] = '{}',
          connection=Depends(request_conn)):
    company = get_company(connection, cid)
    snapshot = company_snapshot(connection, cid, model_id) if model_id else None
    if len(files or ()) > 8:
        raise api_error(422, 'ai_too_many_documents', 'الحد الأقصى 8 ملفات للمعاينة.')
    contents = []
    total = 0
    for file in files or ():
        raw = upload_bytes(file)
        total += len(raw)
        if total > MAX_UPLOAD_BYTES:
            raise api_error(413, 'import_file_too_large', 'الحد الأقصى لمجموع الملفات 10 ميجابايت.')
        contents.append((file.filename or '', raw))
    try:
        rates = json.loads(fx_rates)
        if not isinstance(rates, dict) or any(
            key not in extraction.UNIT_MARKER or key == 'SAR' or type(value) not in (int, float)
            or not 0 < value <= 100_000 for key, value in rates.items()
        ):
            raise ValueError
    except ValueError:
        raise api_error(422, 'ai_invalid_fx', 'راجع أسعار التحويل للريال السعودي.') from None
    rows, schedules = documents.read_documents(contents, description.strip())
    settings = request.app.state.settings
    if not remote:
        settings = replace(settings, llm_provider='rules')
    graph = snapshot['graph'] if snapshot else None
    data = extraction.preview(rows, graph, (snapshot['name'] if snapshot else name.strip() or company['name']),
                              settings, rates)
    xml_files = [schedule.pop('xml') for schedule in schedules]
    if company['template'] != 'contracting':
        xml_files = []
    data.update(schedules=schedules, fx_rates=rates)
    if data['model']:
        proposed = ModelInput.model_validate(data['model'])
        observed, excluded = reviews.observed_history(connection, model_id, proposed.graph, data['diff'])
        data['analysis'] = analysis.refresh(settings, observed) | {'estimated_excluded': excluded}
    return reviews.propose(connection, cid, request.state.principal, 'entry', snapshot, data, xml_files=xml_files)


@router.post('/refresh', status_code=201)
def refresh(cid: Id, body: Refresh, request: Request, connection=Depends(request_conn)):
    snapshot = company_snapshot(connection, cid, body.model_id)
    graph, excluded = reviews.observed_history(connection, snapshot['id'], snapshot['graph'])
    settings = request.app.state.settings
    if not body.remote:
        settings = replace(settings, llm_provider='rules')
    data = analysis.refresh(settings, graph) | {'estimated_excluded': excluded}
    return reviews.propose(connection, cid, request.state.principal, 'refresh', snapshot, data)


@router.post('/forecast', status_code=201)
def forecast(cid: Id, body: Forecast, request: Request, connection=Depends(request_conn)):
    company = get_company(connection, cid)
    snapshot = company_snapshot(connection, cid, body.model_id) if body.model_id else None
    if snapshot:
        graph, excluded = reviews.observed_history(connection, snapshot['id'], snapshot['graph'])
        nodes = analysis.history(graph)
    else:
        nodes, excluded = operational_series(connection, company), {}
    node = nodes.get(body.node_id)
    if node is None:
        raise api_error(422, 'ai_forecast_requires_history', 'اختار مؤشر فيه سجل زمني.')
    state = request.app.state
    with impact_errors():
        result = forecasting.ensemble_forecast(node, body.horizon, state.forecast_worker, state.chronos_worker)
    result['estimated_excluded'] = excluded.get(body.node_id, 0)
    if snapshot is None:
        anchor = date(2000, 1, 3)
        for point in result['points']:
            point['week'] = (date.fromisoformat(point.pop('date')) - anchor).days // 7
        result['dataset_id'] = company['active_dataset_id']
    return reviews.propose(connection, cid, request.state.principal, 'forecast', snapshot, result)


@router.post('/calibrate', status_code=201)
def calibrate(cid: Id, body: ModelSelection, request: Request, connection=Depends(request_conn)):
    snapshot = company_snapshot(connection, cid, body.model_id)
    graph, excluded = reviews.observed_history(connection, snapshot['id'], snapshot['graph'])
    with impact_errors():
        proposals = calibration.proposals(impact_model(snapshot | {'graph': graph}, request, 52))
    return reviews.propose(connection, cid, request.state.principal, 'calibration', snapshot,
                           {'edges': proposals, 'estimated_excluded': excluded})


@router.post('/goal/interpret')
def goal_text(cid: Id, body: GoalText, connection=Depends(request_conn)):
    snapshot = company_snapshot(connection, cid, body.model_id)
    try:
        return decision.interpret_goal(body.text, snapshot['graph'])
    except ValueError:
        raise api_error(422, 'ai_invalid_goal_text', 'راجع صياغة الهدف والأرقام.') from None


@router.post('/recommend', status_code=201)
def recommend(cid: Id, body: GoalRequest, request: Request, connection=Depends(request_conn)):
    snapshot = company_snapshot(connection, cid, body.model_id)
    with impact_errors():
        model = impact_model(snapshot, request, body.goal.weeks)
        result = decision.recommend(model, body.goal)
    return reviews.propose(connection, cid, request.state.principal, 'decision', snapshot, result)


@router.post('/question', status_code=201)
def question(cid: Id, body: Question, request: Request, connection=Depends(request_conn)):
    snapshot = company_snapshot(connection, cid, body.model_id)
    graph = snapshot['graph']
    if body.target_node and body.target_node not in {node.id for node in graph.nodes}:
        raise api_error(422, 'ai_unknown_indicator', 'المؤشر مش موجود في النموذج.')
    settings = request.app.state.settings
    if not body.remote:
        settings = replace(settings, llm_provider='rules')
    result = None
    computed = None
    if body.proposal_id:
        previous = reviews.read(connection, cid, body.proposal_id)
        options = previous['data'].get('recommendations', [])
        if (previous['kind'] != 'decision' or previous['model_id'] != snapshot['id']
                or previous['model_version'] != snapshot['version'] or body.rank >= len(options)):
            raise api_error(422, 'ai_invalid_decision_review', 'اختار فرضية محفوظة من نفس إصدار النموذج.')
        result = options[body.rank]['impact']
    else:
        with impact_errors():
            model = impact_model(snapshot, request, 52)
            assumptions = body.assumptions or parse_rules(body.question, model).assumptions
            if assumptions:
                try:
                    scenario = WhatIfRequest(assumptions=assumptions, n=64)
                except ValidationError:
                    raise ImpactError('راجع الفرضيات وتأكد إن كل مدخل ظاهر مرة واحدة.') from None
                result = model.scenario(scenario)
            elif graph.arms:
                targets = analysis.target_ids(graph, body.question, body.target_node)
                if any(key in model.causal.bound for key in targets) or (not targets and settings.llm_provider == 'codex'):
                    baseline, _ = model.baseline()
                    computed = {key: value for key, value in baseline.metrics().items() if key in model.causal.bound}
    response = analysis.question_answer(settings, graph, body.question, result, body.target_node, computed)
    if result:
        response['assumptions'] = result['assumptions']
    return reviews.propose(connection, cid, request.state.principal, 'answer', snapshot, response)


@router.get('/reviews')
def list_reviews(cid: Id, before: Annotated[int | None, Query(ge=1, lt=2**63)] = None,
                 connection=Depends(request_conn)):
    get_company(connection, cid)
    return reviews.recent(connection, cid, before)


@router.get('/reviews/{review_id}')
def get_review(cid: Id, review_id: Id, connection=Depends(request_conn)):
    return reviews.read(connection, cid, review_id)


@router.post('/reviews/{review_id}/decide')
def decide(cid: Id, review_id: Id, body: Resolution, request: Request, connection=Depends(request_conn)):
    def apply(review, current):
        payload = review['data']
        if review['kind'] == 'entry':
            if not payload['model'] and not payload['schedules']:
                raise api_error(422, 'ai_empty_diff', 'مفيش قيم أو جداول قابلة للاعتماد.')
            result = {'accepted_estimates': []}
            if payload['model'] and (payload['diff'] or payload['relations'] or body.estimates):
                model = ModelInput.model_validate(payload['model'])
                chosen = list(dict.fromkeys(body.estimates))
                if any(not 0 <= index < len(payload['estimates']) for index in chosen):
                    raise api_error(422, 'ai_invalid_estimates', 'راجع القيم التقديرية المختارة.')
                try:
                    model = extraction.apply_estimates(model, payload['estimates'], chosen)
                except ValueError as error:
                    raise api_error(422, 'ai_invalid_estimates', str(error)) from None
                result |= reviews.write_model(connection, cid, current, model)
                result['accepted_estimates'] = [payload['estimates'][index] for index in chosen]
                reviews.update_provenance(connection, result['model_id'], review_id,
                                          payload['diff'], result['accepted_estimates'])
            elif body.estimates:
                raise api_error(422, 'ai_invalid_estimates', 'المراجعة مفيهاش قيم تقديرية قابلة للحفظ.')
            elif not payload['schedules']:
                raise api_error(422, 'ai_empty_diff', 'اختار قيمة تقديرية على الأقل عشان تحفظ المعاينة.')
            imports = reviews.save_schedules(connection, cid, review_id, payload['schedules'])
            return result | {'schedules_reviewed': len(payload['schedules']), 'p6_import_ids': imports}
        if review['kind'] == 'calibration':
            if not payload['edges']:
                raise api_error(422, 'ai_empty_calibration', 'مفيش معاملات قابلة للاعتماد.')
            graph = current['graph']
            edges = {(edge.source, edge.target): edge.model_dump(mode='json') for edge in graph.edges}
            for row in payload['edges']:
                edge = row['edge']
                edges[edge['source'], edge['target']] = edge
            updated = Graph.model_validate(graph.model_dump(mode='json') | {'edges': list(edges.values())})
            with impact_errors():
                impact_model(current | {'graph': updated}, request, 52)
            return reviews.write_model(connection, cid, current, ModelInput(name=current['name'], graph=updated))
        if review['kind'] == 'decision':
            options = payload['recommendations']
            if body.rank >= len(options) or options[body.rank]['violations']:
                raise api_error(422, 'ai_recommendation_infeasible', 'اختار توصية بتحترم القيود قبل الحفظ.')
            return reviews.save_recommendation(connection, cid, current, review_id, body.rank, options[body.rank])
        if review['kind'] == 'forecast' and review['model_id'] is None:
            company = get_company(connection, cid)
            if company['active_dataset_id'] != payload['dataset_id']:
                raise api_error(409, 'ai_stale_review', 'بيانات الشركة اتغيّرت. شغّل التوقع من جديد.')
        return {}

    return reviews.decide(connection, cid, request.state.principal, review_id, body.status, apply)
