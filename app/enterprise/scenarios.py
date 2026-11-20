"""Saved enterprise assumptions, comparable KPIs and one-at-a-time sensitivity."""
import json
from math import isfinite
from typing import Annotated, Literal

from pydantic import Field, StringConstraints, model_validator

from app.access import active_principal, company_visible, require_company
from app.db import audit, encode, utc_now
from app.enterprise.impact_graph import Assumption, ImpactError, ScenarioAssumptions, WhatIfRequest
from app.enterprise.result_context import WEEKLY_CASH_LABEL
from app.enterprise.schema import NodeId, Number, StrictFields
from app.optimization.common import OptimizationInputError
from app.schemas import api_error

Kpi = Literal['profit', 'cash_low', 'credit_breach', 'delays', 'fill_rate']
ModelId = Annotated[int, Field(strict=True, ge=1)]
KPIS = {
    'profit': {'label': 'النتيجة بعد التمويل', 'unit': 'ر.س'},
    'cash_low': {'label': 'أقل رصيد سيولة', 'unit': 'ر.س'},
    'credit_breach': {'label': 'احتمال تجاوز الائتمان', 'unit': '%'},
    'delays': {'label': 'إجمالي تأخير المشروعات', 'unit': 'أسبوع'},
    'fill_rate': {'label': 'نسبة تلبية طلبات الإنتاج', 'unit': '%'},
}
KPI_NODES = {'profit': 'pnl.result', 'cash_low': 'cash', 'credit_breach': 'risk.credit',
             'delays': 'risk.delay', 'fill_rate': 'risk.fill'}


def kpi_specs(graph):
    if not graph.onboarding:
        return KPIS
    return KPIS | {
        'cash_low': {**KPIS['cash_low'], 'label': WEEKLY_CASH_LABEL},
        'profit': {**KPIS['profit'], 'label': 'الربح الأسبوعي بعد التمويل'},
    }


class ScenarioSave(ScenarioAssumptions):
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]


class ComparisonSettings(StrictFields):
    weeks: int = Field(default=52, ge=1, le=104, strict=True)
    n: int = Field(default=200, ge=1, le=1000, strict=True)
    seed: int = Field(default=42, ge=0, le=2**32 - 1, strict=True)


class ComparisonRequest(ComparisonSettings):
    scenario_ids: list[ModelId] = Field(min_length=2, max_length=4)

    @model_validator(mode='after')
    def distinct_scenarios(self):
        if len(set(self.scenario_ids)) != len(self.scenario_ids):
            raise ValueError('Choose distinct scenarios')
        return self


class TornadoRequest(StrictFields):
    model_id: ModelId | None = None
    version: ModelId | None = None
    weeks: int = Field(default=52, ge=1, le=104, strict=True)
    kpi: Kpi
    percent: Number = Field(default=10, gt=0, le=100)
    driver_ids: list[NodeId] | None = Field(default=None, min_length=1, max_length=200)

    @model_validator(mode='after')
    def valid_selection(self):
        if self.version is not None and self.model_id is None:
            raise ValueError('Version requires a saved model ID')
        if self.driver_ids is not None and len(set(self.driver_ids)) != len(self.driver_ids):
            raise ValueError('Choose distinct drivers')
        return self


def scenario_row(row):
    result = dict(row)
    result['assumptions'] = json.loads(result.pop('assumptions_json'))
    return result


def list_saved(connection):
    principal = active_principal.get()
    return [scenario_row(row) for row in connection.execute('SELECT * FROM enterprise_scenarios WHERE model_id IS NOT NULL OR company_id IS NULL ORDER BY id DESC')
            if company_visible(principal, row['company_id'])]


def read_saved(connection, scenario_ids):
    placeholders = ','.join('?' for _ in scenario_ids)
    rows = connection.execute(f'SELECT * FROM enterprise_scenarios WHERE id IN ({placeholders}) AND (model_id IS NOT NULL OR company_id IS NULL)', scenario_ids)
    principal = active_principal.get()
    found = {row['id']: scenario_row(row) for row in rows if company_visible(principal, row['company_id'])}
    if len(found) != len(scenario_ids):
        raise api_error(404, 'enterprise_scenario_not_found', 'فيه سيناريو محفوظ مش موجود.')
    return [found[key] for key in scenario_ids]


