"""Deterministic weekly acceptance history with explicit synthetic event labels."""
from datetime import date, timedelta

import numpy as np

from app.enterprise.schema import Node


def node(key, values, node_type='demand', unit='units', name=None):
    return Node.model_validate({
        'id': key, 'name_ar': name or key, 'type': node_type, 'unit': unit,
        'baseline': {'kind': 'series', 'frequency': 'weekly', 'points': [
            {'date': date(2024, 1, 1) + timedelta(weeks=index), 'value': float(value)}
            for index, value in enumerate(values)
        ]},
        'definition': {'kind': 'formula', 'expression': 'synthetic acceptance history'},
    })


def acceptance_history():
    rng = np.random.default_rng(20261120)
    weeks = np.arange(88)
    promotion_week = 73
    season = np.array([0, 19, 0, -19])[weeks % 4]
    sales = 100 + season + rng.normal(0, .18, len(weeks))
    sales[promotion_week] += 31
    cash = -12 + weeks * .8 + rng.normal(0, 3, len(weeks))
    stock = 82 - .45 * weeks - season + rng.normal(0, .3, len(weeks))
    return [
        node('sales', sales, name='المبيعات الأسبوعية'),
        node('cash', cash, 'cash', 'SAR', 'رصيد السيولة'),
        node('inventory', stock, 'inventory', 'units', 'رصيد المخزون'),
    ], {'sales': {promotion_week: 'promotion'}}
