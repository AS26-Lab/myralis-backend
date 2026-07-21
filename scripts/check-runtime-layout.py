from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.backend_identity import (
    BACKEND_HEALTH_URL,
    BACKEND_SOURCE_ENV_VAR,
    OFFICIAL_BACKEND_DISPLAY_NAME,
    OFFICIAL_BACKEND_EXE_NAME,
    OFFICIAL_BACKEND_PROJECT_NAME,
    PathResolution,
    resolve_backend_executable,
    resolve_backend_source_dir,
)


@dataclass(frozen=True)
class HealthCheckResult:
    reachable: bool
    status_code: int | None
    error: str | None


def _check_health(url: str) -> HealthCheckResult:
    try:
        with urlopen(url, timeout=1.5) as response:
            response.read(256)
            return HealthCheckResult(True, int(response.status), None)
    except URLError as exc:
        return HealthCheckResult(False, None, str(exc.reason))
    except Exception as exc:  # pragma: no cover - defensive diagnostics
        return HealthCheckResult(False, None, str(exc))


def main() -> int:
    root = ROOT
    source_resolution: PathResolution = resolve_backend_source_dir(reference_root=root)
    exe_resolution: PathResolution = resolve_backend_executable(reference_root=root)
    health = _check_health(BACKEND_HEALTH_URL)
    launch_mode = str(os.getenv("MYRALIS_BACKEND_LAUNCH_MODE", "python")).strip() or "python"

    report = {
        "backend_official_name": OFFICIAL_BACKEND_PROJECT_NAME,
        "backend_display_name": OFFICIAL_BACKEND_DISPLAY_NAME,
        "backend_launch_mode": launch_mode,
        "backend_source_resolved_path": str(source_resolution.path),
        "backend_source_resolution_source": source_resolution.selected_source,
        "legacy_fallback_used": source_resolution.legacy,
        "backend_exe_resolved_path": str(exe_resolution.path),
        "backend_source_exists": source_resolution.exists,
        "backend_exe_exists": exe_resolution.exists,
        "health_url": BACKEND_HEALTH_URL,
        "health_reachable": health.reachable,
        "health_status_code": health.status_code,
        "health_error": health.error,
        "backend_source_env_var": BACKEND_SOURCE_ENV_VAR,
        "backend_exe_name": OFFICIAL_BACKEND_EXE_NAME,
    }

    for key, value in report.items():
        print(f"{key}: {value}")

    if source_resolution.selected_source == "error":
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
