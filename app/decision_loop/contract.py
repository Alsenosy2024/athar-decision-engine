"""Versioned forecast-to-decision snapshots and deterministic replay evidence."""
from datetime import datetime, timedelta
from hashlib import sha256
import json
from math import ceil, isfinite

from ortools import __version__ as ortools_version

from app.optimization.common import DETERMINISTIC_TIME_RATIO, SOLVER_SEED, OptimizationInputError

SCHEMA_VERSION = 1
FORECAST_MAX_AGE = timedelta(hours=24)
BANDS = ('p10', 'p50', 'p90')


class ContractError(OptimizationInputError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def fingerprint(value):
    return sha256(json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True,
                             separators=(',', ':')).encode()).hexdigest()


def forecast_evidence(dataset, model, calibrations, cutoff):
    return {
        'version': SCHEMA_VERSION,
        'dataset_fingerprint': fingerprint(dataset),
        'calibrations': calibrations,
        'cutoff_at': cutoff,
        'planning_start': planning_start(model),
    }


def planning_start(model):
    parameters = model['parameters']
    return parameters.get('plan_start_date', parameters.get('project_start_date'))


def _snapshot(template, dataset, forecast, horizon):
    production = template == 'production'
    table = 'skus' if production else 'activities'
    entity_ids = sorted(row['id'] for row in dataset[table])
    by_entity = {row['key']: row for row in forecast['series']}
    if len(by_entity) != len(forecast['series']) or sorted(by_entity) != entity_ids or not entity_ids:
        raise ValueError('entities')
    periods = ceil(horizon / 7) if production else 1
    history = {}
    if production:
        for item in dataset['demand_history']:
            history.setdefault(item['sku_id'], []).append(item['week'])
        end = max(week for weeks in history.values() for week in weeks)
    for entity, row in by_entity.items():
        if production:
            weeks = row['history_weeks']
            if (not weeks or weeks != sorted(history[entity]) or weeks[-1] != end
                    or any(current - previous != 1 for previous, current in zip(weeks, weeks[1:]))):
                raise ValueError('weekly coverage')
        for band in BANDS:
            values = row[band]
            if len(values) < periods or any(type(x) not in (int, float) or not isfinite(x) or x < 0
                                            for x in values):
                raise ValueError('forecast coverage')
        if any(not low <= center <= high for low, center, high in zip(*(row[band] for band in BANDS))):
            raise ValueError('forecast bands')
    return {band: {entity: by_entity[entity][band][:periods] for entity in entity_ids} for band in BANDS}


