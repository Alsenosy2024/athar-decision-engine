from contextlib import asynccontextmanager
import os

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.access import protected, request_principal, require_role, seed_admin
from app.config import STATIC_DIR, Settings
from app.db import get_conn, initialize
from app.decision_loop import routes as decision_benchmark
from app.department_owners import workflow as department_workflow
from app.enterprise.operating_source import source_runtime
from app.forecasting.foundation import TimesFMWorker
from app.ingestion.validators import validate_demo
from app.integrations.runtime import integration_runtime
from app.intelligence import reviews
from app.intelligence.forecasting import ChronosWorker
from app.onboarding.storage import initialize as initialize_onboarding
from app.prescriptive import records as prescriptive_records
from app.process_mining import store as process_store
from app.routers import embed_map, integration_keys, public_v1
from app.routers import (
    actuals, auth, chat, companies, connectors, data, enterprise, enterprise_chat,
    forecast, health, intelligence, living_map, map_briefing, model, onboarding, optimize, plans, prescriptive, process_mining, whatif,
)
from app.routers import department_owners
from app.templates_registry import load_templates

SEED_COMPANIES = (
    ('شركة البناء النموذجية', 'contracting'),
    ('مصنع الأغذية المبردة', 'production'),
)


def seed(path):
    templates = load_templates()
    with get_conn(path) as connection:
        connection.execute('BEGIN IMMEDIATE')
        if connection.execute('SELECT 1 FROM companies LIMIT 1').fetchone():
            return
        for name, template_id in SEED_COMPANIES:
            template = templates[template_id]
            cid = companies.insert_company(connection, name, template)
            payload, validation = validate_demo(template)
            if not validation.ok:
                raise ValueError(f'Invalid demo dataset for {template_id}: {validation.errors}')
            data.save_dataset(connection, cid, 'البيانات التجريبية', 'demo', payload, validation)


def create_app(settings: Settings | None = None):
    settings = settings or Settings()
    forecast_worker = TimesFMWorker() if settings.forecast_engine != 'stat' else None

    chronos_worker = ChronosWorker() if settings.forecast_engine == 'auto' else None

    @asynccontextmanager
    async def lifespan(application):
        initialize(settings.db_path)
        initialize_onboarding(settings.db_path)
        department_workflow.initialize(settings.db_path)
        prescriptive_records.initialize(settings.db_path)
        reviews.initialize(settings.db_path)
        process_store.initialize(settings.db_path)
        seed(settings.db_path)
        seed_admin(settings.db_path)
        try:
            async with integration_runtime(application, settings), source_runtime(settings.db_path):
                yield
        finally:
            if forecast_worker is not None:
                forecast_worker.close()
            if chronos_worker is not None:
                chronos_worker.close()

    application = FastAPI(title='محرك القرارات', version='0.1.0', lifespan=lifespan,
                          docs_url=None, redoc_url=None)
    application.state.settings = settings
    application.state.forecast_worker = forecast_worker
    application.state.chronos_worker = chronos_worker
    application.include_router(intelligence.page)
    application.include_router(department_owners.page)
    application.include_router(department_owners.router, prefix='/api', dependencies=[Depends(protected)])
    application.include_router(health.router, prefix='/api')
    application.include_router(auth.public, prefix='/api')
    application.include_router(public_v1.router)
    application.include_router(public_v1.docs)
    application.include_router(embed_map.router)
    application.include_router(integration_keys.router, prefix='/api', dependencies=[Depends(protected)])
    for router in (companies.router, data.router, model.router, optimize.router,
                   forecast.router, whatif.router, chat.router, plans.router, actuals.router, enterprise.router,
                   connectors.router, enterprise_chat.router, auth.router, onboarding.router,
                   prescriptive.router, intelligence.router, living_map.router, map_briefing.router,
                   process_mining.router, decision_benchmark.router):
        application.include_router(router, prefix='/api', dependencies=[Depends(protected)])

    @application.exception_handler(RequestValidationError)
    async def validation_error(request: Request, error: RequestValidationError):
        return JSONResponse(status_code=422, content={
            'detail': {'msg_ar': 'الطلب غير صالح. راجع الحقول المطلوبة وصيغة القيم.', 'code': 'invalid_request'},
        })

    @application.get('/', include_in_schema=False)
    def index(request: Request):
        principal = request_principal(request)
        if principal is None:
            return RedirectResponse('/login', headers={'Cache-Control': 'no-store'})
        if principal.department_scoped:
            return RedirectResponse('/department-owners', headers={'Cache-Control': 'no-store'})
        return FileResponse(STATIC_DIR / 'index.html', headers={'Cache-Control': 'no-store'})

    @application.get('/login', include_in_schema=False)
    def login_page(request: Request):
        if request_principal(request) is not None:
            return RedirectResponse('/', headers={'Cache-Control': 'no-store'})
        return FileResponse(STATIC_DIR / 'login.html', headers={'Cache-Control': 'no-store'})

    @application.get('/users', include_in_schema=False)
    def users_page(request: Request):
        principal = request_principal(request)
        if principal is None:
            return RedirectResponse('/login', headers={'Cache-Control': 'no-store'})
        require_role(principal)
        return FileResponse(STATIC_DIR / 'users.html', headers={'Cache-Control': 'no-store'})

    application.mount('/static', StaticFiles(directory=STATIC_DIR), name='static')
    return application


app = create_app(Settings(llm_provider=os.getenv('LLM_PROVIDER', 'rules')))
