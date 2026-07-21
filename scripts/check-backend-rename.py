from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.backend_identity import (
    OFFICIAL_BACKEND_DISPLAY_NAME,
    OFFICIAL_BACKEND_PROJECT_NAME,
    resolve_backend_source_dir,
)


LEGACY_TOKENS = (
    "PYTHON_AI_ASSISTANT",
    "python_ai_assistant",
    "PYTHON AI ASSISTANT",
    "PYTHON-AI-ASSISTANT",
)

NEW_TOKENS = ("MyralisBackend", "MyralisBackend.exe")

TEXT_EXTENSIONS = {
    ".py",
    ".md",
    ".json",
    ".xml",
    ".iml",
    ".txt",
    ".ini",
    ".yml",
    ".yaml",
    ".js",
    ".ts",
    ".ps1",
    ".bat",
    ".spec",
}

SKIP_DIR_NAMES = {".git", ".venv", "node_modules", "__pycache__", "dist", "build"}


@dataclass(frozen=True)
class ReferenceHit:
    path: str
    token: str
    category: str
    note: str


def _iter_text_files(root: Path) -> Iterable[Path]:
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if any(part in SKIP_DIR_NAMES for part in path.parts):
            continue
        if path.suffix.lower() not in TEXT_EXTENSIONS:
            continue
        yield path


def _classify(path: Path, token: str) -> tuple[str, str]:
    normalized = path.as_posix().lower()
    if ".idea/" in normalized or "/.vs/" in normalized:
        return "E", "No requiere cambio; es metadata local de IDE."
    if path.name in {"README.md", "runtime-layout.md"} or "/docs/" in normalized:
        return "C", "Referencia documental."
    if path.name in {"__init__.py"}:
        return "D", "Nombre visual o docstring de paquete."
    if path.name == "health_server.py":
        return "A", "Nombre de servicio y payload de health."
    if path.name == "backend_identity.py":
        return "B", "Compatibilidad temporal y resolucion portable."
    if path.name == "main.py":
        return "B", "Nombre visible de aplicacion."
    if "main_window.py" in normalized:
        return "B", "Titulo visible de la UI."
    if path.name.startswith("check-"):
        return "C", "Tooling de diagnostico."
    if token in NEW_TOKENS:
        return "E", "Nombre nuevo oficial o futuro exe."
    return "A", "Referencia funcional o de configuracion."


def _find_references(root: Path) -> list[ReferenceHit]:
    hits: list[ReferenceHit] = []
    for path in _iter_text_files(root):
        try:
            content = path.read_text(encoding="utf-8")
        except Exception:
            continue
        for token in LEGACY_TOKENS + NEW_TOKENS:
            if token in content:
                category, note = _classify(path, token)
                hits.append(
                    ReferenceHit(
                        path=str(path.relative_to(root)),
                        token=token,
                        category=category,
                        note=note,
                    )
                )
    return hits


def main() -> int:
    root = ROOT
    source_resolution = resolve_backend_source_dir(reference_root=root)
    hits = _find_references(root)

    report = {
        "backend_official_name": OFFICIAL_BACKEND_PROJECT_NAME,
        "backend_display_name": OFFICIAL_BACKEND_DISPLAY_NAME,
        "selected_source": source_resolution.selected_source,
        "selected_source_path": str(source_resolution.path),
        "selected_source_exists": source_resolution.exists,
        "legacy_fallback_used": source_resolution.legacy,
        "references": [hit.__dict__ for hit in hits],
        "legacy_reference_count": sum(1 for hit in hits if hit.token in LEGACY_TOKENS),
        "new_reference_count": sum(1 for hit in hits if hit.token in NEW_TOKENS),
    }

    print(json.dumps(report, indent=2, ensure_ascii=False))

    if not source_resolution.exists:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
