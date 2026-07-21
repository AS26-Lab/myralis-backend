from __future__ import annotations

import json
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.request import urlopen


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
EXE = ROOT / "dist" / "MyralisBackend" / "MyralisBackend.exe"
HEALTH_URL = "http://127.0.0.1:8766/health"


def _wait_for_port(host: str, port: int, timeout: float) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=1.0):
                return True
        except OSError:
            time.sleep(0.25)
    return False


def _read_health() -> dict[str, object]:
    with urlopen(HEALTH_URL, timeout=2.0) as response:
        payload = json.loads(response.read().decode("utf-8"))
        if not isinstance(payload, dict):
            raise AssertionError("health payload is not a JSON object")
        return payload


def _check_websocket() -> bool:
    try:
        import asyncio
        import websockets
    except Exception:
        return False

    async def _probe() -> bool:
        async with websockets.connect("ws://127.0.0.1:8765") as websocket:
            await websocket.close()
            return True

    return bool(asyncio.run(_probe()))


def main() -> int:
    if not EXE.exists():
        raise SystemExit(f"Missing packaged executable: {EXE}")

    creationflags = 0
    if sys.platform == "win32":
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]

    proc = subprocess.Popen(
        [str(EXE)],
        cwd=str(EXE.parent),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=creationflags,
    )

    try:
        if not _wait_for_port("127.0.0.1", 8766, 30.0):
            raise AssertionError("health port did not open within 30s")

        health = _read_health()
        assert health.get("ok") is True
        assert health.get("service") == "myralis-backend"
        assert health.get("status") == "running"
        assert "authorized_session" in health
        assert proc.poll() is None
        websocket_ok = _check_websocket()
        print("health: OK")
        print(f"websocket: {'OK' if websocket_ok else 'SKIPPED'}")
        print(f"authorized_session: {health.get('authorized_session')}")
        return 0
    finally:
        if proc.poll() is None:
            try:
                if sys.platform == "win32":
                    proc.send_signal(signal.CTRL_BREAK_EVENT)
                    time.sleep(1.0)
            except Exception:
                pass
            if proc.poll() is None:
                proc.terminate()
            try:
                proc.wait(timeout=10.0)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=10.0)


if __name__ == "__main__":
    raise SystemExit(main())
