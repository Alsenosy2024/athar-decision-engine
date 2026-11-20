from datetime import date
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, StringConstraints, model_validator

from app.company_data.schema import CompanyData
from app.enterprise.edge_contracts import EdgeProvenance, EdgeStatus, EdgeUnit
from app.onboarding.records import OnboardingData

NodeId = Annotated[str, StringConstraints(pattern=r'^[A-Za-z][A-Za-z0-9_.-]{0,79}$')]
Label = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=160)]
Sector = Literal['contracting', 'production']
Number = Annotated[float, Field(strict=True, allow_inf_nan=False)]


class NodeType(StrEnum):
    SALES = 'sales'
    DEMAND = 'demand'
    PRICING = 'pricing'
    LINE = 'production_line'
    INVENTORY = 'inventory'
    STORAGE = 'storage'
    PROCUREMENT = 'procurement'
    SUPPLIER = 'supplier'
    WORKFORCE = 'workforce'
    PROJECT = 'project'
    CASH = 'cash'
    AR = 'receivables'
    AP = 'payables'
    FINANCING = 'financing'
    CREDIT = 'credit'
    PROFIT_LOSS = 'profit_loss'
    RISK = 'risk_kpi'


class Unit(StrEnum):
    SAR = 'SAR'
    SAR_PER_UNIT = 'SAR/unit'
    UNITS = 'units'
    STOCK_SPACE = 'storage_units'
    TEAMS = 'teams'
    DAYS = 'days'
    WEEKS = 'weeks'
    PERCENT = '%'


class StrictFields(BaseModel):
    model_config = ConfigDict(extra='forbid')


class Scalar(StrictFields):
    kind: Literal['scalar'] = 'scalar'
    value: Number | None  # None marks a value not yet measured or calculated.


class Observation(StrictFields):
    date: date
    value: Number


class Series(StrictFields):
    kind: Literal['series'] = 'series'
    frequency: Literal['daily', 'weekly']
    points: list[Observation] = Field(min_length=1)

    @model_validator(mode='after')
    def ordered_dates(self):
        if any(left.date >= right.date for left, right in zip(self.points, self.points[1:])):
            raise ValueError('Series dates must be strictly increasing')
        return self


class Formula(StrictFields):
    kind: Literal['formula'] = 'formula'
    expression: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=600)]


class Engine(StrEnum):
    FORECAST = 'forecasting'
    PRODUCTION = 'production_optimizer'
    CONTRACTING = 'contracting_optimizer'


# Each entry maps a published output path to its entity selector (if any).
ENGINE_OUTPUTS = {
    Engine.FORECAST: {'series.p10': 'key', 'series.p50': 'key', 'series.p90': 'key'},
    Engine.PRODUCTION: {
        'plan.schedule': 'line', 'plan.inventory': 'sku', 'kpis.peak_storage_pct': None,
        'ledger.revenue': None, 'ledger.direct_cost': None, 'ledger.expenses': None,
        'ledger.profit': None, 'ledger.cash': None,
    },
    Engine.CONTRACTING: {'plan.activities': 'project', 'kpis.late_weeks_total': None},
}


class EngineLink(StrictFields):
    kind: Literal['engine'] = 'engine'
    engine: Engine
    sector: Sector
    output: NodeId
    entity_id: NodeId | None = None

    @model_validator(mode='after')
    def compatible_sector(self):
        if self.engine != Engine.FORECAST and self.engine != f'{self.sector}_optimizer':
            raise ValueError('Optimizer and sector must match')
        outputs = ENGINE_OUTPUTS[self.engine]
        if self.output not in outputs or bool(self.entity_id) != bool(outputs[self.output]):
            raise ValueError('Invalid engine output or entity selector')
        return self


class Node(StrictFields):
    id: NodeId
    type: NodeType
    name_ar: Label
    unit: Unit
    baseline: Annotated[Scalar | Series, Field(discriminator='kind')]
    definition: Annotated[Formula | EngineLink, Field(discriminator='kind')]


class EdgeRule(StrictFields):
    multiplier: Number = 1
    mode: Literal['linear', 'elasticity'] = 'linear'
    lag_weeks: int = Field(default=0, ge=0, le=103, strict=True)
    minimum: Number | None = None
    maximum: Number | None = None

    @model_validator(mode='after')
    def valid_capacity(self):
        if self.minimum is not None and self.maximum is not None and self.minimum > self.maximum:
            raise ValueError('Edge minimum must be below or equal to maximum')
        return self


class Edge(StrictFields):
    source: NodeId
    target: NodeId
    name_ar: Label
    rule: EdgeRule | None = None
    unit: EdgeUnit | None = None
    status: EdgeStatus | None = None
    provenance: EdgeProvenance | None = None
    evidence: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=600)] | None = None


class Arm(StrictFields):
    sector: Sector
    # The demo uses the same packaged inputs and default models as V1's seed.
    input_source: Literal['v1_demo', 'client_plan']
    approved_plan_id: int | None = Field(default=None, strict=True, ge=1, lt=2**63,
                                         exclude_if=lambda plan_id: plan_id is None)

    @model_validator(mode='after')
    def check_source(self):
        if self.input_source == 'client_plan':
            if self.sector != 'production' or self.approved_plan_id is None:
                raise ValueError('Client plans require an approved production plan ID')
        elif self.approved_plan_id is not None:
            raise ValueError('Demo arms cannot specify a client plan')
        return self


class Graph(StrictFields):
    _client_sources: dict = PrivateAttr(default_factory=dict)

    nodes: list[Node] = Field(min_length=1)
    edges: list[Edge] = Field(default_factory=list)
    arms: list[Arm] = Field(default_factory=list, max_length=2)
    onboarding: OnboardingData | None = Field(default=None, exclude_if=lambda data: data is None)

    company_data: CompanyData | None = Field(default=None, exclude_if=lambda data: data is None)

    @model_validator(mode='after')
    def validate_references(self):
        ids = {node.id for node in self.nodes}
        if len(ids) != len(self.nodes):
            raise ValueError('Node IDs must be unique')
        pairs = {(edge.source, edge.target) for edge in self.edges}
        if len(pairs) != len(self.edges):
            raise ValueError('Edges must be unique')
        if any(source not in ids or target not in ids or source == target for source, target in pairs):
            raise ValueError('Edges must connect distinct existing nodes')
        sectors = {arm.sector for arm in self.arms}
        if len(sectors) != len(self.arms):
            raise ValueError('Business arms must be unique')
        client_sectors = {arm.sector for arm in self.arms if arm.input_source == 'client_plan'}
        if client_sectors and (len(self.arms) != 1 or self.onboarding is not None):
            raise ValueError('The client production ledger requires its own operating graph')
        if any(isinstance(node.definition, EngineLink) and node.definition.output.startswith('ledger.')
               and (node.definition.sector not in client_sectors or node.unit != Unit.SAR)
               for node in self.nodes):
            raise ValueError('Ledger outputs require a client production arm and SAR units')
        if any(isinstance(node.definition, EngineLink) and node.definition.sector not in sectors
               for node in self.nodes):
            raise ValueError('Engine links must refer to a declared business arm')
        return self


class ModelInput(StrictFields):
    name: Label
    graph: Graph


class ModelUpdate(ModelInput):
    expected_version: int = Field(ge=1, strict=True)


class ModelSummary(BaseModel):
    id: int
    company_id: int | None = None
    name: Label
    template: Literal['enterprise']
    version: int
    created_at: str


class ModelSnapshot(ModelSummary):
    graph: Graph


class Template(ModelInput):
    template: Literal['enterprise'] = 'enterprise'
