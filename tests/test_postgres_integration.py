from __future__ import annotations

import json
import os
import socket
import time
import urllib.error
import urllib.request
from contextlib import closing
from unittest.mock import patch

import pytest

from core.health_server import start_health_server, stop_health_server


def _free_port() -> int:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _fetch_json(url: str) -> tuple[int, dict[str, object]]:
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            body = response.read().decode("utf-8")
            return response.status, json.loads(body)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8")
        return exc.code, json.loads(body)


@pytest.mark.integration
def test_health_endpoints_report_database_connection() -> None:
    port = _free_port()
    env = {
        "DATABASE_URL": "postgresql://bus_telemetry_dev:bus_telemetry_dev_password@127.0.0.1:5432/bus_telemetry_dev",
        "POSTGRES_DB": "bus_telemetry_dev",
        "POSTGRES_USER": "bus_telemetry_dev",
        "POSTGRES_PASSWORD": "bus_telemetry_dev_password",
    }

    stop_health_server()
    with patch.dict(os.environ, env, clear=False):
        started = start_health_server(port=port)
        assert started
        try:
            deadline = time.time() + 10
            while time.time() < deadline:
                try:
                    health_status, health_payload = _fetch_json(
                        f"http://127.0.0.1:{port}/health"
                    )
                    db_status, db_payload = _fetch_json(
                        f"http://127.0.0.1:{port}/health/database"
                    )
                    if health_status == 200 and db_status == 200:
                        assert health_payload == {
                            "status": "ok",
                            "service": "bus-telemetry-api",
                        }
                        assert db_payload == {
                            "status": "ok",
                            "database": "connected",
                        }
                        return
                except Exception:
                    time.sleep(0.5)
            raise AssertionError("health endpoints did not become ready in time")
        finally:
            stop_health_server()
