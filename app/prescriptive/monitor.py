"""Evaluate the current enterprise snapshot against configured thresholds."""
from typing import Annotated

from pydantic import Field

from app.enterprise.impact_graph import Assumption, ImpactError, WhatIfRequest
from app.enterprise.scenarios import kpis
from app.enterprise.schema import NodeId, Number, StrictFields
from app.optimization.common import OptimizationInputError
from app.prescriptive.goal import Value


class Thresholds(StrictFields):
    cash_minimum: Value | None = None
    credit_maximum: Number | None = Field(default=None, ge=0, le=100)
    delays_maximum: Value | None = Field(default=None, ge=0)
    driver_uncertainty: dict[NodeId, Annotated[Number, Field(gt=0, le=1e12)]] = Field(default_factory=dict, max_length=10)


METRICS = {
    'cash_low': ('cash_minimum', 'below'),
    'credit_breach': ('credit_maximum', 'above'),
    'delays': ('delays_maximum', 'above'),
}


def monitor(model, settings, *, strict=False):
    base = model.baseline()
    metrics = kpis(base[0].metrics(), base[0].fill_rate)
    warning, samples = None, 1
    if settings.driver_uncertainty:
        drivers = {row['id']: row['value'] for row in model.drivers()}
        try:
            if settings.driver_uncertainty.keys() - drivers.keys():
                raise ImpactError('راجع مدخلات عدم اليقين بعد تحديث النموذج.')
            assumptions = [Assumption(node_id=key, value=drivers[key], uncertainty=spread)
                           for key, spread in settings.driver_uncertainty.items()]
            result = model.scenario(WhatIfRequest(assumptions=assumptions), base=base)
            metrics['credit_breach'] = kpis(
                {row['id']: row['after'] for row in result['nodes']}, result['fill_rate'],
            )['credit_breach']
            samples = result['simulation']['n']
        except (ImpactError, OptimizationInputError, FloatingPointError, OverflowError):
            if strict:
                raise
            metrics['credit_breach'] = samples = None
            warning = 'تعذّر حساب احتمال تجاوز الائتمان. راجع مدخلات عدم اليقين في الإصدار الحالي.'
    alerts, unavailable = [], []
    for key, (field, direction) in METRICS.items():
        threshold, value = getattr(settings, field), metrics[key]
        if threshold is None:
            continue
        if value is None:
            unavailable.append(key)
        elif (value < threshold if direction == 'below' else value > threshold):
            alerts.append({'kpi': key, 'value': value, 'threshold': threshold, 'direction': direction})
    return {'kpis': metrics, 'alerts': alerts, 'unavailable': unavailable,
            'samples': samples, 'warning': warning}
