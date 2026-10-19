import base64
import hashlib
import hmac
import json
import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import parse_qs

from fastapi import Depends, HTTPException, Request, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from database import get_session
from models import User

LOCAL_DEV_PASSWORD_MARKER = "local-dev-user"
PASSWORD_ALGORITHM = "pbkdf2_sha256"
PASSWORD_ITERATIONS = 260_000


def normalize_email(email: str) -> str:
    return email.strip().lower()


def _base64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _base64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def ensure_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    password_hash = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        PASSWORD_ITERATIONS,
    )
    return "$".join(
        [
            PASSWORD_ALGORITHM,
            str(PASSWORD_ITERATIONS),
            _base64url_encode(salt),
            _base64url_encode(password_hash),
        ]
    )


def verify_password(password: str, stored_hash: str) -> bool:
    if not stored_hash or stored_hash == LOCAL_DEV_PASSWORD_MARKER:
        return False

    try:
        algorithm, iterations_value, salt_value, expected_value = stored_hash.split("$", 3)
        iterations = int(iterations_value)
    except ValueError:
        return False

    if algorithm != PASSWORD_ALGORITHM:
        return False

    salt = _base64url_decode(salt_value)
    expected = _base64url_decode(expected_value)
    password_hash = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        iterations,
    )
    return hmac.compare_digest(password_hash, expected)


def create_csrf_token() -> str:
    nonce = secrets.token_urlsafe(24)
    signature = hmac.new(settings.secret_key.encode("utf-8"), nonce.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{nonce}.{signature}"


def verify_csrf_token(token: str | None) -> bool:
    if not token or "." not in token:
        return False
    nonce, signature = token.rsplit(".", 1)
    expected = hmac.new(settings.secret_key.encode("utf-8"), nonce.encode("ascii"), hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature, expected)


def csrf_token_for_request(request: Request) -> str:
    token = request.cookies.get(settings.csrf_cookie_name)
    if verify_csrf_token(token):
        return str(token)
    return create_csrf_token()


def set_csrf_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key=settings.csrf_cookie_name,
        value=token,
        max_age=settings.auth_cookie_max_age_seconds,
        httponly=False,
        secure=settings.auth_cookie_secure,
        samesite="lax",
    )


async def require_csrf(request: Request) -> None:
    cookie_token = request.cookies.get(settings.csrf_cookie_name)
    submitted_token = request.headers.get(settings.csrf_header_name)

    if not submitted_token and request.headers.get("content-type", "").startswith("application/x-www-form-urlencoded"):
        body = (await request.body()).decode("utf-8")
        parsed = parse_qs(body, keep_blank_values=True)
        submitted_token = (parsed.get("csrf_token") or [""])[-1]

    if (
        not cookie_token
        or not submitted_token
        or not hmac.compare_digest(cookie_token, submitted_token)
        or not verify_csrf_token(cookie_token)
    ):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid CSRF token")


def _jwt_signature(signing_input: str) -> str:
    digest = hmac.new(settings.secret_key.encode("utf-8"), signing_input.encode("ascii"), hashlib.sha256).digest()
    return _base64url_encode(digest)


def create_access_token(user: User) -> str:
    now = int(time.time())
    header = {"alg": "HS256", "typ": "JWT"}
    payload = {
        "sub": user.id,
        "email": user.email,
        "iat": now,
        "exp": now + settings.auth_cookie_max_age_seconds,
    }
    encoded_header = _base64url_encode(json.dumps(header, separators=(",", ":")).encode("utf-8"))
    encoded_payload = _base64url_encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    signing_input = f"{encoded_header}.{encoded_payload}"
    return f"{signing_input}.{_jwt_signature(signing_input)}"


def decode_access_token(token: str) -> dict[str, Any] | None:
    try:
        encoded_header, encoded_payload, signature = token.split(".", 2)
    except ValueError:
        return None

    signing_input = f"{encoded_header}.{encoded_payload}"
    if not hmac.compare_digest(_jwt_signature(signing_input), signature):
        return None

    try:
        payload = json.loads(_base64url_decode(encoded_payload))
    except (json.JSONDecodeError, ValueError):
        return None

    if int(payload.get("exp", 0)) < int(time.time()):
        return None
    return payload


