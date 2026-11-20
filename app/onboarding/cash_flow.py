"""Weekly accrual, stock and cash movements for onboarding forecasts."""
import numpy as np

FLOW_LABELS = {
    'sales.volume': 'المبيعات التراكمية المتوقعة',
    'pnl.revenue': 'الإيراد التراكمي المتوقع',
    'pnl.direct_cost': 'التكلفة المباشرة التراكمية',
    'pnl.expenses': 'مصروفات التشغيل التراكمية',
    'financing': 'تكلفة التمويل التراكمية',
    'pnl.result': 'النتيجة التراكمية بعد التمويل',
}


def lagged(values, weeks):
    result = np.zeros_like(values)
    if weeks < len(values):
        result[weeks:] = values[:len(values) - weeks]
    return result


def forecast_ledger(values, terms, weeks):
    if any(values.get(key) is None for key in ('cash.opening', 'ar', 'ap', *FLOW_LABELS, 'unit_cost')):
        return None

    sales = values['sales.volume']
    revenue = values['pnl.revenue']
    purchase_units = sales.copy()
    stock = values['inventory']
    opening_stock = float(stock[0]) if stock is not None else None
    if opening_stock is not None and terms.inventory_target is not None:
        # Replenishment arrives in the week of purchase. Draw down excess
        # opening stock before ordering more than the target requires.
        balance = opening_stock
        for week, sold in enumerate(sales):
            purchase_units[week] = max(0, sold + terms.inventory_target - balance)
            balance += purchase_units[week] - sold

    purchases = purchase_units * values['unit_cost']
    collected = lagged(revenue, terms.collection_lag_weeks)
    paid = lagged(purchases, terms.payment_lag_weeks)
    collected[0] += values['ar'][0]
    paid[0] += values['ap'][0]
    expenses, financing = values['pnl.expenses'], values['financing']
    incoming, outgoing = collected, paid + expenses + financing
    opening_cash = float(values['cash.opening'][0])
    ending = opening_cash + np.cumsum(incoming - outgoing)
    ar = np.cumsum(revenue - collected) + values['ar'][0]
    ap = np.cumsum(purchases - paid) + values['ap'][0]
    stock_end = (None if opening_stock is None
                 else opening_stock + np.cumsum(purchase_units - sales))
    profit = np.cumsum(values['pnl.result'])
    return [{
        'week': i + 1,
        'opening_cash': opening_cash if i == 0 else float(ending[i - 1]),
        'cash_in': float(incoming[i]), 'cash_out': float(outgoing[i]), 'closing_cash': float(ending[i]),
        'collections': float(collected[i]), 'supplier_payments': float(paid[i]),
        'operating_expenses': float(expenses[i]), 'financing_expenses': float(financing[i]),
        'closing_receivables': float(ar[i]), 'closing_payables': float(ap[i]),
        'opening_inventory': (None if stock_end is None else
                              opening_stock if i == 0 else float(stock_end[i - 1])),
        'purchased_units': float(purchase_units[i]), 'sold_units': float(sales[i]),
        'closing_inventory': None if stock_end is None else float(stock_end[i]),
        'revenue': float(revenue[i]), 'direct_cost': float(values['pnl.direct_cost'][i]),
        'profit': float(values['pnl.result'][i]), 'cumulative_profit': float(profit[i]),
    } for i in range(weeks)]
