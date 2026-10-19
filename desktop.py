"""Launch the local FastAPI application in a native desktop window."""

from __future__ import annotations

import ctypes
import logging
import socket
import sys
import threading
import time
from contextlib import closing
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import BinaryIO
from urllib.parse import urlparse

import uvicorn
import webview

from config import LOCAL_DATA_DIR
from sync_lock import acquire_file_lock
from downloader import request_download_shutdown
from main import app


HOST = "127.0.0.1"
PREFERRED_PORT = 8765
APP_USER_MODEL_ID = "DesktopYouTubeArchive.LocalApp"
ICON_PATH = Path(__file__).resolve().parent / "static" / "img" / "logo-transparent-v2.ico"
AUTH_WINDOW_WIDTH = 900
AUTH_WINDOW_HEIGHT = 650
AUTH_WINDOW_MIN_SIZE = (760, 560)
SERVER_RESTART_DELAY_SECONDS = 1.0
DESKTOP_LOG_PATH = LOCAL_DATA_DIR / "logs" / "desktop.log"
INSTANCE_LOCK_PATH = LOCAL_DATA_DIR / "desktop.lock"
ALREADY_RUNNING_MESSAGE = "Desktop YouTube Archive đang chạy. Hãy dùng cửa sổ đang mở."
AUTH_PATHS = {"/login"}
logger = logging.getLogger("desktop")


def configure_desktop_logging() -> None:
    """Persist desktop and background-server failures for later diagnosis."""
    DESKTOP_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        DESKTOP_LOG_PATH,
        maxBytes=2 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s"))
    for target in (logger, logging.getLogger("uvicorn.error")):
        target.handlers.clear()
        target.addHandler(handler)
        target.setLevel(logging.INFO)
        target.propagate = False


def acquire_single_instance_lock(path: Path = INSTANCE_LOCK_PATH) -> BinaryIO | None:
    """Return a lock held for the process lifetime, or None when another app instance owns it.

    Two instances would run two download queues on one database, and each startup closes the
    other's in-progress syncs as interrupted.
    """
    try:
        return acquire_file_lock(path)
    except OSError:
        return None


def show_already_running_message() -> None:
    if sys.platform == "win32":
        ctypes.windll.user32.MessageBoxW(None, ALREADY_RUNNING_MESSAGE, "Desktop YouTube Archive", 0x40)
    else:  # pragma: no cover - desktop target is Windows
        print(ALREADY_RUNNING_MESSAGE, file=sys.stderr)


def configure_windows_app_identity() -> None:
    """Give the pythonw-hosted window its own taskbar application identity."""
    if sys.platform == "win32":
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_USER_MODEL_ID)


def choose_local_port() -> int:
    """Use the stable desktop port when available, otherwise choose a free one."""
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as probe:
        try:
            probe.bind((HOST, PREFERRED_PORT))
            return PREFERRED_PORT
        except OSError:
            probe.bind((HOST, 0))
            return int(probe.getsockname()[1])


def wait_until_listening(port: int, timeout_seconds: float = 15.0) -> None:
    wait_until_server_ready(port, threading.Event(), timeout_seconds)


def wait_until_server_ready(
    port: int,
    startup_failed: threading.Event,
    timeout_seconds: float = 15.0,
) -> None:
    """Wait for the local server, failing early after an application startup error."""
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if startup_failed.is_set():
            raise RuntimeError("The local application server failed during startup; see desktop.log")
        with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as probe:
            probe.settimeout(0.25)
            if probe.connect_ex((HOST, port)) == 0:
                return
        time.sleep(0.1)
    raise RuntimeError("The local application server did not start in time")


def create_local_server(port: int) -> uvicorn.Server:
    config = uvicorn.Config(
        app,
        host=HOST,
        port=port,
        log_level="info",
        access_log=False,
        log_config=None,
    )
    server = uvicorn.Server(config)
    server.install_signal_handlers = lambda: None
    return server


