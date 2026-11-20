import json
import os
from pathlib import Path
from typing import Annotated, Literal

import httpx
from pydantic import Field, StringConstraints
from sqlalchemy import URL, column, create_engine, select, table
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.pool import NullPool
from sqlalchemy.sql import quoted_name

from app.connectors.mapping import MAX_ROWS, ColumnMapping
from app.enterprise.schema import Label, StrictFields
from app.ingestion.loader import Sheet
from app.schemas import api_error

EnvName = Annotated[str, StringConstraints(pattern=r'^ATHAR_ERP_[A-Z0-9_]{1,75}$')]
Identifier = Annotated[str, StringConstraints(pattern=r'^[A-Za-z_][A-Za-z0-9_]{0,62}$')]
MAX_JSON_BYTES = 10 * 1024 * 1024


class RestConfig(StrictFields):
    kind: Literal['rest']
    name: Label
    url_env: EnvName
    token_env: EnvName | None = None
    rows_path: Annotated[str, StringConstraints(pattern=r'^(?:[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*)?$', max_length=200)] = ''
    mapping: ColumnMapping


class SqlConfig(StrictFields):
    kind: Literal['sql']
    name: Label
    url_env: EnvName
    table: Identifier
    schema_name: Identifier | None = None
    mapping: ColumnMapping


ConnectorConfig = Annotated[RestConfig | SqlConfig, Field(discriminator='kind')]


def env(name):
    value = os.environ.get(name)
    if not value:
        raise api_error(422, 'erp_env_missing', 'متغير الاتصال أو التوثيق مش متظبط على الخادم.')
    return value


def rest_rows(config):
    try:
        url = httpx.URL(env(config.url_env))
        if url.scheme != 'https' and not (url.scheme == 'http' and url.host in ('127.0.0.1', 'localhost', '::1')):
            raise ValueError
        headers = {'Accept': 'application/json'}
        if config.token_env:
            headers['Authorization'] = 'Bearer ' + env(config.token_env)
        data = bytearray()
        with httpx.Client(timeout=8, follow_redirects=False, trust_env=False) as client:
            with client.stream('GET', url, headers=headers) as response:
                response.raise_for_status()
                for chunk in response.iter_bytes(chunk_size=65536):
                    data.extend(chunk)
                    if len(data) > MAX_JSON_BYTES:
                        raise ValueError
        rows = json.loads(data)
        if config.rows_path:
            for key in config.rows_path.split('.'):
                rows = rows[key]
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise ValueError
        return rows
    except (httpx.HTTPError, httpx.InvalidURL, ValueError, TypeError, KeyError, RecursionError):
        raise api_error(502, 'erp_rest_failed', 'تعذّر قراءة استجابة ERP. راجع الاتصال ومسار سجلات JSON.') from None


def sqlite_read_only(url):
    # Rebuild the URL so options on the source cannot override read-only mode.
    if not url.database or url.database == ':memory:' or url.query:
        raise ValueError
    uri = Path(url.database).resolve().as_uri()
    return URL.create('sqlite+pysqlite', database=uri, query={'mode': 'ro', 'uri': 'true', 'timeout': '5'})


def sql_rows(config, columns):
    engine = None
    try:
        url = make_url(env(config.url_env))
        backend = url.get_backend_name()
        if url.drivername in ('sqlite', 'sqlite+pysqlite'):
            url = sqlite_read_only(url)
            options = {}
        elif url.drivername in ('postgresql', 'postgresql+psycopg', 'postgresql+psycopg2'):
            options = {'connect_args': {'connect_timeout': 5}}
        else:
            raise ValueError
        engine = create_engine(url, poolclass=NullPool, hide_parameters=True, **options)
        source = table(quoted_name(config.table, True),
                       *(column(quoted_name(name, True)) for name in columns),
                       schema=quoted_name(config.schema_name, True) if config.schema_name else None)
        with engine.connect() as connection:
            if backend == 'postgresql':
                connection.exec_driver_sql('SET TRANSACTION READ ONLY')
                connection.exec_driver_sql('SET LOCAL statement_timeout = 8000')
            else:
                connection.exec_driver_sql('PRAGMA query_only = ON')
            # Qualify columns with their table so an unknown SQLite column fails, even with DQS enabled.
            return connection.execute(select(*source.c).limit(MAX_ROWS + 1)).mappings().all()
    except (SQLAlchemyError, ImportError, ValueError, OSError):
        raise api_error(502, 'erp_sql_failed', 'تعذّر قراءة جدول ERP. راجع الاتصال والجدول والأعمدة وصلاحية القراءة.') from None
    finally:
        if engine is not None:
            engine.dispose()


def pull(config):
    columns = list(dict.fromkeys(config.mapping.columns()))
    rows = rest_rows(config) if config.kind == 'rest' else sql_rows(config, columns)
    if len(rows) > MAX_ROWS:
        raise api_error(422, 'too_many_rows', f'مصدر ERP محتاج يرجّع {MAX_ROWS} صف أو أقل.')
    return Sheet(columns, [(index, [row.get(key) for key in columns]) for index, row in enumerate(rows, 1)])
