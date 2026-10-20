import pytest

import auth
from models import User


@pytest.fixture(autouse=True)
def reset_auth_settings(monkeypatch) -> None:
    monkeypatch.setattr(auth.settings, "login_max_attempts", 5)
    monkeypatch.setattr(auth.settings, "login_lockout_minutes", 15)


def test_password_hash_verification() -> None:
    stored_hash = auth.hash_password("secret-password")

    assert stored_hash != "secret-password"
    assert auth.verify_password("secret-password", stored_hash)
    assert not auth.verify_password("wrong-password", stored_hash)
    assert not auth.verify_password("secret-password", auth.LOCAL_DEV_PASSWORD_MARKER)


def test_access_token_round_trip() -> None:
    user = User(id="user-1", email="owner@example.com")

    token = auth.create_access_token(user)
    payload = auth.decode_access_token(token)
    tampered = token[:-1] + ("a" if token[-1] != "a" else "b")

    assert payload is not None
    assert payload["sub"] == "user-1"
    assert payload["email"] == "owner@example.com"
    assert auth.decode_access_token(tampered) is None


def test_csrf_token_verification() -> None:
    token = auth.create_csrf_token()
    tampered = token[:-1] + ("a" if token[-1] != "a" else "b")

    assert auth.verify_csrf_token(token)
    assert not auth.verify_csrf_token(tampered)
    assert not auth.verify_csrf_token("")


async def test_login_lockout_after_repeated_failures(session, monkeypatch) -> None:
    monkeypatch.setattr(auth.settings, "login_max_attempts", 2)
    user = await auth.set_initial_desktop_password(session, "password123")

    assert await auth.authenticate_desktop_user(session, "wrong-password") is None
    assert await auth.authenticate_desktop_user(session, "wrong-password") is None
    locked = await auth.authenticate_desktop_user(session, "password123")

    await session.refresh(user)

    assert locked is None
    assert user.locked_until is not None


class _CookieRequest:
    def __init__(self, token: str) -> None:
        self.cookies = {auth.settings.auth_cookie_name: token}


async def test_revoked_sessions_are_rejected(session) -> None:
    user = await auth.set_initial_desktop_password(session, "password123")
    token = auth.create_access_token(user)
    assert await auth.get_optional_user(_CookieRequest(token), session) is not None

    await auth.revoke_sessions(session, user)

    assert await auth.get_optional_user(_CookieRequest(token), session) is None
    fresh = auth.create_access_token(user)
    assert await auth.get_optional_user(_CookieRequest(fresh), session) is not None


async def test_password_change_signs_out_existing_sessions(session) -> None:
    user = await auth.set_initial_desktop_password(session, "password123")
    token = auth.create_access_token(user)

    await auth.change_desktop_password(session, user, "password123", "new-password123")

    assert await auth.get_optional_user(_CookieRequest(token), session) is None


async def test_login_upgrades_hashes_made_with_fewer_iterations(session, monkeypatch) -> None:
    monkeypatch.setattr(auth, "PASSWORD_ITERATIONS", 1_000)
    user = await auth.set_initial_desktop_password(session, "password123")
    assert user.hashed_password.split("$")[1] == "1000"

    monkeypatch.setattr(auth, "PASSWORD_ITERATIONS", 2_000)
    assert auth.password_needs_rehash(user.hashed_password)
    authenticated = await auth.authenticate_desktop_user(session, "password123")

    assert authenticated is not None
    assert authenticated.hashed_password.split("$")[1] == "2000"
    assert not auth.password_needs_rehash(authenticated.hashed_password)
    assert await auth.authenticate_desktop_user(session, "password123") is not None


def test_password_hashes_use_owasp_iteration_count() -> None:
    assert auth.PASSWORD_ITERATIONS >= 600_000