def supervise_local_server(
    port: int,
    stop_event: threading.Event,
    server_state: dict[str, uvicorn.Server],
    startup_failed: threading.Event | None = None,
    *,
    restart_delay_seconds: float = SERVER_RESTART_DELAY_SECONDS,
) -> None:
    """Keep the local API alive while the desktop window is open."""
    restart_count = 0
    while not stop_event.is_set():
        server = create_local_server(port)
        server_state["server"] = server
        try:
            server.run()
        except BaseException:  # noqa: BLE001 - a dead server must not leave a frozen-looking UI
            logger.exception("Local media server crashed")
            if not server.started and startup_failed is not None:
                startup_failed.set()
                break
        finally:
            if server_state.get("server") is server:
                server_state.pop("server", None)

        if stop_event.is_set():
            break

        restart_count += 1
        logger.error(
            "Local media server stopped unexpectedly; restarting in %.1f second(s) (attempt %d)",
            restart_delay_seconds,
            restart_count,
        )
        stop_event.wait(restart_delay_seconds)


def is_auth_url(url: str | None) -> bool:
    """Return whether a desktop URL belongs to the public authentication flow."""
    if not url:
        return False
    return urlparse(url).path in AUTH_PATHS


def center_auth_window(window: webview.Window, screen: object | None) -> None:
    """Restore the compact login window and place it in the screen center."""
    window.restore()
    window.resize(AUTH_WINDOW_WIDTH, AUTH_WINDOW_HEIGHT)
    if screen is None:
        return

    x = int(screen.x + max(0, (screen.width - AUTH_WINDOW_WIDTH) // 2))
    y = int(screen.y + max(0, (screen.height - AUTH_WINDOW_HEIGHT) // 2))
    window.move(x, y)


def bind_window_mode_changes(window: webview.Window, screen: object | None) -> None:
    """Keep authentication compact and maximize all authenticated pages."""
    current_mode = "auth"

    def update_window_mode() -> None:
        nonlocal current_mode
        current_url = window.get_current_url()
        if not current_url:
            return

        next_mode = "auth" if is_auth_url(current_url) else "app"
        if next_mode == current_mode:
            return

        if next_mode == "app":
            window.maximize()
        else:
            center_auth_window(window, screen)
        current_mode = next_mode

    window.events.loaded += update_window_mode


def open_desktop_window(url: str) -> None:
    primary_screen = webview.screens[0] if webview.screens else None
    window = webview.create_window(
        "Desktop YouTube Archive",
        url,
        width=AUTH_WINDOW_WIDTH,
        height=AUTH_WINDOW_HEIGHT,
        min_size=AUTH_WINDOW_MIN_SIZE,
        screen=primary_screen,
    )
    if window is not None:
        bind_window_mode_changes(window, primary_screen)
    webview.start(debug=False, icon=str(ICON_PATH))


def main() -> None:
    configure_desktop_logging()
    instance_lock = acquire_single_instance_lock()
    if instance_lock is None:
        logger.info("Another desktop instance is already running; exiting")
        show_already_running_message()
        return
    configure_windows_app_identity()
    if not ICON_PATH.is_file():
        raise FileNotFoundError(f"Application icon not found: {ICON_PATH}")

    port = choose_local_port()
    stop_event = threading.Event()
    startup_failed = threading.Event()
    server_state: dict[str, uvicorn.Server] = {}
    server_thread = threading.Thread(
        target=supervise_local_server,
        args=(port, stop_event, server_state, startup_failed),
        name="ytarchive-local-server-supervisor",
        daemon=False,
    )
    server_thread.start()

    try:
        wait_until_server_ready(port, startup_failed)
        logger.info("Desktop app started on http://%s:%d", HOST, port)
        open_desktop_window(f"http://{HOST}:{port}")
    finally:
        stop_event.set()
        # Stop yt-dlp first so the server's worker threads can finish within the join timeout.
        request_download_shutdown()
        server = server_state.get("server")
        if server is not None:
            server.should_exit = True
        server_thread.join(timeout=10)
        if server_thread.is_alive():
            logger.error("Local media server did not stop within the shutdown timeout")
        else:
            logger.info("Desktop app stopped")
        instance_lock.close()


if __name__ == "__main__":
    main()
