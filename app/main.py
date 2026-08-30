from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.auth import router as auth_router
from app.api.checkins import router as checkins_router
from app.api.contractor_companies import router as contractor_companies_router
from app.api.field_workers import router as field_workers_router
from app.api.health import router as health_router
from app.api.routes import router as routes_router
from app.api.service_types import router as service_types_router
from app.api.users import router as users_router
from app.core.config import Settings, get_settings
from app.core.mail import Mailer, SmtpMailer
from app.db.session import build_engine, build_session_factory


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


def create_app(settings: Settings | None = None, mailer: Mailer | None = None) -> FastAPI:
    """Build the application.

    A factory, not a module-level singleton: tests build a fresh app per case, pointing
    `settings.database_url` at a testcontainer instead of the real DATABASE_URL.

        app = create_app()
        app = create_app(settings=Settings(database_url=test_url, ...))
    """
    settings = settings or get_settings()
    app = FastAPI(
        title="UFCG Service Route Monitoring API",
        version="0.2.0",
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

    app.include_router(health_router)
    app.include_router(auth_router)
    app.include_router(routes_router)
    app.include_router(checkins_router)
    app.include_router(users_router)
    app.include_router(service_types_router)
    app.include_router(contractor_companies_router)
    app.include_router(field_workers_router)

    return app


app = create_app()
