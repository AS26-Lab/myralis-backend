# -*- mode: python ; coding: utf-8 -*-
from __future__ import annotations

from pathlib import Path

project_root = Path(SPECPATH).resolve()
icon_path = project_root / "assets" / "icons" / "myralis_backend.ico"
if icon_path.exists():
    print(f"Using backend icon: {icon_path}")
    exe_icon = str(icon_path)
else:
    print("Icono no encontrado; se generara la build con el icono predeterminado.")
    print(f"Expected icon path: {icon_path}")
    exe_icon = None

datas = []
for relative_dir in ("assets", "config"):
    source_dir = project_root / relative_dir
    if not source_dir.exists():
        continue
    for file_path in source_dir.rglob("*"):
        if file_path.is_file() and file_path.suffix.lower() != ".env":
            datas.append((str(file_path), str(file_path.parent.relative_to(project_root))))

hiddenimports = ["faster_whisper"]

a = Analysis(
    ["main.py"],
    pathex=[str(project_root)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="MyralisBackend",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    icon=exe_icon,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    name="MyralisBackend",
)
