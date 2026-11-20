"""Poll one configured customer workbook and stage immutable datasets for review."""
import asyncio
import hashlib
import json
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from app.db import audit, encode, get_conn, utc_now
from app.ingestion.loader import load_upload
from app.ingestion.validators import validate_dataset
from app.templates_registry import load_templates

MAX_BYTES = 10 * 1024 * 1024
logger = logging.getLogger(__name__)


class SourceInputError(ValueError):
    pass


def stage(db_path, company_id, path):
    if path.suffix.lower() != '.xlsx':
        raise SourceInputError('The scheduled production source must be an XLSX workbook.')
    with path.open('rb') as source:
        raw = source.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise SourceInputError('The scheduled workbook exceeds 10 MB.')
    digest = hashlib.sha256(raw).hexdigest()
    template = load_templates()['production']
    with get_conn(db_path) as connection:
        if row := existing(connection, company_id, digest):
            return row['dataset_id']
        sheets, errors = load_upload(path.name, raw, template)
        if errors:
            raise SourceInputError('The scheduled workbook could not be parsed.')
        connection.execute('BEGIN IMMEDIATE')
        if row := existing(connection, company_id, digest):
            return row['dataset_id']
        model = connection.execute("""
            SELECT m.model_json FROM company_models m JOIN companies c ON c.id = m.company_id
            WHERE m.company_id = ? AND c.template = 'production' ORDER BY m.version DESC LIMIT 1
        """, (company_id,)).fetchone()
        if model is None:
            raise SourceInputError('The configured company must be a production company.')
        payload, validation = validate_dataset(template, sheets, json.loads(model['model_json'])['parameters'])
        if not validation.ok:
            raise SourceInputError('The scheduled workbook failed production validation.')
        dataset_id = connection.execute("""
            INSERT INTO datasets (company_id, name, source, payload_json, validation_json, is_active, created_at)
            VALUES (?, ?, 'upload', ?, ?, 0, ?)
        """, (company_id, path.name, encode(payload), validation.model_dump_json(), utc_now())).lastrowid
        connection.execute('INSERT INTO operating_source_snapshots VALUES (?, ?, ?, ?)',
                           (company_id, digest, dataset_id, utc_now()))
        audit(connection, company_id, 'operating_source.stage', 'dataset', dataset_id, {'sha256': digest})
        return dataset_id


def existing(connection, company_id, digest):
    return connection.execute("""
        SELECT dataset_id FROM operating_source_snapshots WHERE company_id = ? AND sha256 = ?
    """, (company_id, digest)).fetchone()


def initialize(db_path):
    with get_conn(db_path) as connection:
        connection.execute("""
            CREATE TABLE IF NOT EXISTS operating_source_snapshots (
                company_id INTEGER NOT NULL REFERENCES companies(id), sha256 TEXT NOT NULL,
                dataset_id INTEGER NOT NULL UNIQUE REFERENCES datasets(id), staged_at TEXT NOT NULL,
                PRIMARY KEY (company_id, sha256)
            )
        """)


async def poll(db_path, company_id, path, interval, stop):
    retry = 0
    delay = interval
    while not stop.is_set():
        try:
            await asyncio.to_thread(stage, db_path, company_id, path)
            retry = 0
            delay = interval
        except Exception as error:
            logger.warning('Operating source scan for company %s failed (%s).', company_id, type(error).__name__)
            retry = min(retry + 1, 15)
            delay = min(interval, 5 * 2 ** (retry - 1))
        try:
            await asyncio.wait_for(stop.wait(), timeout=delay)
        except TimeoutError:
            pass


@asynccontextmanager
async def source_runtime(db_path):
    initialize(db_path)
    path = os.getenv('OPERATING_SOURCE_WORKBOOK')
    company = os.getenv('OPERATING_SOURCE_COMPANY_ID')
    job = None
    stop = asyncio.Event()
    if path or company:
        if not path or not company or not company.isdecimal() or not 1 <= int(company) < 2**63:
            raise ValueError('Configure both the operating workbook and a valid company ID.')
        interval = int(os.getenv('OPERATING_SOURCE_INTERVAL_S', '300'))
        if not 1 <= interval <= 86400:
            raise ValueError('Operating source interval must be 1–86400 seconds.')
        job = asyncio.create_task(poll(db_path, int(company), Path(path), interval, stop))
    try:
        yield
    finally:
        if job is not None:
            stop.set()
            await job
