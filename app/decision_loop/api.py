"""Additive authenticated experiments and versioned approved-plan allocations."""
import json
from typing import Annotated, Self

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.db import request_conn
from app.decision_loop.evaluation import measure
from app.decision_loop.frontier import shared_frontier
from app.decision_loop.scenarios import fingerprint
from app.decision_loop.service import PATHS, SHORT_PATHS, experiment
from app.delivery import approvals
from app.enterprise.schema import NodeId, Number
from app.optimization.common import OptimizationInputError
from app.optimization.joint_allocation import Allocation
from app.optimization.production import prepare
from app.routers.companies import get_company
from app.schemas import api_error

router = APIRouter()
Id = Annotated[int, Field(gt=0, le=2**63 - 1)]
Quantity = Annotated[int, Field(strict=True, ge=0, le=10**9)]
Resource = Annotated[str, Field(pattern=r'^(credit|storage)\.(0|[1-9][0-9]?)$')]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)


class Observation(StrictModel):
    day: int = Field(ge=1, le=34)
    quantities: dict[str, Quantity] = Field(min_length=1, max_length=100)


class Commitment(StrictModel):
    through_day: int = Field(ge=0, le=34)
    fingerprint: str = Field(pattern=r'^[a-f0-9]{64}$')


class ProductionExperiment(StrictModel):
    dataset_id: Id | None = None
    model_id: Id | None = None
    alpha: Number = Field(default=0.9, ge=0.5, le=0.95)
    tail_weight: Number = Field(default=0.5, ge=0, le=5)
    min_service_pct: Number = Field(default=0, ge=0, le=100)
    max_service_risk: Number = Field(default=0, ge=0, le=1)
    max_credit_risk: Number = Field(default=1, ge=0, le=1)
    forecast_uplift: Number = Field(default=0.10, ge=0, le=0.5)
    short_paths: int = Field(default=SHORT_PATHS, ge=4, le=PATHS)
    rolling: bool = False
    observations: list[Observation] | None = Field(default=None, max_length=6)
    expected_commitment: Commitment | None = None

    @model_validator(mode='after')
    def require_rolling(self):
        if self.observations is not None and not self.rolling:
            raise ValueError('Observations require rolling mode')
        if self.expected_commitment and (self.observations is None or not self.model_id or not self.dataset_id):
            raise ValueError('Continuing a commitment requires pinned model, dataset and observations')
        return self


@router.post('/companies/{cid}/decision-loop/production')
def production_experiment(cid: Id, body: ProductionExperiment, connection=Depends(request_conn)):
    company = get_company(connection, cid)
    if company['template'] != 'production':
        raise api_error(422, 'production_required', 'التجربة متاحة لقالب الإنتاج.')
    dataset_id = body.dataset_id or company['active_dataset_id']
    row = connection.execute('SELECT payload_json FROM datasets WHERE id=? AND company_id=?',
                             (dataset_id, cid)).fetchone()
    if row is None:
        raise api_error(404, 'dataset_not_found', 'مجموعة البيانات غير موجودة للشركة.')
    model_row = connection.execute(
        'SELECT id, version, model_json FROM company_models WHERE company_id=? AND (? IS NULL OR id=?) '
        'ORDER BY version DESC LIMIT 1', (cid, body.model_id, body.model_id),
    ).fetchone()
    if model_row is None:
        raise api_error(404, 'model_not_found', 'إصدار النموذج غير موجود للشركة.')
    model = json.loads(model_row['model_json'])
    data = json.loads(row['payload_json'])
    hard = model['constraints']['credit_limit'] == 'hard'
    try:
        result = experiment(data, model['parameters'], model['weights'], body, hard_credit=hard)
    except OptimizationInputError as error:
        raise api_error(422, 'invalid_experiment_input', str(error)) from None
    return result | {
        'dataset_id': dataset_id, 'model_id': model_row['id'], 'model_version': model_row['version'],
        'input_fingerprint': fingerprint({'data': data, 'model': model, 'request': body.model_dump()}),
        'mode': 'exploratory',
    }


