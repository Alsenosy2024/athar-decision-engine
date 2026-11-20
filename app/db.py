import json
import sqlite3
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from pathlib import Path

from fastapi import Request

audit_identity = ContextVar("audit_identity", default=None)

DDL = """
CREATE TABLE IF NOT EXISTS companies (id INTEGER PRIMARY KEY, name TEXT NOT NULL, template TEXT NOT NULL CHECK(template IN ('contracting','production')), created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS datasets (id INTEGER PRIMARY KEY, company_id INTEGER NOT NULL REFERENCES companies(id), name TEXT NOT NULL, source TEXT NOT NULL CHECK(source IN ('demo','upload')), payload_json TEXT NOT NULL, validation_json TEXT NOT NULL, is_active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS company_models (id INTEGER PRIMARY KEY, company_id INTEGER NOT NULL REFERENCES companies(id), version INTEGER NOT NULL, model_json TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS forecasts (id INTEGER PRIMARY KEY, company_id INTEGER NOT NULL, dataset_id INTEGER NOT NULL, method TEXT NOT NULL, result_json TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS runs (id INTEGER PRIMARY KEY, company_id INTEGER NOT NULL, dataset_id INTEGER NOT NULL, model_id INTEGER NOT NULL, kind TEXT NOT NULL CHECK(kind IN ('optimize','whatif','montecarlo')), base_run_id INTEGER, scenario_json TEXT, result_json TEXT NOT NULL, status TEXT NOT NULL, wall_s REAL NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS plans (id INTEGER PRIMARY KEY, company_id INTEGER NOT NULL, run_id INTEGER NOT NULL REFERENCES runs(id), variant TEXT NOT NULL CHECK(variant IN ('optimized','scenario')), status TEXT NOT NULL CHECK(status IN ('draft','approved','rejected')), comment TEXT, actor TEXT, created_at TEXT NOT NULL, decided_at TEXT);
CREATE TABLE IF NOT EXISTS audit_log (id INTEGER PRIMARY KEY, company_id INTEGER, actor TEXT NOT NULL, action TEXT NOT NULL, entity TEXT NOT NULL, entity_id INTEGER, details_json TEXT, ts TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS actuals (id INTEGER PRIMARY KEY, company_id INTEGER NOT NULL, plan_id INTEGER NOT NULL REFERENCES plans(id), payload_json TEXT NOT NULL, comparison_json TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS hybrid_observations (run_id INTEGER PRIMARY KEY REFERENCES runs(id), result_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS calibrations (id INTEGER PRIMARY KEY, company_id INTEGER NOT NULL, key TEXT NOT NULL, value_json TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(company_id,key));
CREATE TABLE IF NOT EXISTS chat_messages (id INTEGER PRIMARY KEY, company_id INTEGER NOT NULL, role TEXT NOT NULL CHECK(role IN ('user','assistant')), content TEXT NOT NULL, scenario_json TEXT, run_id INTEGER, provider TEXT, ts TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS enterprise_models (
    id INTEGER PRIMARY KEY,
    template TEXT NOT NULL CHECK(template = 'enterprise'),
    archived_at TEXT
);
CREATE TABLE IF NOT EXISTS enterprise_model_versions (
    model_id INTEGER NOT NULL REFERENCES enterprise_models(id),
    version INTEGER NOT NULL CHECK(version >= 1),
    name TEXT NOT NULL,
    graph_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(model_id, version)
);
CREATE TABLE IF NOT EXISTS enterprise_scenarios (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    model_id INTEGER,
    version INTEGER,
    assumptions_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    CHECK ((model_id IS NULL) = (version IS NULL)),
    FOREIGN KEY(model_id, version) REFERENCES enterprise_model_versions(model_id, version)
);
CREATE TABLE IF NOT EXISTS erp_connectors (
    id INTEGER PRIMARY KEY,
    company_id INTEGER NOT NULL REFERENCES companies(id),
    config_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS p6_imports (
    id INTEGER PRIMARY KEY,
    company_id INTEGER NOT NULL REFERENCES companies(id),
    filename TEXT NOT NULL,
    schedule_json TEXT NOT NULL,
    xml BLOB NOT NULL,
    created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS datasets_one_active ON datasets(company_id) WHERE is_active = 1;
CREATE UNIQUE INDEX IF NOT EXISTS company_models_version ON company_models(company_id, version);
"""


@contextmanager
def get_conn(path: Path):
    connection = sqlite3.connect(path, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute('PRAGMA foreign_keys = ON')
        with connection:
            yield connection
    finally:
        connection.close()


def request_conn(request: Request):
    with get_conn(request.app.state.settings.db_path) as connection:
        yield connection


def initialize(path: Path):
    from app.connectors.ownership import OWNERSHIP_TRIGGERS

    path.parent.mkdir(parents=True, exist_ok=True)
    with get_conn(path) as connection:
        connection.execute('PRAGMA journal_mode = WAL')
        connection.executescript(DDL)
        connection.execute('BEGIN IMMEDIATE')
        columns = {row['name'] for row in connection.execute('PRAGMA table_info(erp_connectors)')}
        if 'company_id' not in columns:
            # Legacy settings have no trusted owner. Preserve them without granting access.
            connection.execute('ALTER TABLE erp_connectors ADD COLUMN company_id INTEGER REFERENCES companies(id)')
        connection.execute('CREATE INDEX IF NOT EXISTS erp_connectors_company ON erp_connectors(company_id, id)')
        for trigger in OWNERSHIP_TRIGGERS:
            connection.execute(trigger)
    from app.access import initialize_access_schema
    initialize_access_schema(path)


def utc_now():
    return datetime.now(UTC).isoformat()


def encode(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


def audit(connection, company_id, action, entity, entity_id, details=None, actor='local', user_id=None):
    identity = audit_identity.get()
    if identity is not None:
        user_id, actor = identity
    connection.execute(
        """INSERT INTO audit_log (company_id, actor, action, entity, entity_id, details_json, ts, user_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (company_id, actor, action, entity, entity_id, encode(details), utc_now(), user_id),
    )