def save(connection, name, snapshot, assumptions):
    with connection:
        connection.execute('BEGIN IMMEDIATE')
        company_id = snapshot.get('company_id')
        if snapshot['id'] is not None:
            owner = connection.execute('SELECT company_id FROM enterprise_models WHERE id = ?',
                                       (snapshot['id'],)).fetchone()
            company_id = owner['company_id']
        require_company(active_principal.get(), company_id)
        if company_id is not None and not connection.execute(
                'SELECT 1 FROM companies WHERE id = ?', (company_id,)).fetchone():
            raise api_error(404, 'company_not_found', 'الشركة مش موجودة.')
        scenario_id = connection.execute(
            '''INSERT INTO enterprise_scenarios (name, model_id, version, assumptions_json, created_at, company_id)
               VALUES (?, ?, ?, ?, ?, ?)''',
            (name, snapshot['id'], snapshot['version'],
             encode([item.model_dump(mode='json') for item in assumptions]), utc_now(), company_id),
        ).lastrowid
        audit(connection, company_id, 'enterprise.scenario.create', 'enterprise_scenario', scenario_id,
              {'model_id': snapshot['id'], 'version': snapshot['version']})
        return read_saved(connection, [scenario_id])[0]


def kpis(values, fill_rate):
    result = {key: values.get(node) for key, node in KPI_NODES.items()}
    if fill_rate is not None:
        result['fill_rate'] = fill_rate
    if any(value is not None and not isfinite(value) for value in result.values()):
        raise ImpactError('أثر المؤشر أكبر من النطاق الرقمي المدعوم.')
    return result


def compare(model, scenarios, body):
    base = model.baseline()
    baseline = kpis(base[0].metrics(), base[0].fill_rate)
    results = []
    for saved in scenarios:
        try:
            request = WhatIfRequest.model_validate({
                **body.model_dump(exclude={'scenario_ids'}),
                **{key: saved[key] for key in ('model_id', 'version', 'assumptions')},
            })
            result = model.scenario(request, base=base)
            metrics = kpis({row['id']: row['after'] for row in result['nodes']}, result['fill_rate'])
        except (ImpactError, OptimizationInputError) as error:
            raise ImpactError(f"السيناريو «{saved['name']}»: {error}") from None
        results.append({**saved, 'kpis': metrics})
    return {'kpis': kpi_specs(model.graph), 'baseline': baseline, 'scenarios': results,
            'settings': {**body.model_dump(exclude={'scenario_ids'}), 'weeks': model.weeks}}


def sweep_case(model, base, key, variation, kpi, reference):
    try:
        centers, _, _, inputs = model.assumption_values([
            Assumption(node_id=key, operation='pct', value=variation),
        ])
        plans = model.plans(inputs[1], 'scenario', base[1])
        result = model.evaluate(centers, inputs, plans, base[0])
        value = kpis(result.metrics(), result.fill_rate)[kpi]
        if value is None:
            return {'value': None, 'delta': None, 'error': 'قيمة المؤشر مش متاحة.'}
        delta = value - reference
        if not isfinite(delta):
            raise ImpactError('أثر المؤشر أكبر من النطاق الرقمي المدعوم.')
        return {'value': value, 'delta': delta, 'error': None}
    except (ImpactError, OptimizationInputError) as error:
        return {'value': None, 'delta': None, 'error': str(error)}
    except (OverflowError, FloatingPointError):
        return {'value': None, 'delta': None, 'error': 'الأرقام أكبر من النطاق المدعوم للحساب.'}


def tornado(model, body):
    drivers = {driver['id']: driver for driver in model.drivers()}
    selected = body.driver_ids if body.driver_ids is not None else list(drivers)
    if not selected or any(key not in drivers for key in selected):
        raise ImpactError('اختار مدخلات قابلة للتعديل في النموذج.')
    base = model.baseline()
    baseline = kpis(base[0].metrics(), base[0].fill_rate)[body.kpi]
    if baseline is None:
        raise ImpactError('المؤشر ده مش متاح في النموذج المختار.')
    rows = []
    for key in selected:
        low = sweep_case(model, base, key, -body.percent, body.kpi, baseline)
        high = sweep_case(model, base, key, body.percent, body.kpi, baseline)
        span = abs(high['value'] - low['value']) if low['error'] is None and high['error'] is None else None
        if span is not None and not isfinite(span):
            raise ImpactError('مدى تأثير المدخل أكبر من النطاق الرقمي المدعوم.')
        rows.append({'driver': drivers[key], 'low': low, 'high': high, 'span': span,
                     'zero_reference': drivers[key]['value'] == 0})
    rows.sort(key=lambda row: (row['span'] is None, -(row['span'] or 0), row['driver']['id']))
    return {'kpi': body.kpi, 'metric': kpi_specs(model.graph)[body.kpi], 'percent': body.percent,
            'baseline': baseline, 'weeks': model.weeks, 'rows': rows}
