from fastapi import FastAPI

from app.api.health import router as health_router


def create_app() -> FastAPI:
    """Build the application.

    A factory, not a module-level singleton: tests build a fresh app per case and
    later stages inject settings without importing side effects.

        app = create_app()
    """
    app = FastAPI(
        title="UFCG Service Route Monitoring API",
        version="0.1.0",
    )
    app.include_router(health_router)

    return app


app = create_app()
