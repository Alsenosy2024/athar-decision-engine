"""Adapt existing sector solvers and replay their plans in the shared ledger."""
from collections import defaultdict
from copy import deepcopy
from dataclasses import dataclass
from datetime import date

import numpy as np

from app.decision_loop.eligibility import assess, constraint_policy
from app.enterprise.demo import SAR_PER_THOUSAND
from app.enterprise.impact_graph import ImpactError
from app.forecasting.service import run_forecast
from app.ingestion.loader import load_demo
from app.optimization import contracting, production
from app.optimization.common import OptimizationTimeout
from app.simulation.montecarlo import contracting_trial_schedule, production_trial_plan
from app.templates_registry import load_templates

SOLVERS = {'production': production, 'contracting': contracting}


@dataclass
class Plan:
    dataset: dict
    parameters: dict
    plan: dict
    status: str
    solver: dict | None = None


class Arms:
    def __init__(self, arms, sources, *, time_limit, forecast_engine, foundation):
        self.sources = sources
        self.datasets, self.defaults = {}, {}
        for arm in arms:
            sector = arm.sector
            source = sources.get(sector)
            if arm.input_source == 'client_plan' and source is None:
                raise ImpactError('حمّل لقطة الخطة المعتمدة قبل حساب الأثر.')
            self.datasets[sector] = source.dataset if source else load_demo(sector)
            self.defaults[sector] = (source.parameters if source else
                                     load_templates()[sector].model_defaults()['parameters'])
        self.time_limit = time_limit
        self.forecast_engine = forecast_engine
        self.foundation = foundation
        self.forecasts = {}
        self.optimizers_run = {'baseline': [], 'scenario': []}

    def inputs(self, sector, get):
        data = deepcopy(self.datasets[sector])
        params = self.defaults[sector].copy()
        if sector == 'production':
            params['credit_limit'] = int(get('credit', params['credit_limit']))
            params['opening_cash'] = int(get('cash.opening' if sector in self.sources else 'cash',
                                             params['opening_cash']))
            params['cold_capacity'] = int(get('storage', params['cold_capacity']))
            initial_capacity = sum(sku['initial_inventory'] * sku['volume'] for sku in data['skus'])
            if params['cold_capacity'] < initial_capacity:
                raise ImpactError('سعة التبريد أقل من حجم المخزون الافتتاحي.')
            extra_days = max(0, round(get('supplier', 5) - 5))
            planned_down = params.get('line_down', {})
            line_down = {}
            for line in data['lines']:
                key = line['id']
                downtime = max(extra_days, round(get('downtime.' + key, 0)))
                days = set(planned_down.get(key, ())) | set(range(min(params['horizon_days'], downtime)))
                if days or sector not in self.sources:
                    line_down[key] = sorted(days)
            params['line_down'] = line_down
            for sku in data['skus']:
                key = sku['id']
                sku['unit_price'] = round(get('price.' + key, sku['unit_price']))
            for order in data['orders']:
                key = order['sku_id']
                order['qty'] = round(order['qty'] * get('demand_factor.' + key, 1))
        else:
            limit = get('credit', params['credit_limit'] * SAR_PER_THOUSAND)
            # V1 contracting uses a zero-opening cash curve in thousands.
            params['credit_limit'] = int((limit + get('cash', 0)) / SAR_PER_THOUSAND)
            for resource in data['resources']:
                available = int(get('workforce.' + resource['id'], resource['capacity']))
                required = max((row['units'] for row in data['activities']
                                if row['resource'] == resource['id']), default=0)
                if available < required:
                    raise ImpactError(f"سعة الفريق {resource['id']} أقل من احتياج نشاط واحد.")
                resource['capacity'] = available
        return data, params

    def plan(self, sector, data, parameters, phase, previous=None):
        if previous and (data, parameters) == (previous.dataset, previous.parameters):
            return previous
        source = self.sources.get(sector)
        if source is not None:
            defaults = source.parameters | {'line_down': {key: days for key, days in
                                                           source.parameters.get('line_down', {}).items() if days}}
            if (data, parameters) == (source.dataset, defaults):
                return Plan(data, parameters, source.schedule, source.status,
                            {'state': 'approved_plan', 'approval_eligibility': {'eligible': True}})
        policy = constraint_policy(sector, {'constraints': source.constraints} if source else {})
        options = {'weights': source.weights} if source else {}
        result = SOLVERS[sector].solve(data, parameters, time_limit=self.time_limit, deterministic=True, **options)
        plan = result['optimized']['plan']
        if not plan and result['solver'].get('state') == 'no_result':
            raise OptimizationTimeout('مهلة البحث خلصت قبل الوصول لخطة تشغيل قابلة للتنفيذ.')
        if not plan:
            raise ImpactError(f"مفيش خطة قابلة للتنفيذ لقطاع {'الإنتاج' if sector == 'production' else 'المقاولات'}.")
        self.optimizers_run[phase].append(sector)
        solver = {key: value for key, value in result['solver'].items() if key != 'wall_s'}
        solver['approval_eligibility'] = assess(
            sector, data, parameters, policy, plan, solver, inputs_approved=True,
        )
        return Plan(data, parameters, plan, solver['status'], solver)

    def forecast(self, sector):
        if sector not in self.forecasts:
            result = run_forecast(sector, self.datasets[sector], engine=self.forecast_engine,
                                  foundation=self.foundation)
            self.forecasts[sector] = {series['key']: series for series in result['series']}
        return self.forecasts[sector]


