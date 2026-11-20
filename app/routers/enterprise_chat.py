from fastapi import APIRouter, Depends, Request
from pydantic import Field, model_validator

from app.db import request_conn
from app.enterprise.impact_graph import WhatIfRequest
from app.llm.enterprise import HELP, grounded_explanation
from app.llm.provider import get_enterprise_provider
from app.routers.chat import ChatRequest
from app.routers.enterprise import impact_errors, impact_model, whatif_snapshot

router = APIRouter(prefix='/enterprise', tags=['enterprise'])


class EnterpriseChatRequest(ChatRequest):
    model_id: int | None = Field(default=None, ge=1)
    version: int | None = Field(default=None, ge=1)
    parse_only: bool = False

    @model_validator(mode='after')
    def valid_selection(self):
        if self.version is not None and self.model_id is None:
            raise ValueError('Version requires a saved model ID')
        return self


@router.post('/chat')
def enterprise_chat(body: EnterpriseChatRequest, request: Request, connection=Depends(request_conn)):
    snapshot = whatif_snapshot(connection, body.model_id, body.version)
    provider = get_enterprise_provider(request.app.state.settings)
    defaults = WhatIfRequest.model_fields
    with impact_errors():
        model = impact_model(snapshot, request, defaults['weeks'].default)
        scenario = provider.parse_enterprise(body.message, model)
        result = None
        if scenario.intent == 'what_if' and not body.parse_only:
            result = model.scenario(WhatIfRequest(assumptions=scenario.assumptions))
    reply = HELP
    if body.parse_only and scenario.intent == 'what_if':
        reply = 'راجع الفرضيات المقترحة وأكّدها عشان تحسب الأثر.'
    if result is not None:
        grounded = grounded_explanation(result)
        reply = provider.explain({'grounded_ar': grounded}, grounded)
    return {'reply_ar': reply, 'scenario': scenario, 'result': result,
            'model_id': snapshot['id'], 'version': snapshot['version'], 'name': snapshot['name'],
            'provider': provider.name}
