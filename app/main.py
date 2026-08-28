from fastapi import FastAPI

from app.api.health import router as health_router
from app.core.config import Settings, get_settings
from app.db.session import build_engine, build_session_factory


def create_app(settings: Settings | None = None) -> FastAPI:
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
    engine = build_engine(settings.database_url)
    app.state.session_factory = build_session_factory(engine)

    app.include_router(health_router)

    return app


app = create_app()
