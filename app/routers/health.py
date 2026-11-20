import ortools
from fastapi import APIRouter, Request

from app.llm.provider import get_provider
from app.schemas import Health

router = APIRouter()


@router.get('/health', response_model=Health)
def health(request: Request):
    provider = get_provider(request.app.state.settings)
    return {'status': 'ok', 'version': '0.1.0', 'solver': f'ortools {ortools.__version__}', 'provider': provider.name}
