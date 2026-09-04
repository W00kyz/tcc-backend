"""RF43 — the manager execution-history API: a filterable paginated list, a detail endpoint
that bundles scans and evidence, and review resolution with an audit-trail entry."""

import uuid
from datetime import UTC, datetime, timedelta

from app.core.config import Settings
from app.domain.audit.models import AuditTrail
from app.domain.execution.models import Execution, ExecutionReviewStatus, ExecutionSource
from app.domain.identity.models import UserRole
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from tests.api.test_checkins import _add_user, _login, _post, _seed_route
from tests.api.test_checkouts import _post_check_out, _publish_form_for_stop
from tests.api.test_evidence import _PHOTO_BYTES, _PHOTO_SHA, _upload_photo

_MANAGER_EMAIL = "gerente@empresa.com"


def _manager_token(client: TestClient) -> str:
    return _login(client, email=_MANAGER_EMAIL)


async def _seed_extra_executions(
    db_session: AsyncSession, *, route_stop_id: uuid.UUID, worker_id: uuid.UUID, count: int
) -> None:
    for offset in range(count):
        db_session.add(
            Execution(
                route_stop_id=route_stop_id,
                field_worker_id=worker_id,
                checked_in_at=datetime.now(UTC) - timedelta(hours=offset + 1),
                synced_at=datetime.now(UTC),
                source=ExecutionSource.APP,
                idempotency_key=uuid.uuid4(),
            )
        )
    await db_session.commit()


