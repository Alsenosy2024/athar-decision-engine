"""Version-pinned, company-scoped AI review records."""
import json

from app.db import audit, encode, get_conn, utc_now
from app.enterprise import store
from app.enterprise.schema import ModelInput, Scalar, Series
from app.schemas import api_error

DDL = """
CREATE TABLE IF NOT EXISTS ai_reviews (
    id INTEGER PRIMARY KEY,
    company_id INTEGER NOT NULL REFERENCES companies(id),
    model_id INTEGER REFERENCES enterprise_models(id),
    model_version INTEGER,
    kind TEXT NOT NULL CHECK (kind IN ('entry', 'refresh', 'forecast', 'calibration', 'decision', 'answer')),
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'accepted', 'rejected')),
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_by_id INTEGER NOT NULL,
    decided_at TEXT,
    decided_by TEXT,
    decided_by_id INTEGER,
    outcome_json TEXT
);
CREATE INDEX IF NOT EXISTS ai_reviews_company_id ON ai_reviews (company_id, id DESC);
CREATE TABLE IF NOT EXISTS ai_review_p6 (
    review_id INTEGER NOT NULL REFERENCES ai_reviews(id),
    ordinal INTEGER NOT NULL,
    xml BLOB NOT NULL,
    PRIMARY KEY (review_id, ordinal)
);
CREATE TABLE IF NOT EXISTS ai_estimated_points (
    model_id INTEGER NOT NULL REFERENCES enterprise_models(id),
    node_id TEXT NOT NULL,
    observed_on TEXT NOT NULL,
    value REAL NOT NULL,
    review_id INTEGER NOT NULL REFERENCES ai_reviews(id),
    PRIMARY KEY (model_id, node_id, observed_on)
);
"""
SUMMARY = '''id, company_id, model_id, model_version, kind, status, created_at, created_by,
             created_by_id, decided_at, decided_by, decided_by_id, outcome_json'''


def initialize(path):
    with get_conn(path) as connection:
        connection.executescript(DDL)


def record(row):
    item = dict(row)
    item['outcome'] = json.loads(item.pop('outcome_json') or 'null')
    if 'payload_json' in item:
        item['data'] = json.loads(item.pop('payload_json'))
    return item


def recent(connection, company_id, before=None):
    return [record(row) for row in connection.execute(
        f'''SELECT {SUMMARY} FROM ai_reviews WHERE company_id = ? AND id < ?
            ORDER BY id DESC LIMIT 50''',
        (company_id, before or 2**63 - 1),
    )]


def read(connection, company_id, review_id):
    row = connection.execute(f'SELECT {SUMMARY}, payload_json FROM ai_reviews WHERE company_id = ? AND id = ?',
                             (company_id, review_id)).fetchone()
    if row is None:
        raise api_error(404, 'ai_review_not_found', 'مراجعة الذكاء الاصطناعي مش موجودة.')
    return record(row)


