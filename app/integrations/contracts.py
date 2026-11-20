"""Public response schemas for the v1 integration contract."""
from datetime import date
from typing import Literal

from pydantic import BaseModel, JsonValue

from app.enterprise.schema import Edge, NodeType, Unit

Source = Literal['actual', 'estimated', 'missing']


class ModelRef(BaseModel):
    id: int
    version: int
    name: str


class CompanyRef(BaseModel):
    id: int
    name: str
    template: str


class SelectionResult(BaseModel):
    company_id: int
    model_id: int
    version: int


class Driver(BaseModel):
    id: str
    name_ar: str
    value: float
    unit: Unit


class Department(BaseModel):
    id: str
    name_ar: str
    node_ids: list[str]
    headline_id: str


class MapNode(BaseModel):
    id: str
    name_ar: str
    type: NodeType
    unit: Unit
    department: str
    baseline: float | None
    source: Source
    history: list[float]


class MapEdge(BaseModel):
    source: str
    target: str
    name_ar: str
    from_department: str
    to_department: str
    lag_weeks: int
    baseline_volume: float | None
    volume_unit: Unit | None
    weight: float


class MapKpi(BaseModel):
    id: str
    node_id: str
    label: str
    unit: str
    baseline: float | None
    history: list[float]
    source: Source


class ResultContext(BaseModel):
    company_id: int
    company_name: str
    model_version: int
    source: str
    data_from: date | None
    data_to: date | None
    weekly_snapshot: bool
    calculation_weeks: int | None


class MapResult(BaseModel):
    context: ResultContext
    company: CompanyRef
    model: ModelRef
    departments: list[Department]
    nodes: list[MapNode]
    edges: list[MapEdge]
    kpis: list[MapKpi]
    drivers: list[Driver]
    goal_drivers: list[Driver]
    readiness: int | None
    analysis_error: str | None
    has_engines: bool


class Kpis(BaseModel):
    profit: float | None
    cash_low: float | None
    credit_breach: float | None
    delays: float | None
    fill_rate: float | None


class KpiResult(SelectionResult):
    context: ResultContext
    weeks: int
    kpis: Kpis


class ImpactEdge(Edge):
    method: str


class Quantiles(BaseModel):
    p10: float
    p50: float
    p90: float


class ImpactNode(BaseModel):
    id: str
    name_ar: str
    type: NodeType
    unit: Unit
    before: float | None
    after: float | None
    delta: float | None
    delta_pct: float | None
    quantiles: Quantiles | None
    path: list[ImpactEdge]
    affected: bool
    source: Source | None = None
    scenario_source: Source | None = None


class AppliedAssumption(BaseModel):
    node_id: str
    before: float
    after: float
    uncertainty: float


class Simulation(BaseModel):
    n: int
    seed: int
    max_financing_iterations: int


class PnlPoint(BaseModel):
    id: str
    before: float | None
    after: float | None


class CashCurve(BaseModel):
    dates: list[str] | None = None
    period_ar: str | None = None
    before: list[float]
    after: list[float]


class Charts(BaseModel):
    pnl: list[PnlPoint]
    cash: CashCurve | None


class WhatIfResult(SelectionResult):
    context: ResultContext
    name: str
    nodes: list[ImpactNode]
    assumptions: list[AppliedAssumption]
    paths: list[ImpactEdge]
    charts: Charts
    fill_rate: float | None
    fill_rate_baseline: float | None
    fill_rate_quantiles: Quantiles | None
    simulation: Simulation
    optimizers_run: dict[str, list[str]]
    solver_status: dict[str, str]
    onboarding: dict[str, JsonValue] | None = None


class EmbedLink(BaseModel):
    path: str
    expires_at: int


class ErrorDetail(BaseModel):
    code: str
    msg_ar: str


class ApiError(BaseModel):
    detail: ErrorDetail
