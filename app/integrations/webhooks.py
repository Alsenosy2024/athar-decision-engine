"""Transactional events and retryable HMAC-signed delivery."""
import asyncio
import json
import logging
import os
import time
from hashlib import sha256
from hmac import digest
from ipaddress import ip_address
from typing import Annotated, Literal
from urllib.parse import urlsplit

import httpx
from pydantic import Field, SecretStr, StringConstraints, TypeAdapter, field_validator

from app.db import encode, get_conn, utc_now
from app.enterprise.schema import StrictFields

logger = logging.getLogger(__name__)
Event = Literal['alert.triggered', 'decision.approved']
MAX_ATTEMPTS = 6
LEASE_SECONDS = 120
DDL = """
CREATE TABLE IF NOT EXISTS integration_webhook_routes (
    destination TEXT NOT NULL,
    company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    event TEXT NOT NULL,
    PRIMARY KEY(destination, company_id, event)
);
CREATE INDEX IF NOT EXISTS integration_webhook_company ON integration_webhook_routes(company_id, event);
CREATE TABLE IF NOT EXISTS integration_webhook_outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    destination TEXT NOT NULL,
    company_id INTEGER NOT NULL,
    event TEXT NOT NULL,
    body TEXT NOT NULL,
    next_at INTEGER NOT NULL DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 0,
    delivered_at TEXT,
    failed_at TEXT,
    FOREIGN KEY(destination, company_id, event)
        REFERENCES integration_webhook_routes(destination, company_id, event) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS integration_webhook_pending ON integration_webhook_outbox(next_at, id)
    WHERE delivered_at IS NULL AND failed_at IS NULL;
CREATE TRIGGER IF NOT EXISTS integration_approval_event AFTER INSERT ON audit_log
WHEN NEW.action IN ('enterprise.decision.approved', 'plan.approve') AND NEW.company_id IS NOT NULL
BEGIN
    INSERT INTO integration_webhook_outbox(destination, company_id, event, body)
    SELECT destination, NEW.company_id, event,
        json_object('event', event, 'company_id', NEW.company_id, 'occurred_at', NEW.ts, 'data',
            CASE NEW.action WHEN 'plan.approve' THEN (
                SELECT json_object('type', 'plan', 'id', id, 'run_id', run_id) FROM plans WHERE id = NEW.entity_id
            ) ELSE (
                SELECT json_object('type', 'enterprise_decision', 'id', id, 'model_id', model_id, 'version', version)
                FROM enterprise_decisions WHERE id = NEW.entity_id
            ) END
        )
    FROM integration_webhook_routes WHERE company_id = NEW.company_id AND event = 'decision.approved';
END;
"""


class Destination(StrictFields):
    name: Annotated[str, StringConstraints(pattern=r'^[A-Za-z0-9_-]{1,80}$')]
    company_id: int = Field(ge=1, lt=2**63, strict=True)
    events: set[Event] = Field(min_length=1)
    url: str = Field(max_length=2048, repr=False)
    secret: SecretStr = Field(min_length=32)

    @field_validator('url')
    @classmethod
    def transport(cls, value):
        url = urlsplit(value)
        if not url.hostname or url.username is not None or url.fragment or any(char.isspace() for char in value):
            raise ValueError('Use an absolute webhook URL without credentials or a fragment.')
        _ = url.port
        if url.scheme == 'https':
            return value
        if url.scheme == 'http':
            if url.hostname == 'localhost':
                return value
            try:
                if ip_address(url.hostname).is_loopback:
                    return value
            except ValueError:
                pass
        raise ValueError('Webhook destinations require HTTPS or loopback HTTP.')

    @property
    def identity(self):
        return sha256(encode([self.name, self.company_id, self.url]).encode()).hexdigest()


def from_env():
    try:
        destinations = TypeAdapter(list[Destination]).validate_json(os.getenv('ATHAR_WEBHOOKS', '[]'))
        if len({item.name for item in destinations}) != len(destinations):
            raise ValueError
    except ValueError:
        raise ValueError('Invalid ATHAR_WEBHOOKS configuration. Check the destination fields and unique names.') from None
    return {item.identity: item for item in destinations}