def freeze_contract(*, company, dataset_id, dataset, dataset_created_at, model, weights,
                    forecast_id, forecast, forecast_created_at, cutoff_at, time_limit, previous_run_id,
                    previous):
    evidence = forecast.get('decision_evidence') or {}
    template = company['template']
    production = template == 'production'
    horizon = model['parameters']['horizon_days' if production else 'horizon_weeks']
    try:
        calibrations = evidence['calibrations']
        if (evidence['version'] != SCHEMA_VERSION
                or evidence['dataset_fingerprint'] != fingerprint(dataset)):
            raise ValueError('forecast evidence')
        generated = datetime.fromisoformat(forecast_created_at)
        source_cutoff = datetime.fromisoformat(evidence['cutoff_at'])
        cutoff = datetime.fromisoformat(cutoff_at)
        source_dates = [datetime.fromisoformat(dataset_created_at),
                        *(datetime.fromisoformat(row['updated_at']) for row in calibrations)]
        if (not max(source_dates) <= source_cutoff <= generated <= cutoff
                or cutoff - source_cutoff > FORECAST_MAX_AGE
                or evidence['planning_start'] != planning_start(model)):
            raise ContractError('forecast_stale', 'التوقع خارج وقت القطع أو مرتبط ببداية تخطيط مختلفة.')
        scenarios = _snapshot(template, dataset, forecast, horizon)
        if not production:
            phases = forecast['phases']
            for activity in dataset['activities']:
                phase = phases[activity['phase']]
                if not isfinite(phase['mu']) or not isfinite(phase['sigma']) or phase['sigma'] < 0:
                    raise ValueError('phase calibration')
    except ContractError:
        raise
    except (KeyError, TypeError, ValueError, OverflowError):
        raise ContractError('forecast_incomplete', 'التوقع لا يغطي كل الكيانات والفترات أو بيانات التتبع ناقصة.') from None

    contract = {
        'version': SCHEMA_VERSION,
        'company_id': company['id'],
        'template': template,
        'dataset_id': dataset_id,
        'dataset_fingerprint': evidence['dataset_fingerprint'],
        'forecast_id': forecast_id,
        'forecast_fingerprint': fingerprint(forecast),
        'forecast_cutoff_at': evidence['cutoff_at'],
        'forecast_created_at': forecast_created_at,
        'calibration_fingerprint': fingerprint(calibrations),
        'calibrations': calibrations,
        'scenario_set_fingerprint': fingerprint(scenarios),
        'scenarios': scenarios,
        'duration_phases': forecast['phases'] if not production else None,
        'selected_scenario': 'p50',
        'cutoff_at': cutoff_at,
        'planning_start': planning_start(model),
        'horizon': {'periods': horizon, 'unit': 'day' if production else 'week'},
        'forecast_period_unit': 'week' if production else 'activity',
        'unit': 'units' if production else 'weeks',
        'entity': 'sku' if production else 'activity',
        'approval_policy': 'shadow_only',
        'model_id': model['id'],
        'model_fingerprint': model_fingerprint(model),
        'weights': weights,
        'solver': solver_settings(time_limit),
        'previous_run_id': previous_run_id,
        'previous_plan_fingerprint': fingerprint(previous) if previous is not None else None,
    }
    return contract | {'input_fingerprint': fingerprint(contract)}


def model_fingerprint(model):
    return fingerprint({key: model[key] for key in ('parameters', 'constraints', 'weights')})


def solver_settings(time_limit):
    return {'engine': 'cp-sat', 'version': ortools_version, 'seed': SOLVER_SEED, 'workers': 1,
            'wall_limit_s': time_limit, 'deterministic_limit': DETERMINISTIC_TIME_RATIO * time_limit}


def solution_fingerprint(result):
    # Wall time, request IDs and narrative vary independently of the solved plan.
    solver = {key: result['solver'][key]
              for key in ('objective', 'bound', 'status', 'search_status', 'state', 'stop_reason')}
    return fingerprint({'baseline': result['baseline'], 'optimized': result['optimized'], 'solver': solver})


def production_targets(dataset, contract):
    if contract is None:
        return {}
    horizon = contract['horizon']['periods']
    scenarios = contract['scenarios'][contract['selected_scenario']]
    confirmed = {}
    for row in dataset['orders']:
        key = (row['sku_id'], row['due_day'] // 7)
        confirmed[key] = confirmed.get(key, 0) + row['qty']
    # The forecast is total demand. Reserve only the unbooked balance as stock
    # at the period end. A partial week uses a uniform daily allocation.
    return {
        (sku, min(7 * (week + 1), horizon) - 1): {
            'confirmed_units': confirmed.get((sku, week), 0),
            'expected_total_units': total,
            'unbooked_units': max(0, total - confirmed.get((sku, week), 0)),
        }
        for sku, values in scenarios.items()
        for week, predicted in enumerate(values)
        for total in [round(predicted * min(7, horizon - 7 * week) / 7)]
    }


def contracting_dataset(dataset, contract):
    if contract is None:
        return dataset
    durations = contract['scenarios'][contract['selected_scenario']]
    return dataset | {'activities': [
        row | {'duration_weeks': max(1, round(durations[row['id']][0]))}
        for row in dataset['activities']
    ]}
