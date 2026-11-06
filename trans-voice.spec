# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec — one onedir build carrying both entry points.

    .venv\\Scripts\\python.exe -m pip install -e ".[build]"
    .venv\\Scripts\\python.exe -m PyInstaller trans-voice.spec --noconfirm

Produces ``dist/trans-voice/``:

* ``trans-voice-gui.exe`` — windowed control panel, no console window;
* ``trans-voice.exe`` — console pipeline, same CLI as ``python -m pipeline.app``;
* ``config.ini`` / ``speaker.wav`` — copied next to the executables *after* the
  build. They stay real, editable files rather than bundled resources, because
  ``config.py`` resolves both against the executable's directory when frozen.

Native libraries are collected by hand: sounddevice and soundfile load
PortAudio / libsndfile from sibling top-level data packages
(``_sounddevice_data``, ``_soundfile_data``) through a runtime path join that
static analysis cannot follow.
"""

from pathlib import Path

from PyInstaller.utils.hooks import collect_dynamic_libs

ROOT = Path(SPECPATH)
OUT_NAME = "DTV"

binaries = (
    collect_dynamic_libs("_sounddevice_data")   # portaudio*.dll
    + collect_dynamic_libs("_soundfile_data")   # libsndfile*.dll
)
hiddenimports = ["_sounddevice_data", "_soundfile_data"]

a_gui = Analysis(
    [str(ROOT / "pipeline" / "gui.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    hiddenimports=hiddenimports,
    excludes=["pytest", "_pytest", "setuptools", "pip"],
)
a_cli = Analysis(
    [str(ROOT / "pipeline" / "app.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    hiddenimports=hiddenimports,
    excludes=["pytest", "_pytest", "setuptools", "pip"],
)

pyz_gui = PYZ(a_gui.pure)
pyz_cli = PYZ(a_cli.pure)

exe_gui = EXE(
    pyz_gui,
    a_gui.scripts,
    [],
    exclude_binaries=True,
    name=f"{OUT_NAME}-gui",
    console=False,
)
exe_cli = EXE(
    pyz_cli,
    a_cli.scripts,
    [],
    exclude_binaries=True,
    name=OUT_NAME,
    console=True,
)

coll = COLLECT(
    exe_gui,
    exe_cli,
    a_gui.binaries,
    a_gui.datas,
    a_cli.binaries,
    a_cli.datas,
    name=OUT_NAME,
)

# Editable app files: written by the GUI's "save", read at startup, therefore
# never read-only assets inside the bundle. A pre-existing file wins, so
# rebuilding does not clobber the user's settings.
for asset in ("config.ini", "speaker.wav"):
    src = ROOT / asset
    dst = Path(DISTPATH) / OUT_NAME / asset
    if src.is_file() and not dst.exists():
        dst.write_bytes(src.read_bytes())
