"""Read-only results from the fixed public demo fixture, under AI Lab permissions."""
from threading import Lock

from fastapi import APIRouter, Depends

from app.db import request_conn
from app.decision_loop.benchmark import run_benchmark
from app.routers.companies import get_company
from app.schemas import api_error

router = APIRouter(prefix='/companies/{cid}/ai', tags=['ai'])
_lock = Lock()
_demo_report = None


@router.post('/decision-benchmark')
def benchmark(cid: int, detail: bool = False, connection=Depends(request_conn)):
    company = get_company(connection, cid, allow_enterprise=True)
    if company['template'] != 'production':
        raise api_error(422, 'production_benchmark_only', 'المقارنة متاحة في مساحة الإنتاج.')
    global _demo_report
    with _lock:
        result = _demo_report
        if result is None:
            result = run_benchmark()
            replay = result['replay']
            if (replay['training_matched'] == replay['training_windows']
                    and replay['evaluation_matched'] == replay['evaluation_windows']
                    and replay['coefficients_matched']):
                _demo_report = result
    if detail:
        return result
    return {key: value for key, value in result.items() if key != 'windows'}
