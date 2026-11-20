"""Company-bound integration keys and short-lived map capabilities."""
import json
import os
import re
import secrets
import time
from base64 import urlsafe_b64decode, urlsafe_b64encode
from hmac import compare_digest, digest
from typing import Literal
from urllib.parse import urlsplit

from app.access import token_hash
from app.db import get_conn, utc_now
from app.schemas import api_error

Scope = Literal['kpis:read', 'map:read', 'whatif:run']
MAX_EMBED_SECONDS = 300
KEY_PATTERN = re.compile(r'athar_[A-Za-z0-9_-]{43}')
TOKEN_PATTERN = re.compile(r'([A-Za-z0-9_-]+)\.([A-Za-z0-9_-]{43})')
ORIGIN_HOST = re.compile(r'(?:[a-zA-Z0-9.-]+|\[[0-9a-fA-F:]+\])(?::[0-9]+)?')

DDL = """
CREATE TABLE IF NOT EXISTS integration_api_keys (
    id INTEGER PRIMARY KEY,
    company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    key_hash TEXT NOT NULL UNIQUE,
    scopes_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    revoked_at TEXT
);
CREATE INDEX IF NOT EXISTS integration_key_company ON integration_api_keys(company_id);
"""


def initialize(path):
    with get_conn(path) as connection:
        connection.executescript(DDL)


def public_key(row):
    result = dict(row)
    result.pop('key_hash', None)
    result['scopes'] = json.loads(result.pop('scopes_json'))
    return result


def issue_key(connection, company_id, name, scopes):
    token = 'athar_' + secrets.token_urlsafe(32)
    row = connection.execute(
        '''INSERT INTO integration_api_keys(company_id, name, key_hash, scopes_json, created_at)
           VALUES (?, ?, ?, ?, ?) RETURNING *''',
        (company_id, name, token_hash(token), json.dumps(sorted(scopes)), utc_now()),
    ).fetchone()
    return {**public_key(row), 'key': token}


def authentication_error(embed=False):
    if embed:
        return api_error(401, 'invalid_embed_token', 'رابط الخريطة غير صالح أو انتهت صلاحيته.')
    return api_error(401, 'invalid_api_key', 'مفتاح التكامل غايب أو غير صالح.')


def authenticate(connection, token, scope):
    if not token or not KEY_PATTERN.fullmatch(token):
        raise authentication_error()
    row = connection.execute(
        'SELECT * FROM integration_api_keys WHERE key_hash = ? AND revoked_at IS NULL', (token_hash(token),),
    ).fetchone()
    if row is None:
        raise authentication_error()
    key = public_key(row)
    if scope not in key['scopes']:
        raise api_error(403, 'insufficient_scope', 'المفتاح ما عندوش صلاحية للعملية دي.')
    return key


def from_env():
    secret = os.getenv('ATHAR_EMBED_SECRET')
    if secret is not None and len(secret.encode()) < 32:
        raise ValueError('ATHAR_EMBED_SECRET must be at least 32 bytes.')
    ancestors = (os.getenv('ATHAR_FRAME_ANCESTORS', "'self'")).split()
    if not ancestors or ("'none'" in ancestors and ancestors != ["'none'"]):
        raise ValueError('ATHAR_FRAME_ANCESTORS needs exact origins, self, or none.')

    for item in set(ancestors) - {"'self'", "'none'"}:
        url = urlsplit(item)
        if (url.scheme not in ('http', 'https') or not ORIGIN_HOST.fullmatch(url.netloc)
                or not url.hostname or url.path or url.query or url.fragment or item != f'{url.scheme}://{url.netloc}'):
            raise ValueError('ATHAR_FRAME_ANCESTORS needs exact origins without paths.')
        # Accessing port validates its range.
        _ = url.port
    return secret.encode() if secret is not None else secrets.token_bytes(32), ' '.join(ancestors)


def base64url(value):
    return urlsafe_b64encode(value).rstrip(b'=').decode('ascii')


def sign_embed(secret, key, model_id, version, lifetime):
    expires_at = int(time.time()) + lifetime
    claims = base64url(json.dumps([key['id'], key['company_id'], model_id, version, expires_at]).encode())
    signature = base64url(digest(secret, claims.encode(), 'sha256'))
    return f'{claims}.{signature}', expires_at


def verify_embed(connection, secret, token):
    match = TOKEN_PATTERN.fullmatch(token) if token and len(token) <= 512 else None
    if match is None:
        raise authentication_error(embed=True)
    claims, signature = match.groups()
    if not compare_digest(signature, base64url(digest(secret, claims.encode(), 'sha256'))):
        raise authentication_error(embed=True)
    try:
        key_id, company_id, model_id, version, expiry = json.loads(urlsafe_b64decode(claims + '=' * (-len(claims) % 4)))
        if any(type(value) is not int or not 0 < value < 2**63
               for value in (key_id, company_id, model_id, version, expiry)):
            raise ValueError
        if not 0 < expiry - int(time.time()) <= MAX_EMBED_SECONDS:
            raise ValueError
    except (ValueError, TypeError):
        raise authentication_error(embed=True) from None
    row = connection.execute(
        '''SELECT scopes_json FROM integration_api_keys WHERE id = ? AND company_id = ? AND revoked_at IS NULL''',
        (key_id, company_id),
    ).fetchone()
    if row is None or 'map:read' not in json.loads(row['scopes_json']):
        raise authentication_error(embed=True)
    return company_id, model_id, version
