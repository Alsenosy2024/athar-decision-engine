"""Edge units, conversion and audit metadata shared by writes and execution."""
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, StringConstraints

EdgeUnit = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=48)]
EdgeStatus = Literal['draft', 'approved']
TIME_IN_DAYS = {'days': 1, 'weeks': 7}


class EdgeContractError(ValueError):
    pass


class EdgeProvenance(BaseModel):
    model_config = ConfigDict(extra='forbid')

    source: str
    actor: str
    recorded_at: datetime
    review_id: int | None = None
    approved_by: str | None = None
    approved_at: datetime | None = None
    evidence: str | None = None


def contract_unit(source, target, rule, method=None):
    if method:
        return target
    if rule and rule.mode == 'elasticity':
        return '1'
    if source == target or (source in TIME_IN_DAYS and target in TIME_IN_DAYS):
        return '1'
    return f'{target}/{source}'


def conversion(edge, nodes, method=None):
    source, target = nodes[edge.source].unit, nodes[edge.target].unit
    expected = contract_unit(source, target, edge.rule, method)
    if edge.unit is not None and edge.unit != expected:
        raise EdgeContractError(f'وحدة العلاقة {edge.source} → {edge.target} لازم تكون {expected}.')
    if method or edge.rule and edge.rule.mode == 'elasticity':
        return 1
    if source in TIME_IN_DAYS and target in TIME_IN_DAYS:
        return TIME_IN_DAYS[source] / TIME_IN_DAYS[target]
    if source != target and edge.rule is None:
        raise EdgeContractError(f'العلاقة {edge.source} → {edge.target} محتاجة معامل تحويل بوحدة {expected}.')
    return 1


def needs_coefficient(edge, nodes, method):
    return (method is None and edge.rule is None
            and contract_unit(nodes[edge.source].unit, nodes[edge.target].unit, None) != '1')