def production_ledger(inputs, planned, offset, payable_lag, ledger, *, trial=False, horizon_days=None):
    data, params = inputs
    problem = production.prepare(data, params)
    if trial and (data, params) != (planned.dataset, planned.parameters):
        schedule = [row for row in planned.plan['schedule'] if row['day'] not in problem.down[row['line']]]
        departures, supply = [[] for _ in problem.days], [defaultdict(int) for _ in problem.days]
        for row in planned.plan['shipments']:
            departures[row['day']].append(row['order'])
        for row in schedule:
            supply[row['day']][row['sku']] += row['qty']
        problem, plan = production_trial_plan(
            problem, schedule, departures, supply, {key: row['qty'] for key, row in problem.orders.items()},
        )
    else:
        plan = planned.plan
    result = production.evaluate(problem, plan)['kpis']

    def expense(day, value):
        ledger.post('expenses', offset + day, value)
        ledger.post('payments', offset + day, value)

    line_output = {line: np.zeros(problem.horizon) for line in problem.lines}
    for row in result['schedule']:
        day = row['day']
        line_output[row['line']][day] += row['qty']
        spend = row['qty'] * problem.skus[row['sku']]['unit_cost']
        ledger.post('produced_units', offset + day, row['qty'])
        ledger.post('purchases', offset + day, spend)
        ledger.post('payments', offset + day + payable_lag, spend)
    for row in result['changeover_days']:
        expense(row['day'], problem.lines[row['line']]['changeover_cost'])
    for sku, stock in result['inventory'].items():
        for day, quantity in enumerate(stock):
            expense(day, quantity * problem.skus[sku]['holding_cost_per_day'] / 100)
    for row in result['shipments']:
        day = row['day']
        order = problem.orders[row['order']]
        sku = problem.skus[order['sku_id']]
        amount = order['qty'] * sku['unit_price']
        ledger.post('shipped_units', offset + day, order['qty'])
        ledger.post('revenue.production', offset + day, amount)
        ledger.post('receipts.production', offset + day + problem.lag, amount)
        ledger.post('direct_cost', offset + day, order['qty'] * sku['unit_cost'])
        expense(day, row['late_days'] * order['penalty_per_day'])
    window = problem.horizon if horizon_days is None else min(problem.horizon, horizon_days)
    ordered_units = sum(order['qty'] for order in problem.orders.values())
    shipped_units = sum(problem.orders[row['order']]['qty'] for row in result['shipments'] if row['day'] < window)
    return {
        'lines': {key: np.cumsum(value[:window]) for key, value in line_output.items()},
        'inventory': {key: stock[:window] for key, stock in result['inventory'].items()},
        'storage': np.array(result['storage_curve'][:window]) / max(1, problem.storage) * 100,
        'booked': sum(row['qty'] * problem.skus[row['sku_id']]['unit_price'] for row in problem.orders.values()),
        'fill_rate': 100 * shipped_units / ordered_units if ordered_units else None,
    }


def contracting_ledger(inputs, planned, offset, ledger, *, trial=False):
    data, params = inputs
    problem = contracting.prepare(data, params)
    rows = planned.plan['activities']
    starts = {row['activity_id']: row['start_week'] for row in rows}
    if trial and data != planned.dataset:
        priority = [row['activity_id'] for row in sorted(rows, key=lambda row: row['start_week'])]
        resources = {row['id']: row['capacity'] for row in data['resources']}
        durations = {activity.id: activity.duration for activity in problem.activities}
        problem, starts = contracting_trial_schedule(problem, resources, priority, durations)
    result = contracting.evaluate(problem, starts)['kpis']
    projects = defaultdict(list)
    for activity in problem.activities:
        start, finish = starts[activity.id], result['finishes'][activity.id]
        projects[activity.project].append(finish)
        cost = activity.cost * SAR_PER_THOUSAND
        for week in range(start, finish):
            for book in ('contract_cost', 'payments', 'direct_cost'):
                ledger.post(book, offset + week * 7, cost)
        revenue = float(problem.receipts[activity.id] * SAR_PER_THOUSAND)
        ledger.post('revenue.contracting', offset + finish * 7, revenue)
        ledger.post('receipts.contracting', offset + (finish + problem.lag) * 7, revenue)
    for key, late in result['late'].items():
        day = offset + max(projects[key]) * 7
        expense = late * problem.projects[key]['ld_per_week'] * SAR_PER_THOUSAND
        ledger.post('expenses', day, expense)
        ledger.post('payments', day, expense)
    return {'projects': {key: max(finishes) for key, finishes in projects.items()},
            'delay': result['late_weeks_total']}


def start_date(sector, params):
    return date.fromisoformat(params['plan_start_date' if sector == 'production' else 'project_start_date'])
