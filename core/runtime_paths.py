from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Final
import os
import sys


MyralisUserRoot: Final[str] = "Myralis AI"
MyralisBackendLogs: Final[str] = "Logs"
MyralisBackendConfig: Final[str] = "Config"
MyralisBackendTemp: Final[str] = "Temp"


@dataclass(frozen=True)
class RuntimePaths:
    project_root: Path
    executable_root: Path
    frozen: bool
    assets_root: Path
    config_root: Path
    external_config_root: Path
    logs_root: Path
    temp_root: Path
    output_root: Path
    icon_path: Path

    def ensure_directories(self) -> None:
        for path in (
            self.assets_root,
            self.config_root,
            self.logs_root,
            self.temp_root,
            self.output_root,
        ):
            path.mkdir(parents=True, exist_ok=True)


def get_project_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


def get_runtime_paths(project_root: Path | None = None) -> RuntimePaths:
    resolved_project_root = (project_root or get_project_root()).resolve()
    frozen = bool(getattr(sys, "frozen", False))
    executable_root = (
        Path(sys.executable).resolve().parent if frozen else resolved_project_root
    )
    external_root = (
        Path(os.getenv("LOCALAPPDATA", str(Path.home() / "AppData" / "Local")))
        / MyralisUserRoot
    )
    preferred_config_root = (
        external_root / MyralisBackendConfig if frozen else resolved_project_root / "config"
    )
    preferred_logs_root = (
        external_root / MyralisBackendLogs / "Backend"
        if frozen
        else resolved_project_root / "output" / "logs"
    )
    preferred_temp_root = (
        external_root / MyralisBackendTemp / "Backend"
        if frozen
        else resolved_project_root / "output" / "temp"
    )
    config_root = _choose_writable_directory(
        preferred_config_root,
        executable_root / "config" if frozen else resolved_project_root / "config",
    )
    logs_root = _choose_writable_directory(
        preferred_logs_root,
        executable_root / "output" / "logs" if frozen else resolved_project_root / "output" / "logs",
    )
    temp_root = _choose_writable_directory(
        preferred_temp_root,
        executable_root / "output" / "temp" if frozen else resolved_project_root / "output" / "temp",
    )
    output_root = (
        executable_root / "output" if frozen else resolved_project_root / "output"
    )
    assets_root = (
        executable_root / "_internal" / "assets"
        if frozen
        else resolved_project_root / "assets"
    )
    icon_path = assets_root / "icons" / "myralis_backend.ico"
    return RuntimePaths(
        project_root=resolved_project_root,
        executable_root=executable_root,
        frozen=frozen,
        assets_root=assets_root,
        config_root=config_root,
        external_config_root=external_root / MyralisBackendConfig,
        logs_root=logs_root,
        temp_root=temp_root,
        output_root=output_root,
        icon_path=icon_path,
    )


def _choose_writable_directory(preferred: Path, fallback: Path) -> Path:
    try:
        preferred.mkdir(parents=True, exist_ok=True)
        return preferred
    except OSError:
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback
