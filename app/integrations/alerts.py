"""Evaluate subscribed alert thresholds when saved data or thresholds change."""
import json
import logging

from app.db import encode, get_conn
from app.enterprise import store
from app.enterprise.whatif import ImpactModel
from app.prescriptive import goal, monitor
from app.integrations.webhooks import enqueue_alert

logger = logging.getLogger(__name__)
DDL = """
CREATE TABLE IF NOT EXISTS integration_alert_state (
    model_id INTEGER PRIMARY KEY REFERENCES enterprise_models(id),
    version INTEGER NOT NULL,
    settings_json TEXT NOT NULL,
    active_json TEXT NOT NULL
);
"""
CURRENT = """
SELECT m.id, m.company_id, MAX(v.version) AS version, t.settings_json
FROM enterprise_kpi_thresholds t
JOIN enterprise_models m ON m.id = t.model_id AND m.archived_at IS NULL
JOIN enterprise_model_versions v ON v.model_id = m.id
WHERE EXISTS (
    SELECT 1 FROM integration_webhook_routes r WHERE r.company_id = m.company_id AND r.event = 'alert.triggered'
)
GROUP BY m.id
"""


def initialize(path):
    with get_conn(path) as connection:
        connection.executescript(DDL)


def same_revision(left, right):
    return left is not None and (left['version'], left['settings_json']) == (right['version'], right['settings_json'])


def save_alerts(path, evaluated, result):
    with get_conn(path) as connection:
        connection.execute('BEGIN IMMEDIATE')
        current = connection.execute(f'SELECT * FROM ({CURRENT}) WHERE id = ?', (evaluated['id'],)).fetchone()
        if not same_revision(current, evaluated):
            return
        previous = connection.execute('SELECT * FROM integration_alert_state WHERE model_id = ?',
                                      (evaluated['id'],)).fetchone()
        if same_revision(previous, evaluated):
            return
        prior = set(json.loads(previous['active_json'])) if previous else set()
        # Missing values keep their prior state until the engine can evaluate the threshold.
        active = prior.intersection(result['unavailable'])
        for item in result['alerts']:
            active.add(item['kpi'])
            if item['kpi'] not in prior:
                enqueue_alert(connection, current['company_id'],
                              {**item, 'model_id': current['id'], 'version': current['version']})
        connection.execute(
            '''INSERT INTO integration_alert_state VALUES (?, ?, ?, ?)
               ON CONFLICT(model_id) DO UPDATE SET version = excluded.version,
                   settings_json = excluded.settings_json, active_json = excluded.active_json''',
            (current['id'], current['version'], current['settings_json'], encode(sorted(active))),
        )


def refresh_alerts(settings):
    path = settings.db_path
    with get_conn(path) as connection:
        changed = connection.execute(f'''
            SELECT c.* FROM ({CURRENT}) c LEFT JOIN integration_alert_state s ON s.model_id = c.id
            WHERE s.model_id IS NULL OR s.version != c.version OR s.settings_json != c.settings_json
            ORDER BY c.id
        ''').fetchall()
    for row in changed:
        try:
            with get_conn(path) as connection:
                snapshot = store.read(connection, row['id'], row['version'])
            model = ImpactModel(snapshot['graph'], weeks=goal.WEEKS,
                                time_limit=settings.solver_time_limit, forecast_engine='stat')
            thresholds = monitor.Thresholds.model_validate_json(row['settings_json'])
            save_alerts(path, row, monitor.monitor(model, thresholds))
        except Exception as error:
            logger.warning('Athar could not evaluate webhook alerts for model %s (%s).',
                           row['id'], type(error).__name__)
