import os
import secrets
from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


BASE_DIR = Path(__file__).resolve().parent
# Keep the desktop edition completely separate from the web project's database
# and downloaded media. The directory can still be moved with this environment
# variable when the user wants to place a large archive on another drive.
LOCAL_DATA_DIR = Path(os.environ.get("YTARCHIVE_DESKTOP_DATA_DIR", BASE_DIR / "data"))
DEFAULT_SQLITE_URL = f"sqlite+aiosqlite:///{(LOCAL_DATA_DIR / 'ytarchive.db').as_posix()}"
ENV_FILE = Path(os.environ.get("ENV_FILE", BASE_DIR / ".env"))


SECRET_KEY_PATH = LOCAL_DATA_DIR / "secret.key"
# Values shipped in source or .env.example; anyone can read them, so they must never sign cookies.
PUBLIC_PLACEHOLDER_SECRET_KEYS = {
    "change-me-in-production-min-32-chars",
    "replace-with-any-long-random-local-secret",
}


def load_or_create_local_secret(path: Path = SECRET_KEY_PATH) -> str:
    """Return a per-installation signing key, creating it once and sharing it across processes."""
    try:
        existing = path.read_text(encoding="utf-8").strip()
        if len(existing) >= 32:
            return existing
    except OSError:
        pass

    path.parent.mkdir(parents=True, exist_ok=True)
    secret = secrets.token_urlsafe(48)
    try:
        # O_EXCL: if the app and the scheduled sync start together, only one key is written.
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        existing = path.read_text(encoding="utf-8").strip()
        if len(existing) >= 32:
            return existing
        path.write_text(secret, encoding="utf-8")
        return secret
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(secret)
    return secret


def sqlite_path_from_url(database_url: str) -> Path | None:
    prefix = "sqlite+aiosqlite:///"
    if not database_url.startswith(prefix):
        return None
    return Path(database_url.removeprefix(prefix))


class Settings(BaseSettings):
    app_name: str = "Desktop YouTube Archive"
    secret_key: str = "change-me-in-production-min-32-chars"
    auth_cookie_name: str = "ytarchive_session"
    auth_cookie_max_age_seconds: int = 60 * 60 * 24 * 7
    # The desktop server only listens on http://127.0.0.1, so Secure cookies would never be sent.
    auth_cookie_secure: bool = False
    csrf_cookie_name: str = "ytarchive_csrf"
    csrf_header_name: str = "x-csrf-token"
    login_max_attempts: int = Field(default=5, ge=1)
    login_lockout_minutes: int = Field(default=15, ge=1)

    database_url: str = DEFAULT_SQLITE_URL

    downloads_dir: Path = LOCAL_DATA_DIR / "downloads"
    thumbnails_dir: Path = LOCAL_DATA_DIR / "thumbnails"
    default_user_email: str = "local@example.com"
    default_playlist_quota: int = 50
    default_storage_quota_gb: int = 20

    sync_interval_hours: int = Field(default=12, ge=1, le=168)
    yt_sleep_min_seconds: int = Field(default=5, ge=0)
    yt_sleep_max_seconds: int = Field(default=15, ge=0)

    model_config = SettingsConfigDict(
        env_file=ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @property
    def storage_quota_bytes(self) -> int:
        return self.default_storage_quota_gb * 1024 * 1024 * 1024


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    if settings.secret_key in PUBLIC_PLACEHOLDER_SECRET_KEYS:
        settings.secret_key = load_or_create_local_secret()
    if sqlite_path := sqlite_path_from_url(settings.database_url):
        sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    settings.downloads_dir.mkdir(parents=True, exist_ok=True)
    settings.thumbnails_dir.mkdir(parents=True, exist_ok=True)
    return settings


settings = get_settings()
