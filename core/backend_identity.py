from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Mapping

OFFICIAL_BACKEND_PROJECT_NAME: Final[str] = "MyralisBackend"
OFFICIAL_BACKEND_DISPLAY_NAME: Final[str] = "Myralis Backend"
OFFICIAL_BACKEND_SERVICE_NAME: Final[str] = "myralis-backend"
OFFICIAL_BACKEND_EXE_NAME: Final[str] = "MyralisBackend.exe"
LEGACY_BACKEND_PROJECT_NAME: Final[str] = "PYTHON_AI_ASSISTANT"

BACKEND_SOURCE_ENV_VAR: Final[str] = "MYRALIS_BACKEND_SOURCE_DIR"
BACKEND_EXE_ENV_VAR: Final[str] = "MYRALIS_BACKEND_EXE"

BACKEND_SOURCE_RELATIVE_PATH: Final[Path] = Path("..") / OFFICIAL_BACKEND_PROJECT_NAME
LEGACY_BACKEND_SOURCE_RELATIVE_PATH: Final[Path] = Path("..") / LEGACY_BACKEND_PROJECT_NAME
BACKEND_EXE_RELATIVE_PATH: Final[Path] = Path("Runtime") / "Backend" / OFFICIAL_BACKEND_EXE_NAME
BACKEND_INSTALL_EXE_RELATIVE_PATH: Final[Path] = Path("Backend") / OFFICIAL_BACKEND_EXE_NAME
BACKEND_HEALTH_URL: Final[str] = "http://127.0.0.1:8766/health"


@dataclass(frozen=True)
class PathResolution:
    selected_source: str
    path: Path
    exists: bool
    legacy: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "selected_source": self.selected_source,
            "path": str(self.path),
            "exists": self.exists,
            "legacy": self.legacy,
        }


def get_backend_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _expand_candidate_path(value: str | Path, reference_root: Path) -> Path:
    candidate = Path(value).expanduser()
    if candidate.is_absolute():
        return candidate.resolve()
    return (reference_root / candidate).resolve()


def _selected_resolution(
    *,
    selected_source: str,
    path: Path,
    legacy: bool,
) -> PathResolution:
    return PathResolution(
        selected_source=selected_source,
        path=path,
        exists=path.exists(),
        legacy=legacy,
    )


def resolve_backend_source_dir(
    explicit_path: str | Path | None = None,
    *,
    reference_root: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> PathResolution:
    root = reference_root or get_backend_root()
    environ = os.environ if env is None else env

    candidates: list[tuple[str, Path, bool]] = []

    env_value = str(environ.get(BACKEND_SOURCE_ENV_VAR, "")).strip()
    if env_value:
        candidates.append(
            ("environment", _expand_candidate_path(env_value, root), _is_legacy_path(env_value))
        )

    if explicit_path is not None:
        candidates.append(
            ("config", _expand_candidate_path(explicit_path, root), _is_legacy_path(explicit_path))
        )

    new_default = (root.parent / OFFICIAL_BACKEND_PROJECT_NAME).resolve()
    legacy_default = (root.parent / LEGACY_BACKEND_PROJECT_NAME).resolve()
    candidates.append(("new_default", new_default, False))
    candidates.append(("legacy_fallback", legacy_default, True))

    for selected_source, path, legacy in candidates:
        if path.exists():
            resolution = _selected_resolution(
                selected_source=selected_source,
                path=path,
                legacy=legacy,
            )
            _log_backend_source_selection(resolution, new_default, legacy_default)
            return resolution

    return _selected_resolution(
        selected_source="error",
        path=new_default,
        legacy=False,
    )


def resolve_backend_executable(
    install_root: str | Path | None = None,
    *,
    reference_root: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> PathResolution:
    root = reference_root or get_backend_root()
    environ = os.environ if env is None else env

    candidates: list[tuple[str, Path, bool]] = []

    env_value = str(environ.get(BACKEND_EXE_ENV_VAR, "")).strip()
    if env_value:
        candidates.append(("environment", _expand_exe_candidate(env_value, root), False))

    if install_root is not None:
        candidates.append(("install_root", _expand_install_candidate(install_root, root), False))

    default_runtime = (root / BACKEND_EXE_RELATIVE_PATH).resolve()
    candidates.append(("runtime_default", default_runtime, False))

    for selected_source, path, legacy in candidates:
        if path.exists():
            return _selected_resolution(
                selected_source=selected_source,
                path=path,
                legacy=legacy,
            )

    return _selected_resolution(
        selected_source="error",
        path=default_runtime,
        legacy=False,
    )


def _expand_exe_candidate(value: str | Path, reference_root: Path) -> Path:
    candidate = _expand_candidate_path(value, reference_root)
    if candidate.is_dir():
        return (candidate / OFFICIAL_BACKEND_EXE_NAME).resolve()
    return candidate


def _expand_install_candidate(value: str | Path, reference_root: Path) -> Path:
    candidate = _expand_candidate_path(value, reference_root)
    if candidate.is_dir():
        return (candidate / BACKEND_INSTALL_EXE_RELATIVE_PATH).resolve()
    return candidate


def _is_legacy_path(value: str | Path) -> bool:
    normalized = str(value).replace("\\", "/").lower()
    return LEGACY_BACKEND_PROJECT_NAME.lower() in normalized


def _log_backend_source_selection(
    resolution: PathResolution,
    new_default: Path,
    legacy_default: Path,
) -> None:
    if resolution.legacy:
        logging.getLogger(__name__).warning(
            "Se esta usando la ruta legacy PYTHON_AI_ASSISTANT. Migra el backend a MyralisBackend."
        )
    elif resolution.path == new_default and legacy_default.exists():
        logging.getLogger(__name__).info(
            "Se ignoro la ruta legacy PYTHON_AI_ASSISTANT porque ya existe MyralisBackend."
        )
