import socket
import threading
from types import SimpleNamespace

import pytest

import desktop


def test_desktop_host_is_loopback_only() -> None:
    assert desktop.HOST == "127.0.0.1"
    assert desktop.APP_USER_MODEL_ID == "DesktopYouTubeArchive.LocalApp"
    assert desktop.ICON_PATH.is_file()


def test_choose_local_port_falls_back_when_preferred_port_is_busy(monkeypatch) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind((desktop.HOST, 0))
        listener.listen()
        occupied_port = int(listener.getsockname()[1])
        monkeypatch.setattr(desktop, "PREFERRED_PORT", occupied_port)

        selected_port = desktop.choose_local_port()

    assert selected_port != occupied_port
    assert 0 < selected_port <= 65535


def test_server_supervisor_restarts_after_unexpected_exit(monkeypatch) -> None:
    stop_event = threading.Event()
    server_state = {}
    servers = []

    class FakeServer:
        should_exit = False

        def run(self) -> None:
            servers.append(self)
            if len(servers) == 2:
                stop_event.set()

    monkeypatch.setattr(desktop, "create_local_server", lambda _port: FakeServer())

    desktop.supervise_local_server(
        8765,
        stop_event,
        server_state,
        restart_delay_seconds=0,
    )

    assert len(servers) == 2
    assert server_state == {}


def test_server_supervisor_stops_after_application_startup_failure(monkeypatch) -> None:
    stop_event = threading.Event()
    startup_failed = threading.Event()
    server_state = {}
    servers = []

    class FakeServer:
        should_exit = False
        started = False

        def run(self) -> None:
            servers.append(self)
            raise SystemExit(3)

    monkeypatch.setattr(desktop, "create_local_server", lambda _port: FakeServer())

    desktop.supervise_local_server(8765, stop_event, server_state, startup_failed, restart_delay_seconds=0)

    assert len(servers) == 1
    assert startup_failed.is_set()
    assert server_state == {}


def test_wait_until_server_ready_reports_startup_failure_immediately() -> None:
    startup_failed = threading.Event()
    startup_failed.set()

    with pytest.raises(RuntimeError, match="failed during startup"):
        desktop.wait_until_server_ready(8765, startup_failed)


class FakeLoadedEvent:
    def __init__(self) -> None:
        self.handler = None

    def __iadd__(self, handler):
        self.handler = handler
        return self

    def fire(self) -> None:
        assert self.handler is not None
        self.handler()


class FakeWindow:
    def __init__(self, current_url: str) -> None:
        self.current_url = current_url
        self.events = SimpleNamespace(loaded=FakeLoadedEvent())
        self.actions = []

    def get_current_url(self) -> str:
        return self.current_url

    def maximize(self) -> None:
        self.actions.append(("maximize",))

    def restore(self) -> None:
        self.actions.append(("restore",))

    def resize(self, width: int, height: int) -> None:
        self.actions.append(("resize", width, height))

    def move(self, x: int, y: int) -> None:
        self.actions.append(("move", x, y))


def test_desktop_window_uses_application_icon_and_compact_startup(monkeypatch) -> None:
    created = []
    started = []
    screen = SimpleNamespace(x=0, y=0, width=1440, height=900)
    window = FakeWindow("http://127.0.0.1:8765/login")

    def fake_create_window(*args, **kwargs):
        created.append((args, kwargs))
        return window

    monkeypatch.setattr(desktop.webview, "screens", [screen])
    monkeypatch.setattr(desktop.webview, "create_window", fake_create_window)
    monkeypatch.setattr(desktop.webview, "start", lambda *args, **kwargs: started.append((args, kwargs)))

    desktop.open_desktop_window("http://127.0.0.1:8765")

    assert created[0][0][0] == "Desktop YouTube Archive"
    assert created[0][0][1] == "http://127.0.0.1:8765"
    assert created[0][1]["width"] == 900
    assert created[0][1]["height"] == 650
    assert created[0][1]["min_size"] == (760, 560)
    assert created[0][1]["screen"] is screen
    assert started[0][1]["icon"] == str(desktop.ICON_PATH)


def test_desktop_window_maximizes_after_login_and_restores_after_logout() -> None:
    screen = SimpleNamespace(x=0, y=0, width=1440, height=900)
    window = FakeWindow("http://127.0.0.1:8765/login")
    desktop.bind_window_mode_changes(window, screen)

    window.events.loaded.fire()
    assert window.actions == []

    window.current_url = "http://127.0.0.1:8765/"
    window.events.loaded.fire()
    assert window.actions == [("maximize",)]

    window.current_url = "http://127.0.0.1:8765/playlist/1"
    window.events.loaded.fire()
    assert window.actions == [("maximize",)]

    window.current_url = "http://127.0.0.1:8765/login"
    window.events.loaded.fire()
    assert window.actions[-3:] == [
        ("restore",),
        ("resize", 900, 650),
        ("move", 270, 125),
    ]


def test_desktop_auth_routes_are_detected() -> None:
    assert desktop.is_auth_url("http://127.0.0.1:8765/login?next=/")
    assert desktop.is_auth_url("http://127.0.0.1:8765/reset-password?token=abc")
    assert not desktop.is_auth_url("http://127.0.0.1:8765/settings")
