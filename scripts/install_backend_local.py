from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "dist" / "MyralisBackend"
TARGET = Path(os.getenv("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))) / "Myralis AI" / "Runtime" / "Backend"


def _is_running(exe_name: str) -> bool:
    if sys.platform != "win32":
        return False
    result = subprocess.run(
        ["tasklist", "/FI", f"IMAGENAME eq {exe_name}"],
        capture_output=True,
        text=True,
        check=False,
    )
    return exe_name.lower() in result.stdout.lower()


def main() -> int:
    if not SOURCE.exists():
        raise SystemExit(f"Missing build output: {SOURCE}")
    if _is_running("MyralisBackend.exe"):
        raise SystemExit("MyralisBackend.exe is running. Close it before installing.")

    TARGET.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=str(TARGET.parent), prefix="Backend.") as temp_dir:
        staging = Path(temp_dir) / "Backend"
        shutil.copytree(SOURCE, staging)
        exe_path = staging / "MyralisBackend.exe"
        if not exe_path.exists():
            raise SystemExit("Staging validation failed: exe not found.")

        backup = TARGET.with_name("Backend.backup")
        if TARGET.exists():
            if backup.exists():
                shutil.rmtree(backup)
            TARGET.replace(backup)
        staging.replace(TARGET)
        if backup.exists():
            shutil.rmtree(backup)

    print(f"Installed backend to: {TARGET}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

