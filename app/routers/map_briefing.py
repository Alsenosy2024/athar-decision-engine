"""Read-only briefing for the model version displayed on the map."""
import numpy as np
from fastapi import APIRouter, Depends, Request, Response

from app.db import request_conn
from app.enterprise.impact_graph import ImpactError
from app.enterprise.map_briefing import briefing, monitor_snapshot
from app.intelligence.reviews import observed_history
from app.optimization.common import OptimizationInputError
from app.prescriptive.monitor import Thresholds
from app.routers.enterprise import whatif_snapshot
from app.routers.living_map import Selection
from app.routers.prescriptive import engine, thresholds
from app.schemas import api_error

router = APIRouter(prefix='/enterprise/map', tags=['enterprise-map'])


@router.get('/briefing')
def read_briefing(request: Request, response: Response, model_id: Selection = None,
                  version: Selection = None, connection=Depends(request_conn)):
    if version is not None and model_id is None:
        raise api_error(422, 'version_requires_model', 'اختار نموذج محفوظ عشان تحدد الإصدار.')
    with connection:
        connection.execute('BEGIN')
        snapshot = whatif_snapshot(connection, model_id, version)
        graph = snapshot['graph']
        historical = model_id is not None and connection.execute(
            'SELECT MAX(version) FROM enterprise_model_versions WHERE model_id = ?', (model_id,)
        ).fetchone()[0] != snapshot['version']
        # Current alert settings apply only to the current version.
        config = thresholds(connection, model_id) if model_id is not None and not historical else Thresholds()
        observed = (observed_history(connection, model_id, graph, empty_as_missing=True)[0]
                    if model_id is not None else graph)
    with np.errstate(over='raise', invalid='raise', divide='raise'):
        try:
            monitored = monitor_snapshot(engine(snapshot, request), config)
        except (ImpactError, OptimizationInputError, FloatingPointError, OverflowError):
            monitored = {'kpis': {}, 'alerts': [], 'unavailable': [],
                         'warning': 'تعذّر حساب مؤشرات خط الأساس للإصدار ده.'}
        result = briefing(request.app.state.settings, graph, observed, monitored, historical=historical)
    response.headers['Cache-Control'] = 'no-store'
    return {'model_id': model_id, 'version': snapshot['version'], **result}
