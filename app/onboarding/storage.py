"""Atomic setup, preview and version commits using the enterprise model store."""
import json
from secrets import token_urlsafe
from time import time

from app.access import require_role
from app.company_data.projection import empty_graph
from app.company_data.schema import COMPANY_TEMPLATE
from app.db import audit, encode, get_conn, utc_now
from app.enterprise import store
from app.enterprise.schema import ModelInput
from app.onboarding.presets import SECTORS, graph_for
from app.onboarding.projection import materialize, readiness, record_key
from app.onboarding.records import Record
from app.schemas import api_error

PREVIEW_SECONDS = 30 * 60


def initialize(path):
    with get_conn(path) as connection:
        # SQLite cannot alter a CHECK constraint in place. Keep child FK targets
        # named companies throughout the rebuild and verify them before commit.
        connection.execute('PRAGMA foreign_keys = OFF')
        connection.execute('BEGIN IMMEDIATE')
        definition = connection.execute("SELECT sql FROM sqlite_master WHERE name='companies'").fetchone()['sql']
        if f"'{COMPANY_TEMPLATE}'" not in definition:
            sectors = ','.join(f"'{sector}'" for sector in ('contracting', 'production', *SECTORS, COMPANY_TEMPLATE))
            connection.execute(f"""
                CREATE TABLE companies_onboarding_upgrade (
                    id INTEGER PRIMARY KEY, name TEXT NOT NULL,
                    template TEXT NOT NULL CHECK(template IN ({sectors})), created_at TEXT NOT NULL)
            """)
            connection.execute('INSERT INTO companies_onboarding_upgrade SELECT id, name, template, created_at FROM companies')
            connection.execute('DROP TABLE companies')
            connection.execute('ALTER TABLE companies_onboarding_upgrade RENAME TO companies')
            if connection.execute('PRAGMA foreign_key_check').fetchone():
                raise RuntimeError('Onboarding migration would leave invalid company references.')

        connection.execute("""
            CREATE TABLE IF NOT EXISTS onboarding_companies (
                company_id INTEGER PRIMARY KEY REFERENCES companies(id),
                model_id INTEGER NOT NULL UNIQUE REFERENCES enterprise_models(id))
        """)
        connection.execute("""
            CREATE TABLE IF NOT EXISTS onboarding_previews (
                token TEXT PRIMARY KEY, company_id INTEGER NOT NULL REFERENCES onboarding_companies(company_id),
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                version INTEGER NOT NULL, expires_at INTEGER NOT NULL, payload TEXT NOT NULL)
        """)


def create(connection, body, principal):
    require_role(principal)
    graph = empty_graph() if body.sector == COMPANY_TEMPLATE else graph_for(body.sector)
    with connection:
        connection.execute('BEGIN IMMEDIATE')
        company_id = connection.execute(
            'INSERT INTO companies (name, template, created_at) VALUES (?, ?, ?)',
            (body.name, body.sector, utc_now()),
        ).lastrowid
        model_id = connection.execute(
            "INSERT INTO enterprise_models (template, company_id) VALUES ('enterprise', ?)", (company_id,),
        ).lastrowid
        connection.execute('INSERT INTO onboarding_companies VALUES (?, ?)', (company_id, model_id))
        store.write_version(connection, model_id, 1, ModelInput(name=body.name, graph=graph), origin='onboarding_template')
        audit(connection, company_id, 'onboarding.create', 'enterprise_model', model_id,
              {'sector': body.sector, 'version': 1})
    return describe(snapshot(connection, company_id, allow_raw=True))


def snapshot(connection, company_id, *, allow_raw=False):
    row = connection.execute('SELECT model_id FROM onboarding_companies WHERE company_id=?',
                             (company_id,)).fetchone()
    if row is None:
        raise api_error(404, 'onboarding_company_not_found', 'الشركة لسه ملهاش نموذج تجهيز.')
    model = store.read(connection, row['model_id'])
    if model['graph'].company_data is not None and not allow_raw:
        raise api_error(422, 'company_data_managed_model', 'استخدم معاينة ملفات الشركة والاعتماد.')
    return model


def describe(model):
    graph = model['graph']
    if graph.company_data is not None:
        return {'company_id': model['company_id'], 'model_id': model['id'], 'version': model['version'],
                'name': model['name'], 'sector': COMPANY_TEMPLATE, 'cash_terms': graph.company_data.cash_terms,
                'counts': {'entities': len(graph.company_data.entities), 'observations': len(graph.company_data.observations),
                           'nodes': len(graph.company_data.node_entities), 'edges': len(graph.edges)}}
    data = graph.onboarding
    return {'company_id': model['company_id'], 'model_id': model['id'], 'version': model['version'],
            'name': model['name'], 'sector': data.sector, 'records': data.records, 'cash_terms': data.cash_terms,
            'edges': [edge for edge in model['graph'].edges if edge.rule],
            'nodes': model['graph'].nodes, 'readiness': readiness(model['graph'])}


