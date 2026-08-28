from app.core.security import hash_password, verify_password


def test_verify_password_accepts_the_original_password() -> None:
    password_hash = hash_password("correct horse battery staple")

    assert verify_password("correct horse battery staple", password_hash) is True


def test_verify_password_rejects_a_wrong_password() -> None:
    password_hash = hash_password("correct horse battery staple")

    assert verify_password("wrong password", password_hash) is False


def test_hash_password_never_returns_the_plain_text() -> None:
    password_hash = hash_password("correct horse battery staple")

    assert "correct horse battery staple" not in password_hash
