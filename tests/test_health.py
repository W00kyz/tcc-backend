from app.main import create_app
from fastapi.testclient import TestClient

from tests.support.object_store import FakeObjectStore


def test_health_reports_ok() -> None:
    client = TestClient(create_app(object_store=FakeObjectStore()))

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
