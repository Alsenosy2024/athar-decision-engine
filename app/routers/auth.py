"""Local sign-in, session lifecycle, and administrator account management."""
import os
from secrets import token_urlsafe
from typing import Annotated

from argon2.exceptions import InvalidHashError, VerificationError
from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, Field, SecretStr, StringConstraints

from app.access import (
    COOKIE, PASSWORDS, SESSION_SECONDS, Role, new_session, principal_for_token, require_role, token_hash,
)
from app.db import audit, request_conn, utc_now
from app.schemas import api_error

public = APIRouter(prefix='/auth', tags=['auth'])
router = APIRouter(tags=['auth'])
USERNAME = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)]
DUMMY_HASH = PASSWORDS.hash(token_urlsafe(32))


class Credentials(BaseModel):
    model_config = ConfigDict(extra='forbid')
    username: USERNAME
    password: SecretStr = Field(min_length=1, max_length=256)


class UserAccess(BaseModel):
    model_config = ConfigDict(extra='forbid')
    role: Role
    company_ids: list[Annotated[int, Field(gt=0, lt=2**63, strict=True)]] = Field(default_factory=list, max_length=500)


class UserCreate(Credentials, UserAccess):
    password: SecretStr = Field(min_length=12, max_length=256)


class UserUpdate(UserAccess):
    password: SecretStr | None = Field(default=None, min_length=12, max_length=256)


@public.post('/login')
def login(body: Credentials, request: Request, response: Response, connection=Depends(request_conn)):
    if request.headers.get('X-Athar-Login') != '1':
        raise api_error(403, 'invalid_csrf', 'افتح صفحة تسجيل الدخول وحاول تاني.')
    user = connection.execute('SELECT * FROM users WHERE username = ?', (body.username,)).fetchone()
    verified_hash = user['password_hash'] if user else DUMMY_HASH
    password = body.password.get_secret_value()
    try:
        PASSWORDS.verify(verified_hash, password)
    except (VerificationError, InvalidHashError):
        user = None
    if user is None:
        raise api_error(401, 'invalid_credentials', 'اسم المستخدم أو كلمة السر غلط.')
    with connection:
        connection.execute('BEGIN IMMEDIATE')
        current = connection.execute('SELECT password_hash FROM users WHERE id = ?', (user['id'],)).fetchone()
        if current is None or current['password_hash'] != verified_hash:
            raise api_error(401, 'invalid_credentials', 'اسم المستخدم أو كلمة السر غلط.')
        token = new_session(connection, user['id'], request.cookies.get(COOKIE))
        principal = principal_for_token(connection, token)
        audit(connection, None, 'auth.login', 'user', principal.id,
              actor=principal.username, user_id=principal.id)
    response.set_cookie(COOKIE, token, max_age=SESSION_SECONDS, httponly=True, samesite='strict',
                        secure=request.url.scheme == 'https' or os.getenv('ATHAR_SECURE_COOKIE') == '1')
    response.headers['Cache-Control'] = 'no-store'
    return principal.public()


@router.get('/auth/me')
def me(request: Request, response: Response):
    response.headers['Cache-Control'] = 'no-store'
    return request.state.principal.public()


@router.post('/auth/logout', status_code=204)
def logout(request: Request, connection=Depends(request_conn)):
    principal = request.state.principal
    with connection:
        connection.execute('DELETE FROM auth_sessions WHERE token_hash = ?',
                           (token_hash(request.cookies[COOKIE]),))
        audit(connection, None, 'auth.logout', 'user', principal.id)
    response = Response(status_code=204)
    response.delete_cookie(COOKIE)
    return response


def user_details(connection, user_id):
    row = connection.execute('SELECT id, username, role FROM users WHERE id = ?', (user_id,)).fetchone()
    if row is None:
        raise api_error(404, 'user_not_found', 'المستخدم مش موجود.')
    grants = connection.execute('SELECT company_id FROM user_companies WHERE user_id = ? ORDER BY company_id',
                                (user_id,))
    return {**dict(row), 'company_ids': [row['company_id'] for row in grants]}


def set_grants(connection, user_id, body):
    for company_id in sorted(set(body.company_ids)):
        if not connection.execute('SELECT 1 FROM companies WHERE id = ?', (company_id,)).fetchone():
            raise api_error(422, 'invalid_company_grant', 'اختار شركات موجودة.')
    connection.execute('DELETE FROM user_companies WHERE user_id = ?', (user_id,))
    connection.executemany('INSERT INTO user_companies VALUES (?, ?)',
                           [(user_id, company_id) for company_id in sorted(set(body.company_ids))])


@router.get('/users')
def list_users(request: Request, connection=Depends(request_conn)):
    require_role(request.state.principal)
    return [user_details(connection, row['id']) for row in connection.execute('SELECT id FROM users ORDER BY id')]


@router.post('/users', status_code=201)
def create_user(body: UserCreate, connection=Depends(request_conn)):
    password_hash = PASSWORDS.hash(body.password.get_secret_value())
    with connection:
        connection.execute('BEGIN IMMEDIATE')
        if connection.execute('SELECT 1 FROM users WHERE username = ?', (body.username,)).fetchone():
            raise api_error(409, 'username_taken', 'اسم المستخدم مستخدم بالفعل.')
        user_id = connection.execute(
            'INSERT INTO users (username, password_hash, role, created_at) VALUES (?, ?, ?, ?)',
            (body.username, password_hash, body.role, utc_now()),
        ).lastrowid
        set_grants(connection, user_id, body)
        result = user_details(connection, user_id)
        audit(connection, None, 'user.create', 'user', user_id, result)
        return result


@router.put('/users/{user_id}')
def update_user(user_id: Annotated[int, Field(gt=0, lt=2**63)], body: UserUpdate,
                connection=Depends(request_conn)):
    password_hash = PASSWORDS.hash(body.password.get_secret_value()) if body.password else None
    with connection:
        connection.execute('BEGIN IMMEDIATE')
        previous = user_details(connection, user_id)
        if previous['role'] == 'admin' and body.role != 'admin' and not connection.execute(
                "SELECT 1 FROM users WHERE role = 'admin' AND id != ?", (user_id,)).fetchone():
            raise api_error(409, 'last_admin', 'لازم يفضل أدمن واحد على الأقل.')
        set_grants(connection, user_id, body)
        connection.execute('UPDATE users SET role = ?, password_hash = COALESCE(?, password_hash) WHERE id = ?',
                           (body.role, password_hash, user_id))
        if password_hash is not None:
            connection.execute('DELETE FROM auth_sessions WHERE user_id = ?', (user_id,))
        result = user_details(connection, user_id)
        audit(connection, None, 'user.update', 'user', user_id, {**result, 'password_reset': password_hash is not None})
        return result