def set_login_cookie(response: Response, user: User) -> None:
    response.set_cookie(
        key=settings.auth_cookie_name,
        value=create_access_token(user),
        max_age=settings.auth_cookie_max_age_seconds,
        httponly=True,
        secure=settings.auth_cookie_secure,
        samesite="lax",
    )


def clear_login_cookie(response: Response) -> None:
    response.delete_cookie(
        key=settings.auth_cookie_name,
        httponly=True,
        secure=settings.auth_cookie_secure,
        samesite="lax",
    )


def is_user_locked(user: User) -> bool:
    return bool(user.locked_until and ensure_aware(user.locked_until) > utc_now())


async def get_desktop_user(session: AsyncSession) -> User | None:
    """Return the local owner and preserve an existing password-backed account."""
    configured_result = await session.execute(
        select(User)
        .where(User.hashed_password.notin_([LOCAL_DEV_PASSWORD_MARKER, ""]))
        .order_by(User.created_at, User.id)
        .limit(1)
    )
    configured_user = configured_result.scalar_one_or_none()
    if configured_user:
        return configured_user

    preferred_result = await session.execute(
        select(User).where(User.email == normalize_email(settings.default_user_email))
    )
    preferred = preferred_result.scalar_one_or_none()
    if preferred:
        return preferred
    result = await session.execute(select(User).order_by(User.created_at, User.id).limit(1))
    return result.scalar_one_or_none()


async def desktop_password_is_configured(session: AsyncSession) -> bool:
    user = await get_desktop_user(session)
    return bool(user and user.hashed_password not in {"", LOCAL_DEV_PASSWORD_MARKER})


async def set_initial_desktop_password(session: AsyncSession, password: str) -> User:
    """Create or upgrade the only local owner during first-run setup."""
    if len(password) < 8:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Password must be at least 8 characters")

    user = await get_desktop_user(session)
    if user and user.hashed_password not in {"", LOCAL_DEV_PASSWORD_MARKER}:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Desktop password is already configured")

    if user is None:
        user = User(
            email=normalize_email(settings.default_user_email),
            hashed_password=hash_password(password),
            is_active=True,
            is_verified=True,
            is_superuser=True,
            playlist_quota=settings.default_playlist_quota,
            storage_quota_bytes=settings.storage_quota_bytes,
        )
        session.add(user)
    else:
        user.hashed_password = hash_password(password)
        user.is_active = True
        user.is_verified = True
        user.is_superuser = True

    await session.commit()
    await session.refresh(user)
    return user


async def authenticate_desktop_user(session: AsyncSession, password: str) -> User | None:
    """Authenticate the local owner without asking for an email or username."""
    user = await get_desktop_user(session)
    if not user:
        return None
    return await authenticate_user(session, user.email, password)


async def change_desktop_password(
    session: AsyncSession,
    user: User,
    current_password: str,
    new_password: str,
) -> None:
    """Change the local owner's password after verifying the existing one."""
    if not verify_password(current_password, user.hashed_password):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Current password is incorrect")
    if len(new_password) < 8:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="New password must be at least 8 characters")
    if verify_password(new_password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="New password must be different from the current password",
        )

    user.hashed_password = hash_password(new_password)
    user.failed_login_count = 0
    user.locked_until = None
    await session.commit()
    await session.refresh(user)


async def authenticate_user(session: AsyncSession, email: str, password: str) -> User | None:
    result = await session.execute(select(User).where(User.email == normalize_email(email)))
    user = result.scalar_one_or_none()
    if not user or not user.is_active:
        return None
    if is_user_locked(user):
        return None
    if not verify_password(password, user.hashed_password):
        user.failed_login_count += 1
        if user.failed_login_count >= settings.login_max_attempts:
            user.locked_until = utc_now() + timedelta(minutes=settings.login_lockout_minutes)
            user.failed_login_count = 0
        await session.commit()
        return None

    user.failed_login_count = 0
    user.locked_until = None
    await session.commit()
    await session.refresh(user)
    return user


async def get_optional_user(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> User | None:
    token = request.cookies.get(settings.auth_cookie_name)
    if not token:
        return None

    payload = decode_access_token(token)
    if not payload:
        return None

    result = await session.execute(select(User).where(User.id == payload.get("sub")))
    user = result.scalar_one_or_none()
    if not user or not user.is_active:
        return None
    return user


async def get_current_user(user: User | None = Depends(get_optional_user)) -> User:
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")
    return user