class UnitPlans(StrictModel):
    id: NodeId
    current: Id
    plans: list[Id] = Field(min_length=1, max_length=12)


class BundleRequest(StrictModel):
    units: list[UnitPlans] = Field(min_length=2, max_length=12)
    limits: dict[Resource, Number] = Field(min_length=1, max_length=12)

    @model_validator(mode='after')
    def validate_references(self) -> Self:
        plans = [pid for unit in self.units for pid in unit.plans]
        if len({unit.id for unit in self.units}) != len(self.units) or len(set(plans)) != len(plans):
            raise ValueError('Department and plan references must be unique')
        if any(unit.current not in unit.plans for unit in self.units):
            raise ValueError('Each department needs a reference plan among its options')
        if not any(key.startswith('credit.') for key in self.limits):
            raise ValueError('A period credit limit is required')
        return self


class PlanMetrics(StrictModel):
    version: str
    cost_sar: Number
    service_loss_pct: Number


@router.post('/companies/{cid}/decision-loop/allocation')
def allocation_frontier(cid: Id, body: BundleRequest, connection=Depends(request_conn)):
    company = get_company(connection, cid)
    if company['template'] != 'production':
        raise api_error(422, 'production_required', 'التجربة متاحة لقالب الإنتاج.')

    units, metrics, origins = [], {}, set()
    owners = {}
    for unit in body.units:
        metrics[unit.id] = {}
        options = []
        for pid in unit.plans:
            plan = approvals.get_plan(connection, pid, company_id=cid)
            approvals.require_approved(plan)
            rows, _, data, params = approvals.plan_snapshot(connection, plan, 'production')
            problem = prepare(data, params)
            for entity, values in (('line', problem.lines), ('order', problem.orders)):
                for key in values:
                    if owners.setdefault((entity, key), unit.id) != unit.id:
                        raise api_error(422, 'overlapping_operations',
                                        'في إدارتين بيستخدموا نفس خط الإنتاج أو نفس الطلب في الحزمة.')
            result = measure(problem, rows)
            if not result['physical_ok'] or result['credit_breach_sar']:
                raise api_error(422, 'ineligible_plan', 'في خطة معتمدة تجاوزت قيد تشغيل أو ائتمان عند إعادة التحقق.')
            origins.add((params['plan_start_date'], problem.horizon))
            usage = {}
            for key in body.limits:
                resource, offset = key.split('.')
                day = int(offset)
                if day >= problem.horizon:
                    raise api_error(422, 'invalid_period', 'في فترة تخصيص خارج أفق إحدى الخطط.')
                series = 'credit_by_day' if resource == 'credit' else 'storage_by_day'
                usage[key] = result[series][day]
            opt = f'plan.{pid}'
            options.append({'id': opt, 'resources': usage, 'value': -result['cost_sar']})
            metrics[unit.id][opt] = PlanMetrics(
                version=fingerprint({'run': plan['run_id'], 'plan': rows, 'parameters': params}),
                cost_sar=result['cost_sar'], service_loss_pct=result['service_loss_pct'],
            )
        units.append({'id': unit.id, 'current': f'plan.{unit.current}', 'options': options})
    if len(origins) != 1:
        raise api_error(422, 'unaligned_periods', 'خطط الحزمة لازم تبدأ في نفس التاريخ ولها نفس الأفق.')
    try:
        problem = Allocation(units=units, limits=body.limits)
        result = shared_frontier(problem, metrics)
    except (ValidationError, ValueError):
        raise api_error(422, 'invalid_allocation', 'راجع حدود الموارد وعدد البدائل في حزمة التخصيص.') from None
    origin, horizon = origins.pop()
    return result | {'period_origin': origin, 'horizon_days': horizon, 'limits': body.limits,
                     'service_aggregation': 'equal_department_weight', 'credit_aggregation': 'gross_draw_sum'}
