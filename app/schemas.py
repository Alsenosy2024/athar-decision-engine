from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, JsonValue, StringConstraints
from typing_extensions import Annotated

from app.onboarding.records import Sector

TemplateId = Literal['contracting', 'production']
CompanyName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=150)]


class CompanyCreate(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: CompanyName
    template: TemplateId


class Company(BaseModel):
    id: int
    name: CompanyName
    template: TemplateId | Sector | Literal['enterprise_company']
    active_dataset_id: int | None
    model_version: int


class FieldError(BaseModel):
    sheet: str
    row: int
    column: str
    msg_ar: str


class ValidationResult(BaseModel):
    ok: bool
    errors: list[FieldError]
    summary: dict[str, int]


class DatasetResult(BaseModel):
    dataset_id: int | None
    validation: ValidationResult


class DatasetSummary(BaseModel):
    id: int
    name: str
    source: Literal['demo', 'upload']
    is_active: bool
    created_at: str
    validation: ValidationResult


class DatasetDetail(BaseModel):
    id: int
    payload: dict[str, list[dict[str, JsonValue]]]
    validation: ValidationResult


class ModelPatch(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)
    parameters: dict[str, JsonValue] | None = None
    constraints: dict[str, Literal['hard', 'soft']] | None = None
    weights: dict[str, float] | None = None


class CompanyModel(BaseModel):
    id: int
    version: int
    template: TemplateId
    parameters: dict[str, JsonValue]
    constraints: dict[str, Literal['hard', 'soft']]
    weights: dict[str, float]


class Health(BaseModel):
    status: Literal['ok']
    version: str
    solver: str
    provider: str


RunKind = Literal['optimize', 'whatif', 'montecarlo']


class ForecastRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    dataset_id: int | None = Field(default=None, gt=0)


class ForecastBacktest(BaseModel):
    mae: float
    mape: float
    method: str
    n: int
    mape_n: int


class ForecastBand(BaseModel):
    key: str
    name_ar: str
    engine: Literal['stat', 'timesfm'] = 'stat'
    history: list[float]
    p10: list[float]
    p50: list[float]
    p90: list[float]
    backtest: ForecastBacktest


class DurationForecastBand(ForecastBand):
    phase: str


class DemandCandidateBacktests(BaseModel):
    stat: ForecastBacktest
    timesfm: ForecastBacktest


class DemandForecastBand(ForecastBand):
    method: Literal['croston_sba', 'holt_damped', 'timesfm']
    history_weeks: list[int]
    candidate_backtests: DemandCandidateBacktests | None = None


class DurationFit(BaseModel):
    mu: float
    sigma: float
    n: int
    source: Literal['history', 'default', 'calibration']
    source_ar: str


class DurationForecastResult(BaseModel):
    forecast_id: int
    method: Literal['lognormal']
    series: list[DurationForecastBand]
    phases: dict[str, DurationFit]
    summary_ar: str


class DemandForecastResult(BaseModel):
    forecast_id: int
    method: Literal['holt_croston', 'timesfm', 'mixed']
    series: list[DemandForecastBand]
    summary_ar: str


ForecastResult = Annotated[DurationForecastResult | DemandForecastResult, Field(discriminator='method')]


class MonteCarloRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    run_id: int = Field(gt=0)
    n: int = Field(default=200, gt=0)
    seed: int = Field(default=42, ge=0)


class RiskBand(BaseModel):
    p10: float
    p50: float
    p90: float


class HistogramBin(BaseModel):
    bin: int
    count: int


class RiskDistribution(RiskBand):
    hist: list[HistogramBin]


class LateWeeksDistribution(RiskDistribution):
    mean: float


class OrderRisk(BaseModel):
    p_late: float


class ProjectRisk(OrderRisk):
    late_p50: float


class ContractingRisk(BaseModel):
    n: int
    p_any_late: float
    p_credit_breach: float
    late_weeks: LateWeeksDistribution
    total_cost: RiskBand
    peak_od: RiskBand
    per_project: dict[str, ProjectRisk]


class ProductionRisk(BaseModel):
    n: int
    p_any_late: float
    p_credit_breach: float
    p_storage_overflow: float
    fill_rate: RiskBand
    late_orders: RiskDistribution
    per_order: dict[str, OrderRisk]


class OptimizeRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)
    dataset_id: int | None = Field(default=None, gt=0)
    weights: dict[str, float] | None = None
    time_limit: float | None = Field(default=None, gt=0)


class PlanVariant(BaseModel):
    kpis: dict[str, JsonValue]
    plan: dict[str, JsonValue]


class SolverSummary(BaseModel):
    status: str
    objective: float | None
    bound: float | None
    wall_s: float
    time_limit_s: float | None = None
    state: Literal['complete', 'partial', 'no_result'] | None = None
    stop_reason: Literal['search_limit', 'finished'] | None = None
    search_status: str | None = None


class OptimizeResult(BaseModel):
    run_id: int
    baseline: PlanVariant
    optimized: PlanVariant
    solver: SolverSummary
    explanation_ar: str


class RunDetail(BaseModel):
    id: int
    company_id: int
    dataset_id: int
    model_id: int
    kind: RunKind
    base_run_id: int | None
    scenario: JsonValue
    result: dict[str, JsonValue]
    status: str
    wall_s: float
    created_at: str


def api_error(status, code, message):
    return HTTPException(status_code=status, detail={'code': code, 'msg_ar': message})


