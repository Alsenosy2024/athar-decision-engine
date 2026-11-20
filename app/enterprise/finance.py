"""Daily operating ledger and bounded cash/facility settlement in SAR."""
from collections import defaultdict
from dataclasses import dataclass, field

import numpy as np

from app.enterprise.impact_graph import ImpactError

MAX_ITERATIONS = 40
TOLERANCE = 0.00001


def settle(position, limit, daily_rate, previous_draw):
    draw = previous_draw
    for iteration in range(1, MAX_ITERATIONS + 1):
        balance = position + draw * (1 - daily_rate)
        next_draw = min(limit, max(0, draw - balance))
        if abs(draw - next_draw) <= TOLERANCE:
            interest = next_draw * daily_rate
            settled = position + next_draw - interest
            return (0.0 if abs(settled) <= TOLERANCE else settled), next_draw, interest, iteration
        draw = next_draw
    raise ImpactError('حساب السيولة والتسهيل ما استقرش داخل الحد الأقصى للتكرار.')


@dataclass
class Ledger:
    days: int
    books: dict = field(default_factory=lambda: defaultdict(lambda: defaultdict(float)))

    def post(self, book, day, amount):
        self.books[book][day] += amount

    def series(self, book):
        return np.array([self.books[book].get(day, 0) for day in range(self.days)], dtype=float)

    def close(self, opening, limit, rate):
        revenue = {sector: self.series(f'revenue.{sector}') for sector in ('production', 'contracting')}
        receipts = {sector: self.series(f'receipts.{sector}') for sector in revenue}
        expenses = self.series('expenses')
        purchases = self.series('purchases')
        costs = self.series('direct_cost')
        incurred = purchases + self.series('contract_cost') + expenses
        payments = self.series('payments')
        cash, draws, interest = [], [], []
        balance, outstanding, max_iterations = opening, 0.0, 0
        for flow in sum(receipts.values()) - payments:
            balance, outstanding, charge, iterations = settle(
                balance - outstanding + flow, limit, rate / 7, outstanding,
            )
            cash.append(balance)
            draws.append(outstanding)
            interest.append(charge)
            max_iterations = max(max_iterations, iterations)
        total_revenue = sum(revenue.values())
        return {
            'cash': np.array([opening, *cash]),
            'financing.draw': np.array(draws),
            'financing': np.cumsum(interest),
            'ar.production': np.cumsum(revenue['production'] - receipts['production']),
            'ar.contracting': np.cumsum(revenue['contracting'] - receipts['contracting']),
            'ap': np.cumsum(incurred - payments),
            'pnl.revenue': np.cumsum(total_revenue),
            'pnl.direct_cost': np.cumsum(costs),
            'pnl.expenses': np.cumsum(expenses),
            'pnl.result': np.cumsum(total_revenue - costs - expenses - interest),
            'procurement': np.cumsum(purchases),
            'risk.credit': np.array([100.0 if min(cash) < -0.005 else 0.0]),
        }, max_iterations
