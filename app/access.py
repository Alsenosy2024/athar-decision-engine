"""Authentication storage and the API's role and company boundary."""
import os
from contextvars import ContextVar
from dataclasses import dataclass
from hashlib import sha256
from secrets import compare_digest, token_urlsafe
from time import time
from typing import Literal

from argon2 import PasswordHasher
from fastapi import Request
from starlette.concurrency import run_in_threadpool

from app.db import audit_identity, get_conn, utc_now
from app.schemas import api_error

Role = Literal['admin', 'planner', 'approver', 'viewer']
COOKIE = 'athar_session'
SESSION_SECONDS = 8 * 60 * 60
READ_METHODS = {'GET', 'HEAD', 'OPTIONS'}
PASSWORDS = PasswordHasher()
active_principal = ContextVar("active_principal", default=None)
DDL = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('admin', 'planner', 'approver', 'viewer')),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS user_companies (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    PRIMARY KEY(user_id, company_id)
);
CREATE TABLE IF NOT EXISTS auth_sessions (
    token_hash TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    csrf_token TEXT NOT NULL,
    expires_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS auth_sessions_user ON auth_sessions(user_id);
"""


@dataclass(frozen=True)
class Principal:
    id: int
    username: str
    role: Role
    company_ids: frozenset[int]
    csrf_token: str
    department_scoped: bool = False

    def public(self):
        return {'id': self.id, 'username': self.username, 'role': self.role,
                'company_ids': sorted(self.company_ids), 'csrf_token': self.csrf_token}


def initialize_access_schema(path):
    with get_conn(path) as connection:
        connection.executescript(DDL)
        connection.execute('BEGIN IMMEDIATE')
        if 'department_scoped' not in {row['name'] for row in connection.execute('PRAGMA table_info(users)')}:
            connection.execute('ALTER TABLE users ADD COLUMN department_scoped INTEGER NOT NULL DEFAULT 0')
        for table in ('audit_log', 'plans'):
            column = 'user_id' if table == 'audit_log' else 'decided_by_user_id'
            if column not in {row['name'] for row in connection.execute(f'PRAGMA table_info({table})')}:
                connection.execute(f'ALTER TABLE {table} ADD COLUMN {column} INTEGER REFERENCES users(id)')
        for table in ('enterprise_models', 'enterprise_scenarios'):
            if 'company_id' not in {row['name'] for row in connection.execute(f'PRAGMA table_info({table})')}:
                connection.execute(f'ALTER TABLE {table} ADD COLUMN company_id INTEGER REFERENCES companies(id)')


def seed_admin(path):
    with get_conn(path) as connection:
        connection.execute('BEGIN IMMEDIATE')
        if connection.execute('SELECT 1 FROM users LIMIT 1').fetchone():
            return
        username = os.getenv('ATHAR_ADMIN_USERNAME', 'admin').strip()
        password = os.getenv('ATHAR_ADMIN_PASSWORD')
        if password is None:
            return
        if not 1 <= len(username) <= 80 or not 12 <= len(password) <= 256:
            raise ValueError('Set a valid ATHAR_ADMIN_USERNAME and an ATHAR_ADMIN_PASSWORD of 12–256 characters.')
        connection.execute(
            "INSERT INTO users (username, password_hash, role, created_at) VALUES (?, ?, 'admin', ?)",
            (username, PASSWORDS.hash(password), utc_now()),
        )


def token_hash(token):
    return sha256(token.encode()).hexdigest()


def new_session(connection, user_id, previous_token):
    token = token_urlsafe(32)
    csrf_token = token_urlsafe(32)
    now = int(time())
    if previous_token:
        connection.execute('DELETE FROM auth_sessions WHERE token_hash = ?', (token_hash(previous_token),))
    connection.execute('DELETE FROM auth_sessions WHERE expires_at <= ?', (now,))
    connection.execute('INSERT INTO auth_sessions VALUES (?, ?, ?, ?)',
                       (token_hash(token), user_id, csrf_token, now + SESSION_SECONDS))
    return token


def principal_for_token(connection, token):
    if not token:
        return None
    row = connection.execute(
        '''SELECT u.id, u.username, u.role, s.csrf_token,
                  (u.department_scoped = 1 AND u.role <> 'admin') AS department_scoped FROM auth_sessions s
           JOIN users u ON u.id = s.user_id WHERE s.token_hash = ? AND s.expires_at > ?''',
        (token_hash(token), int(time())),
    ).fetchone()
    if row is None:
        return None
    companies = connection.execute('SELECT company_id FROM user_companies WHERE user_id = ?', (row['id'],))
    return Principal(**dict(row), company_ids=frozenset(item['company_id'] for item in companies))


def require_role(principal, *roles):
    if principal.role != 'admin' and principal.role not in roles:
        raise api_error(403, 'forbidden_role', 'صلاحيتك ما تسمحش بالإجراء ده.')


def company_visible(principal, company_id):
    return principal is None or principal.role == 'admin' or company_id in principal.company_ids


def require_company(principal, company_id):
    if not company_visible(principal, company_id):
        raise api_error(404, 'company_not_found', 'الشركة أو السجل المطلوب مش موجود.')
    return company_id


def request_principal(request):
    with get_conn(request.app.state.settings.db_path) as connection:
        return principal_for_token(connection, request.cookies.get(COOKIE))


def authorize(request):
    route = request.scope['route'].path
    principal = request_principal(request)
    if principal is None:
        raise api_error(401, 'authentication_required', 'سجّل دخولك الأول.')
    if principal.department_scoped and not route.startswith(('/api/department-owners/', '/api/auth/')):
        raise api_error(403, 'department_scope_required', 'حسابك مخصص لأقسامك في صفحة أصحاب البيانات.')

    method = request.method
    decision = route in {
        '/api/plans/{pid}/decide',
        '/api/enterprise/models/{model_id}/edges/{source}/{target}/approve',
        '/api/enterprise/models/{model_id}/prescriptive/calibration',
        '/api/enterprise/models/{model_id}/prescriptive/decisions/{decision_id}/decide',
    }
    if method not in READ_METHODS or route == '/api/plans/{pid}/export.{fmt}':
        supplied = request.headers.get('X-CSRF-Token', '').encode()
        if not compare_digest(supplied, principal.csrf_token.encode()):
            raise api_error(403, 'invalid_csrf', 'الجلسة أو رمز الحماية اتغيّر. حدّث الصفحة.')
    if decision and method not in READ_METHODS:
        require_role(principal, 'approver')
    elif method not in READ_METHODS and not route.startswith('/api/auth/') and route not in {
        '/api/enterprise/chat', '/api/companies/{cid}/ai/reviews/{review_id}/decide',
    }:
        if route == '/api/companies' or route.startswith('/api/users'):
            require_role(principal)
        else:
            require_role(principal, 'planner')

    params = request.path_params
    company_id = params.get('cid', params.get('company_id'))
    if company_id is not None:
        require_company(principal, path_id(company_id))
    if principal.role != 'admin':
        for key, table in (('did', 'datasets'), ('rid', 'runs'), ('pid', 'plans')):
            if key in params:
                with get_conn(request.app.state.settings.db_path) as connection:
                    row = connection.execute(f'SELECT company_id FROM {table} WHERE id = ?',
                                             (path_id(params[key]),)).fetchone()
                require_company(principal, row['company_id'] if row else None)
    return principal


def path_id(value):
    try:
        number = int(value)
        return number if 0 < number < 2**63 else None
    except ValueError:
        return None


async def protected(request: Request):
    principal = await run_in_threadpool(authorize, request)
    request.state.principal = principal
    active = active_principal.set(principal)
    identity = audit_identity.set((principal.id, principal.username))
    try:
        yield
    finally:
        audit_identity.reset(identity)
        active_principal.reset(active)
