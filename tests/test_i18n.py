import re
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import i18n
import main as app_module
from i18n_catalog import JS_KEYS, VI
from models import User

ROOT = Path(__file__).resolve().parents[1]
LOCAL_BASE_URL = "http://127.0.0.1"


def _template_keys() -> set[str]:
    pattern = re.compile(r"""_\(\s*(?P<q>['"])(?P<text>(?:(?!(?P=q)).)*)(?P=q)""")
    keys = set()
    for path in (ROOT / "templates").glob("*.html"):
        keys.update(match.group("text") for match in pattern.finditer(path.read_text(encoding="utf-8")))
    return keys


def _script_keys() -> set[str]:
    script = (ROOT / "static" / "js" / "app.js").read_text(encoding="utf-8")
    keys = {match.group(1) for match in re.finditer(r"""\bt\(\s*"((?:[^"\\]|\\.)*)\"""", script)}
    # TASK_STATUS_LABELS values reach t() through a lookup.
    return keys | {"queued", "running", "done", "failed"}


def _server_keys() -> set[str]:
    keys = set()
    for name in ["main.py", "crud.py", "auth.py", "media_import.py", "storage.py"]:
        text = (ROOT / name).read_text(encoding="utf-8")
        keys.update(re.findall(r"""\btr\(\s*"((?:[^"\\]|\\.)*)\"""", text))
        keys.update(re.findall(r"""detail="((?:[^"\\]|\\.)*)\"""", text))
    return keys


def test_every_ui_message_has_a_vietnamese_translation() -> None:
    missing = (_template_keys() | _script_keys() | _server_keys()) - set(VI)
    assert not missing


def test_script_messages_are_exported_to_the_page() -> None:
    assert not _script_keys() - JS_KEYS
    assert i18n.js_messages("en") == {}
    assert i18n.js_messages("vi")["Sync queued"] == "Đã xếp hàng đồng bộ"


def test_translations_keep_every_placeholder() -> None:
    for english, vietnamese in VI.items():
        assert set(re.findall(r"\{(\w+)\}", english)) == set(re.findall(r"\{(\w+)\}", vietnamese)), english


def test_language_preference_round_trips_and_ignores_bad_values(isolated_language_preference: Path) -> None:
    assert i18n.current_language() == "vi"
    i18n.set_language("en")
    assert i18n.current_language() == "en"
    assert i18n.tr("Playlist deleted") == "Playlist deleted"
    i18n.set_language("vi")
    assert i18n.tr("Playlist deleted") == "Đã xoá playlist"

    isolated_language_preference.write_text('{"language": "fr"}', encoding="utf-8")
    assert i18n.current_language() == "vi"
    with pytest.raises(ValueError):
        i18n.set_language("fr")


def test_naive_database_datetimes_are_shown_in_local_time() -> None:
    stored = datetime(2026, 10, 19, 20, 30, 0)  # SQLite drops tzinfo from UTC values
    expected = stored.replace(tzinfo=timezone.utc).astimezone().strftime("%d/%m/%Y")
    assert app_module.format_local_datetime(stored, date_only=True) == expected
    # Task Scheduler strings are already local and must not be shifted.
    assert app_module.format_local_datetime("2026-10-20 12:00", weekday=True) == "Thứ Ba, 20/10/2026 12:00"
    assert app_module.format_local_datetime("2026-10-20 12:00", weekday=True, language="en") == "Tuesday, 20/10/2026 12:00"


async def test_http_errors_are_translated(isolated_language_preference: Path) -> None:
    response = await app_module.translated_http_exception_handler(
        None, HTTPException(status_code=404, detail="Task not found")
    )
    assert b"Kh\xc3\xb4ng t\xc3\xacm th\xe1\xba\xa5y t\xc3\xa1c v\xe1\xbb\xa5" in response.body


def test_language_switch_saves_preference_and_redirects(monkeypatch) -> None:
    async def fake_session():
        yield None

    async def password_configured(_session) -> bool:
        return True

    monkeypatch.setattr(app_module, "desktop_password_is_configured", password_configured)
    app_module.app.dependency_overrides[app_module.get_session] = fake_session
    try:
        _check_language_switch()
    finally:
        app_module.app.dependency_overrides.clear()