async def test_list_executions_manager_paginated(
    client: TestClient, db_session: AsyncSession, test_settings: Settings
) -> None:
    seeded = await _seed_route(db_session, test_settings)
    await _add_user(db_session, _MANAGER_EMAIL, UserRole.MANAGER)
    worker_token = _login(client)
    checkin = _post(client, worker_token, seeded.qr_code.public_code)
    assert checkin.status_code == 201, checkin.text
    await _seed_extra_executions(
        db_session, route_stop_id=seeded.stops[0].id, worker_id=seeded.worker.id, count=2
    )

    response = client.get(
        "/executions",
        params={"page": 1, "page_size": 2},
        headers={"Authorization": f"Bearer {_manager_token(client)}"},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 3
    assert body["page"] == 1
    assert body["page_size"] == 2
    assert len(body["items"]) == 2
    checked_in = [item["checked_in_at"] for item in body["items"]]
    assert checked_in == sorted(checked_in, reverse=True)
    first = body["items"][0]
    assert first["field_worker_name"] == "João"
    assert first["building_name"] == "Bloco CI"


async def test_list_executions_page_size_clamped_to_200(
    client: TestClient, db_session: AsyncSession, test_settings: Settings
) -> None:
    await _seed_route(db_session, test_settings)
    await _add_user(db_session, _MANAGER_EMAIL, UserRole.MANAGER)

    response = client.get(
        "/executions",
        params={"page_size": 5000},
        headers={"Authorization": f"Bearer {_manager_token(client)}"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["page_size"] == 200


async def test_list_executions_filter_by_review_status(
    client: TestClient, db_session: AsyncSession, test_settings: Settings
) -> None:
    seeded = await _seed_route(db_session, test_settings)
    await _add_user(db_session, _MANAGER_EMAIL, UserRole.MANAGER)
    worker_token = _login(client)
    # An out-of-radius check-in is accepted but flagged PENDING_REVIEW (spec §4.2).
    flagged = _post(
        client, worker_token, seeded.qr_code.public_code, latitude=-7.3, longitude=-35.9
    )
    assert flagged.status_code == 201, flagged.text
    await _seed_extra_executions(
        db_session, route_stop_id=seeded.stops[0].id, worker_id=seeded.worker.id, count=1
    )

    pending = client.get(
        "/executions",
        params={"review_status": "PENDING_REVIEW"},
        headers={"Authorization": f"Bearer {_manager_token(client)}"},
    )

    assert pending.status_code == 200, pending.text
    body = pending.json()
    assert body["total"] == 1
    assert body["items"][0]["review_status"] == "PENDING_REVIEW"


async def test_list_executions_unknown_enum_filter_422(
    client: TestClient, db_session: AsyncSession, test_settings: Settings
) -> None:
    await _seed_route(db_session, test_settings)
    await _add_user(db_session, _MANAGER_EMAIL, UserRole.MANAGER)

    response = client.get(
        "/executions",
        params={"source": "NOT_A_SOURCE"},
        headers={"Authorization": f"Bearer {_manager_token(client)}"},
    )

    assert response.status_code == 422, response.text


async def test_get_execution_detail_includes_scans_and_evidence(
    client: TestClient, db_session: AsyncSession, test_settings: Settings
) -> None:
    seeded = await _seed_route(db_session, test_settings)
    await _add_user(db_session, _MANAGER_EMAIL, UserRole.MANAGER)
    worker_token = _login(client)
    checkin = _post(client, worker_token, seeded.qr_code.public_code)
    execution_id = checkin.json()["execution_id"]
    assert (
        _upload_photo(client, worker_token, execution_id, _PHOTO_BYTES, _PHOTO_SHA).status_code
        == 201
    )

    response = client.get(
        f"/executions/{execution_id}",
        headers={"Authorization": f"Bearer {_manager_token(client)}"},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["execution_id"] == execution_id
    assert len(body["scans"]) == 1
    assert body["scans"][0]["kind"] == "CHECK_IN"
    assert len(body["evidence"]) == 1
    assert body["evidence"][0]["kind"] == "PHOTO"
    assert body["manual_completion"] is None
    # No service-type form on this stop → no form section (Ruling 15).
    assert body["form_version_id"] is None
    assert body["answers"] == []


async def test_get_execution_detail_surfaces_form_answers(
    client: TestClient, db_session: AsyncSession, test_settings: Settings
) -> None:
    seeded = await _seed_route(db_session, test_settings)
    form = await _publish_form_for_stop(db_session, seeded)
    await _add_user(db_session, _MANAGER_EMAIL, UserRole.MANAGER)
    worker_token = _login(client)
    checkin = _post(client, worker_token, seeded.qr_code.public_code)
    execution_id = checkin.json()["execution_id"]

    check_out = _post_check_out(
        client,
        worker_token,
        seeded.qr_code.public_code,
        form_version_id=str(form.v1_id),
        answers=[
            {"stable_key": form.text_key, "value": "tudo certo"},
            {"stable_key": form.bool_key, "value": True},
        ],
    )
    assert check_out.status_code == 201, check_out.text

    response = client.get(
        f"/executions/{execution_id}",
        headers={"Authorization": f"Bearer {_manager_token(client)}"},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["form_version_id"] == str(form.v1_id)
    by_key = {a["question_stable_key"]: a for a in body["answers"]}
    assert by_key[form.text_key]["prompt"] == "Observações?"
    assert by_key[form.text_key]["value"] == "tudo certo"
    assert by_key[form.bool_key]["prompt"] == "Área limpa?"
    assert by_key[form.bool_key]["value"] is True


async def test_get_execution_detail_missing_404(
    client: TestClient, db_session: AsyncSession, test_settings: Settings
) -> None:
    await _seed_route(db_session, test_settings)
    await _add_user(db_session, _MANAGER_EMAIL, UserRole.MANAGER)

    response = client.get(
        f"/executions/{uuid.uuid4()}",
        headers={"Authorization": f"Bearer {_manager_token(client)}"},
    )

    assert response.status_code == 404, response.text


async def test_resolve_review_sets_resolved_and_audits(
    client: TestClient, db_session: AsyncSession, test_settings: Settings
) -> None:
    seeded = await _seed_route(db_session, test_settings)
    await _add_user(db_session, _MANAGER_EMAIL, UserRole.MANAGER)
    worker_token = _login(client)
    flagged = _post(
        client, worker_token, seeded.qr_code.public_code, latitude=-7.3, longitude=-35.9
    )
    execution_id = flagged.json()["execution_id"]

    response = client.post(
        f"/executions/{execution_id}/review/resolve",
        json={"note": "Verificado com o supervisor."},
        headers={"Authorization": f"Bearer {_manager_token(client)}"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["review_status"] == "RESOLVED"

    stored = await db_session.get(Execution, uuid.UUID(execution_id))
    assert stored is not None
    assert stored.review_status is ExecutionReviewStatus.RESOLVED

    audit_count = await db_session.scalar(
        select(func.count())
        .select_from(AuditTrail)
        .where(
            AuditTrail.entity_type == "execution",
            AuditTrail.action == "resolve_review",
            AuditTrail.entity_id == uuid.UUID(execution_id),
        )
    )
    assert audit_count == 1


async def test_resolve_review_when_none_409(
    client: TestClient, db_session: AsyncSession, test_settings: Settings
) -> None:
    seeded = await _seed_route(db_session, test_settings)
    await _add_user(db_session, _MANAGER_EMAIL, UserRole.MANAGER)
    worker_token = _login(client)
    clean = _post(client, worker_token, seeded.qr_code.public_code)
    execution_id = clean.json()["execution_id"]
    assert clean.json()["review_status"] == "NONE"

    response = client.post(
        f"/executions/{execution_id}/review/resolve",
        headers={"Authorization": f"Bearer {_manager_token(client)}"},
    )

    assert response.status_code == 409, response.text


async def test_resolve_review_missing_404(
    client: TestClient, db_session: AsyncSession, test_settings: Settings
) -> None:
    await _seed_route(db_session, test_settings)
    await _add_user(db_session, _MANAGER_EMAIL, UserRole.MANAGER)

    response = client.post(
        f"/executions/{uuid.uuid4()}/review/resolve",
        headers={"Authorization": f"Bearer {_manager_token(client)}"},
    )

    assert response.status_code == 404, response.text


async def test_field_worker_forbidden_on_history_403(
    client: TestClient, db_session: AsyncSession, test_settings: Settings
) -> None:
    await _seed_route(db_session, test_settings)
    worker_token = _login(client)

    listing = client.get("/executions", headers={"Authorization": f"Bearer {worker_token}"})
    detail = client.get(
        f"/executions/{uuid.uuid4()}", headers={"Authorization": f"Bearer {worker_token}"}
    )
    resolve = client.post(
        f"/executions/{uuid.uuid4()}/review/resolve",
        headers={"Authorization": f"Bearer {worker_token}"},
    )

    assert listing.status_code == 403, listing.text
    assert detail.status_code == 403, detail.text
    assert resolve.status_code == 403, resolve.text
