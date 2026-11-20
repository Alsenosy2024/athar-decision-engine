"""Versioned inputs for a company under progressive onboarding."""
from datetime import date as Date
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

Sector = Literal['trading', 'retail', 'services', 'custom']
Source = Literal['actual', 'estimated']
Key = Annotated[str, StringConstraints(pattern=r'^[A-Za-z][A-Za-z0-9_.-]{0,79}$')]
Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=160)]
Value = Annotated[float, Field(strict=True, ge=-1e12, le=1e12, allow_inf_nan=False)]


class Record(BaseModel):
    model_config = ConfigDict(extra='forbid')
    key: Key
    value: Value | Text
    date: Date | None = None
    source: Source = 'actual'


class CashTerms(BaseModel):
    model_config = ConfigDict(extra='forbid')
    collection_lag_weeks: int = Field(default=0, strict=True, ge=0, le=104)
    payment_lag_weeks: int = Field(default=0, strict=True, ge=0, le=104)
    inventory_target: Annotated[Value, Field(ge=0)] | None = None


class OnboardingData(BaseModel):
    model_config = ConfigDict(extra='forbid')
    sector: Sector
    records: list[Record] = Field(default_factory=list, max_length=20000)
    cash_terms: CashTerms | None = Field(default=None, exclude_if=lambda terms: terms is None)
