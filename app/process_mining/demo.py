"""Seeded synthetic event logs for the built-in company templates."""
import csv
from datetime import UTC, datetime, timedelta
from io import StringIO
from random import Random

from app.process_mining.events import COLUMNS

ROUTES = {
    'contracting': ('sales', 'projects', 'procurement', 'projects', 'accounts'),
    'production': ('sales', 'procurement', 'operations', 'inventory', 'accounts'),
    'enterprise': ('sales', 'procurement', 'operations', 'projects', 'accounts'),
    'trading': ('sales', 'procurement', 'inventory', 'operations', 'accounts'),
    'retail': ('procurement', 'inventory', 'sales', 'accounts'),
    'services': ('sales', 'projects', 'workforce', 'projects', 'accounts'),
    'custom': ('sales', 'operations', 'accounts'),
}
ACTIVITIES = {
    'sales': 'تسجيل الطلب', 'projects': 'تنفيذ المشروع', 'procurement': 'تجهيز التوريد',
    'accounts': 'مراجعة الفاتورة', 'operations': 'تنفيذ الطلب', 'inventory': 'تجهيز المخزون',
    'workforce': 'تخصيص الفريق',
}


def generate_csv(template, seed=42):
    rng = Random(seed)
    output = StringIO(newline='')
    writer = csv.writer(output)
    writer.writerow(COLUMNS)
    route = ROUTES[template]
    start = datetime(2026, 1, 5, tzinfo=UTC)
    for case in range(48):
        time = start + timedelta(hours=11 * case)
        steps = list(route)
        if case % 4 == 0:
            steps[2:2] = [steps[0], steps[1]]
        for department in steps:
            capacity = 1 if department in {'projects', 'operations'} else 3
            resource = f'{department}-{rng.randrange(capacity) + 1}'
            writer.writerow([f'demo-{case + 1}', ACTIVITIES[department], time.isoformat(), resource, department])
            time += timedelta(hours=rng.randint(16, 25) if capacity == 1 else rng.randint(2, 8))
    return output.getvalue().encode('utf-8')
