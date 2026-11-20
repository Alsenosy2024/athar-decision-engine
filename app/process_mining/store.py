"""Persist the latest discovery and its audit atomically for each company."""
from json import loads
from uuid import uuid4

from app.db import audit, encode, get_conn, utc_now


def initialize(path):
    with get_conn(path) as connection:
        connection.execute("""
            CREATE TABLE IF NOT EXISTS company_process_mining (
                company_id INTEGER PRIMARY KEY REFERENCES companies(id) ON DELETE CASCADE,
                revision TEXT NOT NULL,
                source TEXT NOT NULL CHECK(source IN ('actual', 'synthetic')),
                format TEXT NOT NULL CHECK(format IN ('csv', 'xes')),
                created_at TEXT NOT NULL,
                profile_json TEXT NOT NULL
            )
        """)


def read(connection, company_id):
    if company_id is None:
        return None
    row = connection.execute('SELECT * FROM company_process_mining WHERE company_id = ?', (company_id,)).fetchone()
    if row is None:
        return None
    result = dict(row)
    result['profile'] = loads(result.pop('profile_json'))
    return result


def save(connection, company_id, source, format_name, profile):
    revision, created_at = uuid4().hex, utc_now()
    with connection:
        connection.execute("""
            INSERT INTO company_process_mining VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(company_id) DO UPDATE SET revision=excluded.revision, source=excluded.source,
                format=excluded.format, created_at=excluded.created_at, profile_json=excluded.profile_json
        """, (company_id, revision, source, format_name, created_at, encode(profile)))
        audit(connection, company_id, 'process_mining.import', 'company_process_mining', company_id,
              {'revision': revision, 'source': source, 'format': format_name, 'events': profile['event_count']})
    return {'company_id': company_id, 'revision': revision, 'source': source, 'format': format_name,
            'created_at': created_at, 'profile': profile}


def report(record):
    profile = record['profile']
    return {**{key: value for key, value in record.items() if key != 'profile'},
            **{key: value for key, value in profile.items() if key != 'variants'}}
