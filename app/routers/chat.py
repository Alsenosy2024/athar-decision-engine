import json
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field, JsonValue, StringConstraints

from app.db import request_conn
from app.llm.schema import Intent, Scenario, StrictBody
from app.llm.service import chat
from app.routers.companies import get_company

router = APIRouter()


class ChatRequest(StrictBody):
    message: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=3000)]


    run_id: int | None = Field(default=None, gt=0)
    remote: bool = True


class ChatResult(BaseModel):
    reply_ar: str
    intent: Intent
    scenario: Scenario
    action: Literal['whatif', 'explain', 'query', 'optimize', 'none']
    run_id: int | None
    kpis: dict[str, JsonValue] | None
    provider: str
    evidence: list[dict[str, JsonValue]] = Field(default_factory=list)
    diagnosis: dict[str, JsonValue] | None = None
    constraint_proposal: dict[str, JsonValue] | None = None


class HistoryItem(BaseModel):
    role: Literal['user', 'assistant']
    content: str
    ts: str
    evidence: list[dict[str, JsonValue]] = Field(default_factory=list)


@router.post('/companies/{cid}/chat', response_model=ChatResult)
def send_message(cid: int, body: ChatRequest, request: Request, connection=Depends(request_conn)):
    return chat(connection, get_company(connection, cid), body.message, request.app.state.settings,
                selected_run_id=body.run_id, remote=body.remote)


@router.get('/companies/{cid}/chat/history', response_model=list[HistoryItem])
def history(cid: int, connection=Depends(request_conn)):
    get_company(connection, cid)
    rows = connection.execute(
        'SELECT role, content, ts, scenario_json FROM chat_messages WHERE company_id = ? ORDER BY id', (cid,),
    )
    return [{**dict(row), 'evidence': json.loads(row['scenario_json'] or '{}').get('evidence', [])} for row in rows]