def _check_language_switch() -> None:
    client = TestClient(app_module.app, base_url=LOCAL_BASE_URL)
    login_page = client.get("/login")
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', login_page.text).group(1)
    assert 'lang="vi"' in login_page.text

    response = client.post(
        "/language",
        data={"csrf_token": csrf, "language": "en", "next": "/login"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/login"
    assert i18n.current_language() == "en"
    assert 'lang="en"' in client.get("/login").text


def test_shell_header_carries_title_and_section_for_soft_navigation() -> None:
    owner = User(id="header-owner", email="header@example.com", storage_used_bytes=0, storage_quota_bytes=1024)

    async def fake_user():
        return owner

    async def fake_session():
        yield None

    async def fake_history(*_args, **_kwargs):
        return {
            "groups": [], "playlist_options": [], "total_runs": 0, "downloaded_count": 0,
            "failed_count": 0, "unavailable_count": 0, "page": 1, "total_pages": 1,
        }

    async def fake_count(*_args) -> int:
        return 3

    app_module.app.dependency_overrides[app_module.get_optional_user] = fake_user
    app_module.app.dependency_overrides[app_module.get_session] = fake_session
    original_history, original_count = app_module.crud.get_scheduled_sync_history, app_module.crud.count_downloaded_media
    app_module.crud.get_scheduled_sync_history = fake_history
    app_module.crud.count_downloaded_media = fake_count
    try:
        response = TestClient(app_module.app, base_url=LOCAL_BASE_URL).get("/history")
    finally:
        app_module.crud.get_scheduled_sync_history = original_history
        app_module.crud.count_downloaded_media = original_count
        app_module.app.dependency_overrides.clear()

    header = response.text.split('<div id="shellHeader"', 1)[1].split('<main class="app-content">', 1)[0]
    assert 'data-nav-section="history"' in header
    assert "<h1>Lịch sử</h1>" in header
    assert "<strong>3</strong>" in header
    assert 'class="sidebar-link active" data-nav="history"' in response.text


def _stored_message_keys() -> set[str]:
    """Fixed messages that downloads and the scheduled sync save and later show to the user."""
    keys = set()
    for name in ["tasks.py", "downloader.py"]:
        text = (ROOT / name).read_text(encoding="utf-8")
        # AssertionError marks internal invariants, never a message for the user.
        keys.update(re.findall(r"""(?<!Assertion)(?:Error|Cancelled)\(\s*"([^"{]+)"\s*\)""", text))
        keys.update(re.findall(r"""\b(?:message|error)\s*=\s*"([^"{]+)\"""", text))
    return keys


def test_stored_task_messages_have_translations() -> None:
    assert not _stored_message_keys() - set(VI)


def test_stored_messages_are_translated_when_shown() -> None:
    assert i18n.translate_text("vi", "Storage quota reached") == "Đã hết dung lượng lưu trữ"
    assert (
        i18n.translate_text("vi", "Scheduled MP3 sync finished; 9 succeeded, 1 failed")
        == "Đồng bộ MP3 theo lịch đã xong; 9 thành công, 1 lỗi"
    )
    # Raw yt-dlp output and the English UI stay as they are.
    assert i18n.translate_text("vi", "ERROR: [youtube] abc: Sign in to confirm") == "ERROR: [youtube] abc: Sign in to confirm"
    assert i18n.translate_text("en", "Storage quota reached") == "Storage quota reached"
    assert i18n.translate_text("vi", None) == ""


def test_task_status_translates_stored_errors(isolated_language_preference: Path) -> None:
    from tasks import TaskProgress, TaskVideoError

    task = TaskProgress(task_id="t1", status="failed", error="All YouTube imports failed")
    task.errors.append(TaskVideoError(video_id=1, yt_video_id="abc", title="Song", message="Storage quota reached"))

    data = app_module.translated_task(task)

    assert data["error"] == "Tất cả link YouTube đều tải thất bại"
    assert data["errors"][0]["message"] == "Đã hết dung lượng lưu trữ"
    assert task.error == "All YouTube imports failed"
