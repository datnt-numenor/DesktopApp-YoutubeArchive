"""Start the loopback server without a GUI and verify its essential routes."""

from __future__ import annotations

import json
import sys
import threading
from pathlib import Path
from urllib.request import urlopen

import uvicorn


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from desktop import HOST, choose_local_port, wait_until_listening  # noqa: E402
from main import app  # noqa: E402


def main() -> None:
    assert HOST == "127.0.0.1", "Desktop server must only bind to loopback"
    port = choose_local_port()
    server = uvicorn.Server(uvicorn.Config(app, host=HOST, port=port, log_level="warning", access_log=False))
    server.install_signal_handlers = lambda: None
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    try:
        wait_until_listening(port)
        with urlopen(f"http://{HOST}:{port}/login", timeout=10) as response:
            login_status = response.status
            login_html = response.read().decode("utf-8")
        with urlopen(f"http://{HOST}:{port}/healthz", timeout=10) as response:
            health_status = response.status
            health_payload = json.load(response)
        with urlopen(f"http://{HOST}:{port}/register", timeout=10) as response:
            registration_final_url = response.geturl()
        with urlopen(f"http://{HOST}:{port}/forgot-password", timeout=10) as response:
            recovery_final_url = response.geturl()

        assert login_status == 200
        assert 'name="email"' not in login_html
        assert 'name="password"' in login_html
        assert "/register" not in login_html
        assert registration_final_url.endswith("/login")
        assert recovery_final_url.endswith("/login")
        assert health_status == 200
        assert health_payload["status"] == "ok"
        print(
            json.dumps(
                {
                    "host": HOST,
                    "port": port,
                    "login": login_status,
                    "auth": "password-only",
                    "health": health_payload["status"],
                }
            )
        )
    finally:
        server.should_exit = True
        thread.join(timeout=10)


if __name__ == "__main__":
    main()
