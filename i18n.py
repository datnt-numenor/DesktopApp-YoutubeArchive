"""Vietnamese/English UI text.

English strings are the message keys (gettext style); VI maps them to Vietnamese. The chosen
language is an app-wide preference because the desktop app has a single local owner, so API
messages can be translated without threading the request through every helper.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path

from config import LOCAL_DATA_DIR
from i18n_catalog import JS_KEYS, VI

logger = logging.getLogger(__name__)

LANGUAGES = ("vi", "en")
DEFAULT_LANGUAGE = "vi"
PREFERENCES_PATH = LOCAL_DATA_DIR / "preferences.json"
WEEKDAYS = {
    "vi": ("Thứ Hai", "Thứ Ba", "Thứ Tư", "Thứ Năm", "Thứ Sáu", "Thứ Bảy", "Chủ Nhật"),
    "en": ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"),
}

_lock = threading.Lock()
_cached_language: tuple[float | None, str] | None = None


def _read_preferences(path: Path) -> dict[str, object]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        logger.warning("Ignoring unreadable preferences file %s", path, exc_info=True)
        return {}
    return data if isinstance(data, dict) else {}


def current_language(path: Path | None = None) -> str:
    """Return the saved UI language, re-reading the file only when it changes."""
    global _cached_language
    path = path or PREFERENCES_PATH
    try:
        mtime: float | None = path.stat().st_mtime
    except OSError:
        mtime = None
    with _lock:
        if _cached_language and _cached_language[0] == mtime:
            return _cached_language[1]
        language = _read_preferences(path).get("language") if mtime is not None else None
        language = language if language in LANGUAGES else DEFAULT_LANGUAGE
        _cached_language = (mtime, language)
        return language


def set_language(language: str, path: Path | None = None) -> str:
    global _cached_language
    if language not in LANGUAGES:
        raise ValueError(f"Unsupported language: {language}")
    path = path or PREFERENCES_PATH
    with _lock:
        preferences = _read_preferences(path)
        preferences["language"] = language
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(preferences, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)
        _cached_language = None
    return language


def translate(language: str, message: str, **values: object) -> str:
    text = VI.get(message, message) if language == "vi" else message
    return text.format(**values) if values else text


def tr(message: str, **values: object) -> str:
    """Translate into the saved language; used for API messages and server-side errors."""
    return translate(current_language(), message, **values)


def js_messages(language: str) -> dict[str, str]:
    """Strings app.js needs; English needs none because keys are the English text."""
    return {key: value for key, value in VI.items() if key in JS_KEYS} if language == "vi" else {}

