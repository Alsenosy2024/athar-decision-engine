"""Cookie-independent map document for ERP frames."""
from html import escape
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse

from app.db import encode, request_conn
from app.integrations.credentials import verify_embed
from app.integrations.snapshots import project_map, snapshot_for

router = APIRouter(include_in_schema=False)
TEMPLATE = (Path(__file__).resolve().parents[1] / 'integrations' / 'map.html').read_text(encoding='utf-8')


@router.get('/embed/map', name='embedded_map', response_class=HTMLResponse)
def embedded_map(request: Request, token: str | None = None, connection=Depends(request_conn)):
    state = request.app.state
    try:
        company_id, model_id, version = verify_embed(connection, state.embed_secret, token)
        snapshot = snapshot_for(connection, company_id, model_id, version)
        payload = project_map(connection, company_id, snapshot)
        data = {key: payload[key] for key in ('company', 'model', 'departments', 'nodes', 'edges', 'kpis')}
        status, message = 200, ''
    except HTTPException as error:
        data, status, message = None, error.status_code, error.detail['msg_ar']
    markup = TEMPLATE.replace('<!-- ERROR -->', escape(message))
    # An HTML parser recognizes a closing script tag even in application/json.
    markup = markup.replace('<!-- PAYLOAD -->', encode(data).replace('<', '\\u003c'))
    csp = ("default-src 'none'; script-src 'self'; style-src 'self'; font-src 'self'; "
           "base-uri 'none'; form-action 'none'; frame-ancestors " + state.frame_ancestors)
    return HTMLResponse(markup, status_code=status, headers={
        'Content-Security-Policy': csp,
        'Cache-Control': 'no-store',
        'Referrer-Policy': 'no-referrer',
        'X-Content-Type-Options': 'nosniff',
        'X-Robots-Tag': 'noindex, nofollow',
    })