def propose(connection, company_id, user, kind, snapshot, data, *, xml_files=()):
    model_id = snapshot['id'] if snapshot else None
    version = snapshot['version'] if snapshot else None
    with connection:
        review_id = connection.execute(
            '''INSERT INTO ai_reviews
               (company_id, model_id, model_version, kind, payload_json, created_at, created_by, created_by_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
            (company_id, model_id, version, kind, encode(data), utc_now(), user.username, user.id),
        ).lastrowid
        if xml_files:
            connection.executemany('INSERT INTO ai_review_p6 (review_id, ordinal, xml) VALUES (?, ?, ?)',
                                   [(review_id, i, contents) for i, contents in enumerate(xml_files)])
        audit(connection, company_id, 'ai.propose.' + kind, 'ai_review', review_id,
              {'model_id': model_id, 'model_version': version})
        return read(connection, company_id, review_id)


def write_model(connection, company_id, existing, model: ModelInput):
    store.validate_link_entities(model.graph)
    if existing is None:
        model_id = connection.execute(
            "INSERT INTO enterprise_models (template, company_id) VALUES ('enterprise', ?)",
            (company_id,),
        ).lastrowid
        version = 1
    else:
        model_id = existing['id']
        version = existing['version'] + 1
    store.write_version(connection, model_id, version, model)
    audit(connection, company_id, 'enterprise.create' if existing is None else 'enterprise.update',
          'enterprise_model', model_id, {'version': version, 'source': 'ai_review'})
    return {'model_id': model_id, 'version': version}


def update_provenance(connection, model_id, review_id, actual, estimated):
    connection.executemany(
        'DELETE FROM ai_estimated_points WHERE model_id = ? AND node_id = ? AND observed_on = ?',
        [(model_id, item['node_id'], item['date']) for item in actual if item['date']],
    )
    connection.executemany(
        '''INSERT INTO ai_estimated_points (model_id, node_id, observed_on, value, review_id)
           VALUES (?, ?, ?, ?, ?) ON CONFLICT (model_id, node_id, observed_on)
           DO UPDATE SET value = excluded.value, review_id = excluded.review_id''',
        [(model_id, item['node_id'], item['date'], item['value'], review_id) for item in estimated],
    )


def observed_history(connection, model_id, graph, imported=(), *, empty_as_missing=False):
    stored = {(row['node_id'], row['observed_on']): row['value'] for row in connection.execute(
        'SELECT node_id, observed_on, value FROM ai_estimated_points WHERE model_id = ?', (model_id,))}
    for item in imported:
        stored.pop((item['node_id'], item['date']), None)
    nodes, excluded = [], {}
    for node in graph.nodes:
        base = node.baseline
        if isinstance(base, Series):
            points = [point for point in base.points if stored.get((node.id, point.date.isoformat())) != point.value]
            if (points or empty_as_missing) and len(points) < len(base.points):
                excluded[node.id] = len(base.points) - len(points)
                baseline = base.model_copy(update={'points': points}) if points else Scalar(value=None)
                node = node.model_copy(update={'baseline': baseline})
        nodes.append(node)
    return graph.model_copy(update={'nodes': nodes}), excluded


def save_recommendation(connection, company_id, snapshot, review_id, rank, recommendation):
    scenario_id = connection.execute(
        '''INSERT INTO enterprise_scenarios (name, model_id, version, assumptions_json, created_at, company_id)
           VALUES (?, ?, ?, ?, ?, ?)''',
        (f'اقتراح أثر {review_id} · {rank + 1}', snapshot['id'], snapshot['version'],
         encode(recommendation['assumptions']), utc_now(), company_id),
    ).lastrowid
    audit(connection, company_id, 'enterprise.scenario.create', 'enterprise_scenario', scenario_id,
          {'model_id': snapshot['id'], 'version': snapshot['version'], 'review_id': review_id})
    return {'scenario_id': scenario_id, 'rank': rank}


def save_schedules(connection, company_id, review_id, schedules):
    imports = []
    for row in connection.execute('SELECT ordinal, xml FROM ai_review_p6 WHERE review_id = ? ORDER BY ordinal',
                                  (review_id,)):
        schedule = schedules[row['ordinal']]
        filename = schedule['source'].rsplit('/', 1)[-1]
        import_id = connection.execute(
            'INSERT INTO p6_imports (company_id, filename, schedule_json, xml, created_at) VALUES (?, ?, ?, ?, ?)',
            (company_id, filename, encode(schedule['records']), row['xml'], utc_now()),
        ).lastrowid
        audit(connection, company_id, 'p6.import', 'p6_import', import_id, {'review_id': review_id})
        imports.append(import_id)
    return imports


def decide(connection, company_id, user, review_id, status, apply):
    with connection:
        connection.execute('BEGIN IMMEDIATE')
        review = read(connection, company_id, review_id)
        if review['status'] != 'pending':
            raise api_error(409, 'ai_review_decided', 'المراجعة دي اتسجل قرارها قبل كده.')
        current = None
        if status == 'accepted' and review['model_id'] is not None:
            current = store.read(connection, review['model_id'])
            if current['version'] != review['model_version'] or current['company_id'] != company_id:
                raise api_error(409, 'ai_stale_review', 'إصدار النموذج اتغيّر. اعمل معاينة جديدة.')
        outcome = apply(review, current) if status == 'accepted' else None
        connection.execute(
            '''UPDATE ai_reviews SET status = ?, decided_at = ?, decided_by = ?, decided_by_id = ?, outcome_json = ?
               WHERE id = ?''',
            (status, utc_now(), user.username, user.id, encode(outcome), review_id),
        )
        connection.execute('DELETE FROM ai_review_p6 WHERE review_id = ?', (review_id,))
        audit(connection, company_id, 'ai.' + status, 'ai_review', review_id, outcome)
        return read(connection, company_id, review_id)
