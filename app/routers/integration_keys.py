"""Session-protected API key administration."""
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Request, Response
from pydantic import Field, StringConstraints

from app.access import require_role
from app.db import audit, request_conn, utc_now
from app.enterprise.schema import StrictFields
from app.integrations.credentials import Scope, issue_key, public_key
from app.schemas import api_error

router = APIRouter(prefix='/companies/{cid}/integration-keys', tags=['integration-keys'])
Id = Annotated[int, Path(ge=1, lt=2**63)]


class KeyInput(StrictFields):
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
    scopes: list[Scope] = Field(min_length=1, max_length=3)


def check_company(connection, cid):
    if not connection.execute('SELECT 1 FROM companies WHERE id = ?', (cid,)).fetchone():
        raise api_error(404, 'company_not_found', 'الشركة مش موجودة.')


@router.get('')
def list_keys(cid: Id, request: Request, connection=Depends(request_conn)):
    require_role(request.state.principal)
    check_company(connection, cid)
    return [public_key(row) for row in connection.execute(
        'SELECT * FROM integration_api_keys WHERE company_id = ? ORDER BY id', (cid,),
    )]


@router.post('', status_code=201)
def create_key(cid: Id, body: KeyInput, request: Request, response: Response, connection=Depends(request_conn)):
    require_role(request.state.principal)
    check_company(connection, cid)
    with connection:
        key = issue_key(connection, cid, body.name, set(body.scopes))
        audit(connection, cid, 'integration.key.create', 'integration_api_key', key['id'], {'scopes': key['scopes']})
    response.headers['Cache-Control'] = 'no-store'
    return key


@router.delete('/{key_id}', status_code=204)
def revoke_key(cid: Id, key_id: Id, request: Request, connection=Depends(request_conn)):
    require_role(request.state.principal)
    with connection:
        row = connection.execute(
            '''UPDATE integration_api_keys SET revoked_at = ?
               WHERE id = ? AND company_id = ? AND revoked_at IS NULL''', (utc_now(), key_id, cid),
        )
        if row.rowcount:
            audit(connection, cid, 'integration.key.revoke', 'integration_api_key', key_id)
        elif not connection.execute('SELECT 1 FROM integration_api_keys WHERE id = ? AND company_id = ?',
                                    (key_id, cid)).fetchone():
            raise api_error(404, 'integration_key_not_found', 'مفتاح التكامل مش موجود للشركة دي.')
    return Response(status_code=204)
