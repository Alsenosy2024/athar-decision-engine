"""Exploratory demand paths. Confirmed order quantities are immutable."""
from collections import defaultdict
from dataclasses import dataclass, replace
from hashlib import sha256
import json
import math

import numpy as np

from app.decision_loop.contract import BANDS, production_targets
from app.optimization.common import OptimizationInputError


@dataclass(frozen=True)
class Demand:
    orders: dict
    release: dict
    forecast: tuple
    bands: dict | None = None

    @classmethod
    def from_contract(cls, dataset, problem, contract):
        orders = dict(problem.orders)
        release = dict.fromkeys(orders, 0)
        targets = {
            band: production_targets(dataset, contract | {'selected_scenario': band})
            for band in BANDS
        }
        penalty = defaultdict(float)
        for order in problem.orders.values():
            if order['qty']:
                sku = order['sku_id']
                penalty[sku] = max(penalty[sku], order['penalty_per_day'] / order['qty'])
        bands = {}
        for index, (sku, due) in enumerate(targets['p50']):
            low, center, high = (targets[band][sku, due]['unbooked_units'] for band in BANDS)
            if high > 10**9:
                raise OptimizationInputError('الطلب المتوقع أكبر من نطاق تجربة الإنتاج.')
            key = f'forecast:{index}'
            while key in orders:
                key += ':'
            orders[key] = {'id': key, 'sku_id': sku, 'qty': center, 'due_day': due, 'is_export': False,
                           'is_forecast': True, 'penalty_per_day': round(center * penalty[sku])}
            release[key] = due // 7 * 7
            bands[key] = (low, center, high)
        return cls(orders, release, tuple(bands), bands)

    @classmethod
    def from_problem(cls, problem, uplift):
        orders = dict(problem.orders)
        release = dict.fromkeys(orders, 0)
        forecast = []
        for index, order in enumerate(problem.orders.values()):
            quantity = round(order['qty'] * uplift)
            if not quantity or order['due_day'] == 0:
                continue
            key = f'forecast:{index}'
            while key in orders:
                key += ':'
            due = order['due_day']
            orders[key] = order | {'id': key, 'qty': quantity, 'is_forecast': True,
                                   'penalty_per_day': round(order['penalty_per_day'] * uplift)}
            release[key] = max(1, due // 7 * 7)
            forecast.append(key)
        return cls(orders, release, tuple(forecast))

    def problem(self, base, quantities):
        orders = dict(self.orders)
        for key in self.forecast:
            if key in quantities:
                quantity = quantities[key]
                orders[key] = orders[key] | {'qty': quantity}
                if not quantity:
                    orders[key]['penalty_per_day'] = 0
        return replace(base, orders=orders)

    def observe(self, base, quantities, day):
        return self.problem(base, {key: quantity for key, quantity in quantities.items()
                                   if self.release[key] <= day})


def fingerprint(value):
    return sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def draw_paths(demand, count, seed):
    """Shared bounded market shock with temporal persistence and SKU noise."""
    rng = np.random.default_rng(seed)
    days = sorted({demand.release[key] for key in demand.forecast})
    skus = sorted({demand.orders[key]['sku_id'] for key in demand.forecast})
    paths = []
    for _ in range(count):
        markets = {}
        previous = rng.normal()
        for day in days:
            previous = 0.75 * previous + math.sqrt(1 - 0.75**2) * rng.normal()
            markets[day] = previous
        noise = {(day, sku): rng.normal() for day in days for sku in skus}
        path = {}
        for key in demand.forecast:
            order = demand.orders[key]
            day = demand.release[key]
            shock = 0.85 * markets[day] + 0.35 * noise[day, order['sku_id']]
            if demand.bands is None:
                path[key] = max(1, round(order['qty'] * (1 + 0.6 * math.tanh(shock))))
            else:
                percentile = (1 + math.erf(shock / (math.sqrt(2) * math.hypot(0.85, 0.35)))) / 2
                path[key] = round(float(np.interp(percentile, (0.1, 0.5, 0.9), demand.bands[key])))
        paths.append(path)
    return paths


def reduced_sample(paths, count):
    """Equal-mass strata by total demand. Preserve each stratum's probability."""
    indices = sorted(range(len(paths)), key=lambda i: sum(paths[i].values()))
    return [(int(group[len(group) // 2]), len(group) / len(paths))
            for group in np.array_split(indices, min(count, len(paths)))]


def check_observations(demand, observations):
    release_days = sorted({demand.release[key] for key in demand.forecast})
    for index, observed in enumerate(observations):
        day = observed.day
        keys = {key for key in demand.forecast if demand.release[key] == day}
        if index >= len(release_days) or day != release_days[index] or observed.quantities.keys() != keys:
            raise OptimizationInputError('سجّل الكميات لكل طلب تقديري بالترتيب في يوم ظهورها.')
    return {key: qty for entry in observations for key, qty in entry.quantities.items()}
