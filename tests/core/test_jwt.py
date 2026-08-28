import uuid

import jwt
import pytest
from app.core.jwt import create_access_token, create_refresh_token, decode_token
from app.domain.identity.models import UserRole

_SECRET = "test-secret"


def test_create_access_token_round_trips_claims() -> None:
    user_id = uuid.uuid4()

    token = create_access_token(
        user_id=user_id, role=UserRole.FIELD_WORKER, secret=_SECRET, minutes=15
    )
    payload = decode_token(token, secret=_SECRET)

    assert payload["sub"] == str(user_id)
    assert payload["role"] == UserRole.FIELD_WORKER.value
    assert payload["type"] == "access"


def test_refresh_token_is_typed_differently_from_access_token() -> None:
    user_id = uuid.uuid4()

    token = create_refresh_token(user_id=user_id, secret=_SECRET, days=7)
    payload = decode_token(token, secret=_SECRET)

    assert payload["type"] == "refresh"
    assert "role" not in payload  # refresh carries no role — role comes from the database on use


def test_decode_token_rejects_a_token_signed_with_a_different_secret() -> None:
    token = create_access_token(
        user_id=uuid.uuid4(), role=UserRole.MANAGER, secret=_SECRET, minutes=15
    )

    with pytest.raises(jwt.InvalidTokenError):
        decode_token(token, secret="a-different-secret")
