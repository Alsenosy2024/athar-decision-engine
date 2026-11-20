"""Paired, predeclared H1 demo replan benchmark. Run with python -m qa.hybrid_h1_benchmark."""
from copy import deepcopy
from datetime import date, timedelta
import json
from math import ceil
import platform
from statistics import mean, median

from app.decision_loop.contract import solver_settings
from app.decision_loop.warm_start import PreviousPlan
from app.ingestion.loader import load_demo
from app.optimization import contracting, production
from app.templates_registry import load_templates

WALL_LIMIT = 5.0
REPEATS = 2


def shifted(start, days):
    return (date.fromisoformat(start) + timedelta(days=days)).isoformat()


def production_cases(dataset, params):
    for line in ('L1', 'L2'):
        for day in (2, 7):
            yield f'outage-{line}-{day}', dataset, params | {'line_down': {line: [day]}}
    for change in (-0.10, 0.10):
        current = deepcopy(dataset)
        current['orders'][0]['qty'] = round(current['orders'][0]['qty'] * (1 + change))
        yield f'qty-{change:+}', current, params
    for change in (-2, 2):
        current = deepcopy(dataset)
        current['orders'][0]['due_day'] += change
        yield f'due-{change:+}', current, params
    for change in (-0.15, 0.15):
        yield f'credit-{change:+}', dataset, params | {
            'credit_limit': round(params['credit_limit'] * (1 + change)),
        }
    current = deepcopy(dataset)
    current['orders'] = [row | {'due_day': max(0, row['due_day'] - 1)} for row in current['orders']]
    yield 'roll-one-day', current, params | {'plan_start_date': shifted(params['plan_start_date'], 1)}
    yield 'lag-plus-one', dataset, params | {'payment_lag_days': params['payment_lag_days'] + 1}


def contracting_cases(dataset, params):
    for crew in ('R1', 'R2'):
        for week in (4, 12):
            yield f'crew-{crew}-{week}', dataset, params | {'capacity_override': {crew: {str(week): 1}}}
    for change in (-2, 2):
        current = deepcopy(dataset)
        current['projects'][0]['deadline_week'] += change
        yield f'deadline-{change:+}', current, params
    for change in (-1, 1):
        current = deepcopy(dataset)
        current['activities'][0]['duration_weeks'] += change
        yield f'duration-{change:+}', current, params
    for change in (-0.15, 0.15):
        yield f'credit-{change:+}', dataset, params | {
            'credit_limit': round(params['credit_limit'] * (1 + change)),
        }
    current = deepcopy(dataset)
    current['projects'] = [row | {'nta_week': max(0, row['nta_week'] - 1),
                                  'deadline_week': row['deadline_week'] - 1} for row in current['projects']]
    yield 'roll-one-week', current, params | {'project_start_date': shifted(params['project_start_date'], 7)}
    yield 'lag-plus-one', dataset, params | {'payment_lag_weeks': params['payment_lag_weeks'] + 1}


def gap(solver):
    if solver['objective'] is None or solver['bound'] is None:
        return None
    return max(0, solver['objective'] - solver['bound']) / max(1, abs(solver['objective']))


def measure():
    rows = []
    for name, module, scenarios in (
        ('production', production, production_cases),
        ('contracting', contracting, contracting_cases),
    ):
        initial = load_demo(name)
        defaults = load_templates()[name].model_defaults()
        params = defaults['parameters']
        prior = module.solve(initial, params, defaults['weights'], WALL_LIMIT, deterministic=True)['optimized']
        assert prior['kpis']['credit_ok'] and prior['kpis'].get('feasible', True)
        previous = PreviousPlan(prior['plan'], initial, params)
        for index, (scenario, dataset, parameters) in enumerate(scenarios(initial, params)):
            for repeat in range(REPEATS):
                row = {'sector': name, 'case': scenario, 'repeat': repeat + 1}
                arms = {'before': None, 'after': previous}
                order = tuple(arms) if (index + repeat) % 2 == 0 else tuple(reversed(arms))
                for arm in order:
                    result = module.solve(dataset, parameters, defaults['weights'], WALL_LIMIT, deterministic=True,
                                          previous=arms[arm], measure_first=True)
                    solver = result['solver']
                    kpis = result['optimized']['kpis']
                    row[arm] = {
                        'first_qualified_s': solver['first_qualified_s'],
                        'gap': gap(solver),
                        'objective': solver['objective'],
                        'bound': solver['bound'],
                        'hint_source': solver['hint_source'],
                        'qualified_final': bool(kpis.get('credit_ok')) and kpis.get('feasible', True),
                        'search_status': solver['search_status'],
                    }
                rows.append(row)
    return rows


def percentile_95(values):
    return sorted(values)[ceil(0.95 * len(values)) - 1]


def summarize(rows):
    summary = {}
    for arm in ('before', 'after'):
        records = [row[arm] for row in rows]
        observed = [r['first_qualified_s'] for r in records if r['first_qualified_s'] is not None]
        # Non-events are censored at the wall limit, so a fast surviving subset
        # cannot make the arm look faster when it found fewer solutions.
        observed_or_limit = [WALL_LIMIT if r['first_qualified_s'] is None else r['first_qualified_s']
                             for r in records]
        gaps = [r['gap'] for r in records if r['gap'] is not None]
        summary[arm] = {
            'p95_first_qualified_s': percentile_95(observed_or_limit),
            'p95_right_censored': sum(t < WALL_LIMIT for t in observed_or_limit) < ceil(0.95 * len(rows)),
            'qualified_incumbents': len(observed),
            'qualified_final': sum(r['qualified_final'] for r in records),
            'mean_gap': mean(gaps) if gaps else None,
            'median_gap': median(gaps) if gaps else None,
            'known_gaps': len(gaps),
            'previous_hints': sum(r['hint_source'] == 'previous_plan' for r in records),
        }
    before = summary['before']['p95_first_qualified_s']
    summary['p95_reduction_pct'] = 100 * (1 - summary['after']['p95_first_qualified_s'] / before)
    paired = [row for row in rows
              if all(row[arm]['gap'] is not None and row[arm]['qualified_final']
                     for arm in ('before', 'after'))]
    if paired:
        summary['paired_qualified_gaps'] = {
            'n': len(paired),
            'before_pct': 100 * mean(row['before']['gap'] for row in paired),
            'after_pct': 100 * mean(row['after']['gap'] for row in paired),
            'max_delta_percentage_points': 100 * max(row['after']['gap'] - row['before']['gap']
                                                      for row in paired),
        }
    return summary


def main():
    rows = measure()
    groups = {'all': rows} | {
        key: [row for row in rows if row['sector'] == key] for key in ('production', 'contracting')
    }
    print(json.dumps({
        'environment': platform.platform(),
        'solver': solver_settings(WALL_LIMIT),
        'repeats': REPEATS,
        'summary': {key: summarize(group) for key, group in groups.items()},
        'pairs': rows,
    }, indent=2, allow_nan=False))


if __name__ == '__main__':
    main()
