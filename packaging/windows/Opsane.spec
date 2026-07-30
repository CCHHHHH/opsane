# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path

from PyInstaller.utils.hooks import (
    collect_data_files,
    collect_submodules,
    copy_metadata,
)


ROOT = Path(SPECPATH).resolve().parents[1]
ENTRY = ROOT / "packaging" / "windows" / "portable_entry.py"
ICON = ROOT / "shell_agent" / "web" / "static" / "assets" / "favicon-32.png"

datas = [
    (
        str(ROOT / "shell_agent" / "web" / "static"),
        "shell_agent/web/static",
    ),
]
for package_dir in ("domains", "runbooks"):
    source_dir = ROOT / "shell_agent" / package_dir
    for source_file in source_dir.glob("*.py"):
        datas.append(
            (
                str(source_file),
                f"shell_agent/{package_dir}",
            )
        )
for swift_file in (ROOT / "shell_agent" / "attachments").glob("*.swift"):
    datas.append((str(swift_file), "shell_agent/attachments"))
datas += collect_data_files("certifi")

for distribution in ("shell-agent", "openai", "fastapi", "pydantic", "uvicorn"):
    try:
        datas += copy_metadata(distribution)
    except Exception:
        pass

hiddenimports = collect_submodules("shell_agent")
hiddenimports += [
    "multipart.multipart",
    "uvicorn.lifespan.on",
    "uvicorn.logging",
    "uvicorn.loops.auto",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets.auto",
]

a = Analysis(
    [str(ENTRY)],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["pytest", "tkinter"],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Opsane",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(ICON),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="Opsane",
)
