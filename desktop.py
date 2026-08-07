"""Launch the local FastAPI application in a native desktop window."""

from __future__ import annotations

import ctypes
import socket
import sys
import threading
import time
from contextlib import closing
from pathlib import Path
from urllib.parse import urlparse

import uvicorn
import webview

from main import app


HOST = "127.0.0.1"
PREFERRED_PORT = 8765
APP_USER_MODEL_ID = "DesktopYouTubeArchive.LocalApp"
ICON_PATH = Path(__file__).resolve().parent / "static" / "img" / "logo-transparent-v2.ico"
AUTH_WINDOW_WIDTH = 900
AUTH_WINDOW_HEIGHT = 650
AUTH_WINDOW_MIN_SIZE = (760, 560)
AUTH_PATHS = {
    "/login",
    "/register",
    "/forgot-password",
    "/reset-password",
    "/verify-email",
}


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
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as probe:
            probe.settimeout(0.25)
            if probe.connect_ex((HOST, port)) == 0:
                return
        time.sleep(0.1)
    raise RuntimeError("The local application server did not start in time")


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
    configure_windows_app_identity()
    if not ICON_PATH.is_file():
        raise FileNotFoundError(f"Application icon not found: {ICON_PATH}")

    port = choose_local_port()
    config = uvicorn.Config(app, host=HOST, port=port, log_level="info", access_log=False)
    server = uvicorn.Server(config)
    server.install_signal_handlers = lambda: None

    server_thread = threading.Thread(target=server.run, name="ytarchive-local-server", daemon=True)
    server_thread.start()

    try:
        wait_until_listening(port)
        open_desktop_window(f"http://{HOST}:{port}")
    finally:
        server.should_exit = True
        server_thread.join(timeout=10)


if __name__ == "__main__":
    main()
