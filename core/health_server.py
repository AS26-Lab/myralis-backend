from __future__ import annotations

import json
import logging
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

try:
    import psycopg
except Exception:  # pragma: no cover - optional during bootstrap
    psycopg = None

LOGGER = logging.getLogger(__name__)

_DEFAULT_HOST = "127.0.0.1"
_DEFAULT_PORT = 8766
_SERVICE_NAME = "bus-telemetry-api"
_DEFAULT_DATABASE_URL = (
    "postgresql://bus_telemetry_dev:bus_telemetry_dev_password@127.0.0.1:5432/"
    "bus_telemetry_dev"
)

_server_lock = threading.RLock()
_server_thread: threading.Thread | None = None
_http_server: ThreadingHTTPServer | None = None
_websocket_running_getter: Callable[[], bool] | None = None
_authorized_session_getter: Callable[[], bool] | None = None


class _HealthHandler(BaseHTTPRequestHandler):
    server_version = "MyralisHealth/1.0"

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        path = self.path.rstrip("/")
        if path == "/health":
            self._write_json(200, {"status": "ok", "service": _SERVICE_NAME})
            return

        if path == "/health/database":
            connected = _database_is_connected()
            if connected:
                self._write_json(200, {"status": "ok", "database": "connected"})
            else:
                self._write_json(503, {"status": "error", "database": "disconnected"})
            return

        self._write_json(404, {"ok": False, "reason": "not_found"})

    def _write_json(self, status_code: int, payload: dict[str, object]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A003 - API signature
        LOGGER.debug("health_http: " + format, *args)


def _database_url() -> str:
    return os.getenv("DATABASE_URL", "").strip() or _DEFAULT_DATABASE_URL


def _database_is_connected() -> bool:
    if psycopg is None:
        return False

    try:
        with psycopg.connect(_database_url(), connect_timeout=3) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
                cursor.fetchone()
        return True
    except Exception:
        LOGGER.debug("Database health check failed", exc_info=True)
        return False

def _get_websocket_running() -> bool:
    with _server_lock:
        getter = _websocket_running_getter
    if getter is None:
        return False
    try:
        return bool(getter())
    except Exception:
        LOGGER.debug("Failed to read websocket status for health endpoint", exc_info=True)
        return False


def _get_authorized_session() -> bool:
    with _server_lock:
        getter = _authorized_session_getter
    if getter is None:
        return False
    try:
        return bool(getter())
    except Exception:
        LOGGER.debug("Failed to read authorization status for health endpoint", exc_info=True)
        return False


def start_health_server(
    websocket_running_getter: Callable[[], bool] | None = None,
    authorized_session_getter: Callable[[], bool] | None = None,
    *,
    host: str = _DEFAULT_HOST,
    port: int = _DEFAULT_PORT,
) -> bool:
    global _server_thread, _http_server, _websocket_running_getter, _authorized_session_getter

    with _server_lock:
        if _server_thread is not None and _server_thread.is_alive():
            return False

        _websocket_running_getter = websocket_running_getter
        _authorized_session_getter = authorized_session_getter

        try:
            httpd = ThreadingHTTPServer((host, port), _HealthHandler)
            httpd.daemon_threads = True
        except OSError as exc:
            LOGGER.warning(
                "No se pudo iniciar health HTTP en http://%s:%s/health: %s",
                host,
                port,
                exc,
            )
            _http_server = None
            return False

        thread = threading.Thread(target=httpd.serve_forever, name="HealthHTTPServer", daemon=True)
        _http_server = httpd
        _server_thread = thread
        thread.start()
        LOGGER.info("Health HTTP activo en http://%s:%s/health", host, port)
        return True


def stop_health_server() -> None:
    global _server_thread, _http_server, _websocket_running_getter, _authorized_session_getter

    with _server_lock:
        httpd = _http_server
        thread = _server_thread
        _http_server = None
        _server_thread = None
        _websocket_running_getter = None
        _authorized_session_getter = None

    if httpd is not None:
        try:
            httpd.shutdown()
            httpd.server_close()
        except Exception:
            LOGGER.exception("No se pudo detener el health HTTP limpiamente")

    if thread is not None and thread.is_alive() and thread is not threading.current_thread():
        thread.join(timeout=3.0)


def is_health_server_active() -> bool:
    with _server_lock:
        return _server_thread is not None and _server_thread.is_alive()
