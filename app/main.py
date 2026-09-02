from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx2
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.auth import router as auth_router
from app.api.buildings import router as buildings_router
from app.api.checkins import router as checkins_router
from app.api.checkouts import router as checkouts_router
from app.api.contractor_companies import router as contractor_companies_router
from app.api.events import router as events_router
from app.api.evidence import router as evidence_router
from app.api.executions import router as executions_router
from app.api.field_workers import router as field_workers_router
from app.api.floors import router as floors_router
from app.api.health import router as health_router
from app.api.qr_codes import router as qr_codes_router
from app.api.route_templates import router as route_templates_router
from app.api.routes import router as routes_router
from app.api.service_points import router as service_points_router
from app.api.service_types import router as service_types_router
from app.api.users import router as users_router
from app.core.config import Settings, get_settings
from app.core.mail import Mailer, SmtpMailer
from app.core.object_store import MinioObjectStore, ObjectStore
from app.db.session import build_engine, build_session_factory
from app.domain.routing.osrm import HttpxOsrmClient, OsrmClient


def _add_cors_middleware(app: FastAPI, settings: Settings) -> None:
    """Allow the dashboard's origin to call this API from a real browser.

    Browsers block cross-origin fetch() unless the server opts in explicitly; the
    dashboard (localhost:5173) calls this API (localhost:8000) with a Bearer token in
    the Authorization header, which triggers a CORS preflight (OPTIONS) that FastAPI
    rejects with no middleware at all. allow_credentials=True is needed for the
    Authorization header to survive fetch's CORS mode, which forces allow_origins to
    be an explicit origin (never "*" — the two together are both insecure and rejected
    outright by browsers).
    """
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[settings.dashboard_base_url],
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
    )


def create_app(
    settings: Settings | None = None,
    mailer: Mailer | None = None,
    osrm_client: OsrmClient | None = None,
    object_store: ObjectStore | None = None,
) -> FastAPI:
    """Build the application.

    A factory, not a module-level singleton: tests build a fresh app per case, pointing
    `settings.database_url` at a testcontainer instead of the real DATABASE_URL.

        app = create_app()
        app = create_app(settings=Settings(database_url=test_url, ...))
    """
    settings = settings or get_settings()

    _httpx_client = httpx2.AsyncClient(timeout=10.0)
    resolved_osrm = osrm_client or HttpxOsrmClient(settings.osrm_base_url, _httpx_client)
    resolved_store = object_store or MinioObjectStore(
        endpoint=settings.minio_endpoint,
        access_key=settings.minio_access_key,
        secret_key=settings.minio_secret_key,
        bucket=settings.minio_bucket_evidence,
        secure=settings.minio_secure,
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        # ensure_bucket() only exists on the real MinIO seam; a FakeObjectStore in tests must
        # never reach the network (spec §8). The isinstance check is against the concrete
        # class, not the Protocol, so it needs no @runtime_checkable.
        if isinstance(resolved_store, MinioObjectStore):
            await resolved_store.ensure_bucket()
        yield
        # Only close the client we own. An injected osrm_client brings its own transport.
        if osrm_client is None:
            await _httpx_client.aclose()

    app = FastAPI(
        title="UFCG Service Route Monitoring API",
        version="0.3.0",
        lifespan=lifespan,
    )
    app.state.settings = settings
    _add_cors_middleware(app, settings)
    engine = build_engine(settings.database_url)
    app.state.session_factory = build_session_factory(engine)
    app.state.mailer = mailer or SmtpMailer(
        host=settings.mail_smtp_host,
        port=settings.mail_smtp_port,
        from_address=settings.mail_from_address,
    )
    app.state.osrm_client = resolved_osrm
    app.state.object_store = resolved_store

    app.include_router(health_router)
    app.include_router(auth_router)
    app.include_router(routes_router)
    app.include_router(route_templates_router)
    app.include_router(checkins_router)
    app.include_router(checkouts_router)
    app.include_router(evidence_router)
    app.include_router(executions_router)
    app.include_router(users_router)
    app.include_router(service_types_router)
    app.include_router(contractor_companies_router)
    app.include_router(field_workers_router)
    app.include_router(buildings_router)
    app.include_router(floors_router)
    app.include_router(events_router)
    app.include_router(service_points_router)
    app.include_router(qr_codes_router)

    return app


app = create_app()
