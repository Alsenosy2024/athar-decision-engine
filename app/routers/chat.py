from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, JsonValue, StringConstraints

from app.db import request_conn
from app.llm.schema import Intent, Scenario, StrictBody
from app.llm.service import chat
from app.routers.companies import get_company

router = APIRouter()


class ChatRequest(StrictBody):
    message: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=3000)]


class ChatResult(BaseModel):
    reply_ar: str
    intent: Intent
    scenario: Scenario
    action: Literal['whatif', 'explain', 'query', 'optimize', 'none']
    run_id: int | None
    kpis: dict[str, JsonValue] | None
    provider: str


class HistoryItem(BaseModel):
    role: Literal['user', 'assistant']
    content: str
    ts: str


@router.post('/companies/{cid}/chat', response_model=ChatResult)
def send_message(cid: int, body: ChatRequest, request: Request, connection=Depends(request_conn)):
    return chat(connection, get_company(connection, cid), body.message, request.app.state.settings)


@router.get('/companies/{cid}/chat/history', response_model=list[HistoryItem])
def history(cid: int, connection=Depends(request_conn)):
    get_company(connection, cid)
    return [dict(row) for row in connection.execute(
        'SELECT role, content, ts FROM chat_messages WHERE company_id = ? ORDER BY id', (cid,),
    )]
