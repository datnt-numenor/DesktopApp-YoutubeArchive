from pathlib import Path

import auth
import pytest
from fastapi import HTTPException
from models import User


async def test_first_run_sets_one_local_password(session) -> None:
    assert not await auth.desktop_password_is_configured(session)

    user = await auth.set_initial_desktop_password(session, "password123")

    assert user.email == auth.settings.default_user_email
    assert user.is_active
    assert user.is_verified
    assert user.is_superuser
    assert await auth.desktop_password_is_configured(session)
    assert await auth.authenticate_desktop_user(session, "password123") is not None
    assert await auth.authenticate_desktop_user(session, "wrong-password") is None


async def test_existing_email_account_can_unlock_with_password_only(session) -> None:
    existing = User(
        email="previous@example.com",
        hashed_password=auth.hash_password("existing-password"),
        is_active=True,
        is_verified=True,
        is_superuser=True,
    )
    session.add(existing)
    await session.commit()

    authenticated = await auth.authenticate_desktop_user(session, "existing-password")

    assert authenticated is not None
    assert authenticated.email == "previous@example.com"


def test_login_template_has_no_email_or_account_recovery_links() -> None:
    template = (Path(__file__).resolve().parents[1] / "templates" / "login.html").read_text(encoding="utf-8")

    assert 'name="email"' not in template
    assert "/register" not in template
    assert "/forgot-password" not in template
    assert 'name="password"' in template


async def test_change_desktop_password_requires_current_password(session) -> None:
    user = await auth.set_initial_desktop_password(session, "password123")

    with pytest.raises(HTTPException, match="Current password is incorrect"):
        await auth.change_desktop_password(session, user, "wrong-password", "new-password123")

    assert await auth.authenticate_desktop_user(session, "password123") is not None


async def test_change_desktop_password_replaces_password(session) -> None:
    user = await auth.set_initial_desktop_password(session, "password123")

    await auth.change_desktop_password(session, user, "password123", "new-password123")

    assert await auth.authenticate_desktop_user(session, "password123") is None
    assert await auth.authenticate_desktop_user(session, "new-password123") is not None


def test_settings_template_contains_change_password_form() -> None:
    template = (Path(__file__).resolve().parents[1] / "templates" / "settings.html").read_text(encoding="utf-8")

    assert 'id="changePasswordForm"' in template
    assert 'name="current_password"' in template
    assert 'name="new_password"' in template
