"""CORS middleware regression test: the dashboard (localhost:5173) must be allowed to
call this API cross-origin with a Bearer token, or the browser's preflight rejects the
request before it is ever sent. See app/main.py's create_app() for the middleware setup.
"""

from app.main import create_app
from fastapi.testclient import TestClient

_DASHBOARD_ORIGIN = "http://localhost:5173"


def test_actual_request_echoes_dashboard_origin_with_credentials() -> None:
    client = TestClient(create_app())

    response = client.get("/health", headers={"Origin": _DASHBOARD_ORIGIN})

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == _DASHBOARD_ORIGIN
    assert response.headers["access-control-allow-credentials"] == "true"


def test_preflight_allows_bearer_auth_header_and_post_method() -> None:
    client = TestClient(create_app())

    response = client.options(
        "/checkins",
        headers={
            "Origin": _DASHBOARD_ORIGIN,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "Authorization,Content-Type",
        },
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == _DASHBOARD_ORIGIN
    assert "POST" in response.headers["access-control-allow-methods"]
    allowed_headers = response.headers["access-control-allow-headers"].lower()
    assert "authorization" in allowed_headers
    assert "content-type" in allowed_headers


def test_preflight_rejects_an_unrecognized_origin() -> None:
    client = TestClient(create_app())

    response = client.options(
        "/checkins",
        headers={
            "Origin": "http://evil.example.com",
            "Access-Control-Request-Method": "POST",
        },
    )

    # Starlette's CORSMiddleware still returns 200 for a disallowed origin's preflight,
    # but omits Access-Control-Allow-Origin — the browser is what refuses to proceed.
    assert "access-control-allow-origin" not in response.headers