def initialize(path, destinations):
    routes = {(key, item.company_id, event) for key, item in destinations.items() for event in item.events}
    with get_conn(path) as connection:
        connection.executescript(DDL)
        connection.execute('BEGIN IMMEDIATE')
        old = {tuple(row) for row in connection.execute('SELECT * FROM integration_webhook_routes')}
        connection.executemany(
            'DELETE FROM integration_webhook_routes WHERE destination = ? AND company_id = ? AND event = ?', old - routes,
        )
        connection.executemany('INSERT INTO integration_webhook_routes VALUES (?, ?, ?)', sorted(routes - old))


def enqueue_alert(connection, company_id, data):
    event = 'alert.triggered'
    body = encode({'event': event, 'company_id': company_id, 'occurred_at': utc_now(), 'data': data})
    connection.execute(
        '''INSERT INTO integration_webhook_outbox (destination, company_id, event, body)
           SELECT destination, company_id, event, ? FROM integration_webhook_routes
           WHERE company_id = ? AND event = ?''', (body, company_id, event),
    )


def claim(path, now, destinations):
    with get_conn(path) as connection:
        connection.execute('BEGIN IMMEDIATE')
        # An expired final lease may be left behind by an interrupted worker.
        connection.execute(
            '''UPDATE integration_webhook_outbox SET failed_at = ?
               WHERE delivered_at IS NULL AND failed_at IS NULL AND next_at <= ? AND attempts >= ?''',
            (utc_now(), now, MAX_ATTEMPTS),
        )
        return connection.execute(
            '''UPDATE integration_webhook_outbox SET next_at = ?, attempts = attempts + 1 WHERE id = (
                   SELECT id FROM integration_webhook_outbox
                   WHERE delivered_at IS NULL AND failed_at IS NULL AND next_at <= ? AND attempts < ?
                     AND destination IN (SELECT value FROM json_each(?))
                   ORDER BY next_at, id LIMIT 1
               ) RETURNING *''', (now + LEASE_SECONDS, now, MAX_ATTEMPTS, encode(list(destinations))),
        ).fetchone()


def deliver(client, destination, row, timestamp):
    body = row['body'].encode()
    event_id = str(row['id'])
    signed = f'{timestamp}.{event_id}.'.encode() + body
    signature = digest(destination.secret.get_secret_value().encode(), signed, 'sha256').hex()
    try:
        with client.stream('POST', destination.url, content=body, headers={
            'Content-Type': 'application/json',
            'X-Athar-Event-ID': event_id,
            'X-Athar-Timestamp': str(timestamp),
            'X-Athar-Signature': 'sha256=' + signature,
        }) as response:
            return response.is_success
    except httpx.HTTPError:
        return False


def flush(path, destinations, *, clock=time.time, limit=10):
    with httpx.Client(timeout=5, follow_redirects=False, trust_env=False) as client:
        for _ in range(limit):
            timestamp = int(clock())
            row = claim(path, timestamp, destinations)
            if row is None:
                return
            sent = deliver(client, destinations[row['destination']], row, timestamp)
            now, attempts = int(clock()), row['attempts']
            with get_conn(path) as connection:
                connection.execute(
                    '''UPDATE integration_webhook_outbox SET delivered_at = ?, failed_at = ?, next_at = ?
                       WHERE id = ? AND attempts = ?''',
                    (utc_now() if sent else None, utc_now() if not sent and attempts >= MAX_ATTEMPTS else None,
                     now + min(300, 5 * 2**(attempts - 1)), row['id'], attempts),
                )
            if not sent and attempts >= MAX_ATTEMPTS:
                logger.warning('Athar webhook %s reached its delivery attempt limit.', row['id'])


async def worker(settings, destinations, stop):
    from app.integrations.alerts import refresh_alerts

    next_scan = 0
    while not stop.is_set():
        try:
            await asyncio.to_thread(flush, settings.db_path, destinations)
            if not stop.is_set() and time.monotonic() >= next_scan:
                await asyncio.to_thread(refresh_alerts, settings)
                next_scan = time.monotonic() + 30
        except Exception as error:
            logger.warning('Athar webhook job failed (%s).', type(error).__name__)
        try:
            await asyncio.wait_for(stop.wait(), timeout=1)
        except TimeoutError:
            pass
