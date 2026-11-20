"""Versioned company dimensions, source mappings, and observations."""
from datetime import date
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.onboarding.records import CashTerms

COMPANY_TEMPLATE = 'enterprise_company'
Dimension = Literal['branch', 'department', 'account', 'cost_center', 'product', 'supplier']
DIMENSIONS = ('branch', 'department', 'account', 'cost_center', 'product', 'supplier')
MeasureUnit = Literal['SAR', 'units', 'days', 'weeks', 'teams', 'storage_units']
Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=160)]
Key = Annotated[str, StringConstraints(pattern=r'^[a-z][a-z0-9_]{0,39}$')]
Value = Annotated[float, Field(strict=True, allow_inf_nan=False)]


class Record(BaseModel):
    model_config = ConfigDict(extra='forbid')


class Entity(Record):
    id: str
    kind: Dimension
    code: Text
    name: Text
    parent_id: str | None = None


class Measure(Record):
    column: Text
    key: Key
    name: Text
    unit: MeasureUnit
    aggregation: Literal['sum', 'closing'] = 'sum'


class Mapping(Record):
    day: Text | None = None
    dimensions: dict[Dimension, Text] = Field(default_factory=dict)
    parents: dict[Dimension, Text] = Field(default_factory=dict)
    names: dict[Dimension, Text] = Field(default_factory=dict)
    measures: list[Measure] = Field(default_factory=list, max_length=20)


class Source(Record):
    file: Text
    sha256: str
    sheet: str
    column: Text
    rows: list[int] = Field(min_length=1)


class Observation(Record):
    day: date
    entities: tuple[str, ...]
    measure: Key
    value: Value
    source: Source


class CompanyData(Record):
    cash_terms: CashTerms | None = Field(default=None, exclude_if=lambda terms: terms is None)
    entities: dict[str, Entity] = Field(default_factory=dict)
    mappings: dict[str, Mapping] = Field(default_factory=dict)
    observations: list[Observation] = Field(default_factory=list)
    node_measures: dict[str, Key] = Field(default_factory=dict)
    node_entities: dict[str, tuple[str, ...]] = Field(default_factory=dict)
