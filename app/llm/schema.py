from dataclasses import dataclass
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas import TemplateId, api_error
from app.templates_registry import load_templates

Quantity = Annotated[int, Field(ge=0, le=10**12)]
Period = Annotated[int, Field(ge=0, le=10000)]
Duration = Annotated[int, Field(gt=0, le=10000)]
Shift = Annotated[int, Field(ge=-10000, le=10000)]
Percent = Annotated[float, Field(ge=-100, le=100000)]
Entity = Annotated[str, Field(min_length=1, max_length=200)]
Intent = Literal['what_if', 'explain', 'query', 'compare', 'optimize', 'help', 'unknown']
Kpi = Literal['total', 'late', 'peak_od', 'ld', 'changeovers', 'storage']


class StrictBody(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, allow_inf_nan=False)


class CreditPercent(StrictBody):
    type: Literal['credit_limit_pct']
    value: Percent


class CreditAbsolute(StrictBody):
    type: Literal['credit_limit_abs']
    value: Quantity


class CrewUnavailable(StrictBody):
    type: Literal['crew_unavailable']
    resource: Entity
    from_week: Period = Field(alias='from')
    to_week: Period = Field(alias='to')
    units: Annotated[int, Field(gt=0, le=10**12)] = 1


class DeadlineShift(StrictBody):
    type: Literal['deadline_shift']
    project: Entity
    weeks: Shift


class PenaltyChange(StrictBody):
    type: Literal['ld_change']
    project: Entity
    value: Quantity


class ProjectStartDelay(StrictBody):
    type: Literal['project_start_delay']
    project: Entity
    weeks: Duration


class PaymentLag(StrictBody):
    type: Literal['payment_lag']
    value: Shift
    relative: bool = False

    @model_validator(mode='after')
    def validate_absolute(self):
        if not self.relative and self.value < 0:
            raise ValueError('Absolute payment lag must be nonnegative')
        return self


class Margin(StrictBody):
    type: Literal['margin_pct']
    value: Annotated[float, Field(ge=0, le=10000)]


class Demand(StrictBody):
    type: Literal['demand_pct']
    value: Percent
    sku: Entity | None = None


class LineDown(StrictBody):
    type: Literal['line_down']
    line: Entity
    from_day: Period
    days: Duration


class Storage(StrictBody):
    type: Literal['storage_pct']
    value: Percent


class OrderDueShift(StrictBody):
    type: Literal['order_due_shift']
    order: Entity
    days: Shift


class ChangeoverCost(StrictBody):
    type: Literal['changeover_cost_pct']
    value: Percent


class WeightChange(StrictBody):
    type: Literal['weight_change']
    name: Entity
    value: Annotated[float, Field(ge=0, le=5)]
    relative: bool = False


Change = Annotated[
    CreditPercent | CreditAbsolute | CrewUnavailable | DeadlineShift | PenaltyChange |
    ProjectStartDelay | PaymentLag | Margin | Demand | LineDown | Storage |
    OrderDueShift | ChangeoverCost | WeightChange,
    Field(discriminator='type'),
]

ENTITY_FIELDS = {
    'project': ('projects', 'المشروع'),
    'resource': ('resources', 'الفريق'),
    'line': ('lines', 'خط الإنتاج'),
    'order': ('orders', 'الطلب'),
    'sku': ('skus', 'المنتج'),
}


@dataclass(frozen=True)
class ChatContext:
    template: TemplateId
    dataset: dict

    def entities(self):
        return {
            field: {row['id']: row.get('name_ar', row['id']) for row in self.dataset.get(table, [])}
            for field, (table, _) in ENTITY_FIELDS.items()
        }


class Scenario(StrictBody):
    template: TemplateId
    intent: Intent
    changes: list[Change] = Field(default_factory=list, max_length=20)
    query_kpi: Kpi | None = None
    subject: Entity | None = None
    echo_ar: str = Field(default='', max_length=2000)
    confidence: float = Field(default=0, ge=0, le=1)

    @model_validator(mode='after')
    def validate_intent(self):
        if (self.intent == 'what_if') != bool(self.changes):
            raise ValueError('Only what_if may have changes and it requires at least one')
        if self.intent == 'query' and self.query_kpi is None:
            raise ValueError('A query must specify a KPI')
        return self


def validate_changes(ctx: ChatContext, changes: list[Change]):
    template = load_templates()[ctx.template]
    entities = ctx.entities()
    for change in changes:
        if change.type not in template.whatif_changes:
            raise api_error(422, 'unsupported_change', 'التغيير المطلوب غير متاح لقالب القطاع الحالي.')
        for field, (_, label) in ENTITY_FIELDS.items():
            value = getattr(change, field, None)
            if value is not None and value not in entities[field]:
                raise api_error(422, 'unknown_entity', f'{label} «{value}» غير موجود في بيانات التشغيل.')
        if isinstance(change, WeightChange) and change.name not in {w.id for w in template.objective_weights}:
            raise api_error(422, 'invalid_weight', 'وزن الهدف غير موجود في قالب القطاع الحالي.')