def check_version(model, expected):
    if model['version'] != expected:
        raise api_error(409, 'onboarding_version_conflict', 'الإصدار اتغيّر. حمّل البيانات وراجع التغييرات من جديد.')


def preview(connection, model, user_id, records=(), rules=None):
    graph = model['graph']
    current = {record_key(record): record for record in graph.onboarding.records}
    diff = [
        {'key': record.key, 'date': record.date, 'before': current.get(record_key(record)), 'after': record}
        for record in records if record != current.get(record_key(record))
    ]
    rules = rules or {}
    rule_diff = [
        {'key': key, 'before': edge.rule.multiplier, 'after': rules[key]}
        for edge in graph.edges if edge.rule and (key := f'{edge.source}:{edge.target}') in rules
        and rules[key] != edge.rule.multiplier
    ]
    if not diff and not rule_diff:
        return {'token': None, 'version': model['version'], 'diff': [], 'rule_diff': []}
    if len(current.keys() | {record_key(record) for record in records}) > 20000:
        raise api_error(422, 'too_many_onboarding_rows', 'الحد الأقصى 20,000 سجل محفوظ للشركة.')
    token = token_urlsafe(32)
    now = int(time())
    with connection:
        connection.execute('BEGIN IMMEDIATE')
        check_version(snapshot(connection, model['company_id']), model['version'])
        connection.execute('DELETE FROM onboarding_previews WHERE expires_at <= ?', (now,))
        connection.execute('INSERT INTO onboarding_previews VALUES (?, ?, ?, ?, ?, ?)', (
            token, model['company_id'], user_id, model['version'], now + PREVIEW_SECONDS,
            encode({'records': [item['after'].model_dump(mode='json') for item in diff],
                    'rules': {item['key']: item['after'] for item in rule_diff}}),
        ))
    return {'token': token, 'version': model['version'], 'diff': diff, 'rule_diff': rule_diff}


def commit(connection, company_id, user_id, token):
    with connection:
        connection.execute('BEGIN IMMEDIATE')
        row = connection.execute("""
            SELECT version, payload FROM onboarding_previews
            WHERE token=? AND company_id=? AND user_id=? AND expires_at>?
        """, (token, company_id, user_id, int(time()))).fetchone()
        if row is None:
            raise api_error(404, 'onboarding_preview_not_found', 'المعاينة مش موجودة أو انتهت. اعمل معاينة جديدة.')
        model = snapshot(connection, company_id)
        check_version(model, row['version'])
        payload = json.loads(row['payload'])
        graph = model['graph']
        records = {record_key(record): record for record in graph.onboarding.records}
        for record in map(Record.model_validate, payload['records']):
            records[record_key(record)] = record
        edges = []
        for edge in graph.edges:
            key = f'{edge.source}:{edge.target}'
            if key in payload['rules']:
                edge = edge.model_copy(update={
                    'name_ar': 'مرونة مخصصة تقديرية',
                    'rule': edge.rule.model_copy(update={'multiplier': payload['rules'][key]}),
                })
            edges.append(edge)
        graph = graph.model_copy(update={'edges': edges, 'onboarding': graph.onboarding.model_copy(
            update={'records': sorted(records.values(), key=lambda row: (row.key, row.date is not None, row.date))})})
        version = model['version'] + 1
        store.write_version(connection, model['id'], version,
                            ModelInput(name=model['name'], graph=materialize(graph)), origin='onboarding_review')
        audit(connection, company_id, 'onboarding.commit', 'enterprise_model', model['id'],
              {'version': version, 'records': len(payload['records']), 'rules': len(payload['rules'])})
        connection.execute('DELETE FROM onboarding_previews WHERE token=?', (token,))
        return describe(snapshot(connection, company_id))


def update_cash_terms(connection, company_id, expected_version, terms):
    with connection:
        connection.execute('BEGIN IMMEDIATE')
        model = snapshot(connection, company_id, allow_raw=True)
        check_version(model, expected_version)
        graph = model['graph']
        field = 'company_data' if graph.company_data is not None else 'onboarding'
        data = getattr(graph, field)
        if data.cash_terms == terms:
            return describe(model)
        updated = graph.model_copy(update={field: data.model_copy(update={'cash_terms': terms})})
        if graph.onboarding:
            updated = materialize(updated)
        version = model['version'] + 1
        store.write_version(connection, model['id'], version,
                            ModelInput(name=model['name'], graph=updated))
        audit(connection, company_id, 'onboarding.cash_terms', 'enterprise_model', model['id'],
              {'version': version, 'terms': terms.model_dump(mode='json')})
        return describe(snapshot(connection, company_id, allow_raw=True))
