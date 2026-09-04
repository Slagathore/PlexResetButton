# -*- mode: python ; coding: utf-8 -*-
# =============================================================================
# Sensarr-linux.spec — Task H item 8
# =============================================================================
# The Linux build. MUST be built ON Linux (PyInstaller is not a
# cross-compiler) — CI does this in .github/workflows/linux-smoke.yml and
# runs the artifact under Xvfb with --smoke-test.
#
# Differences from the Windows specs (which stay Windows-specific):
#   - no uac_admin (a UAC manifest means nothing on Linux; the app never
#     elevates there)
#   - no .ico icon (Windows resource format)
#   - the bundled torrent_runner is the read-only seed copy; the writable
#     one (with node_modules) lives in the XDG data dir per app_paths.
# =============================================================================

from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules


spec_file = globals().get("__file__")
project_dir = Path(spec_file).resolve().parent if spec_file else Path.cwd().resolve()

datas = []
# sv-ttk ships its Sun Valley theme as Tcl data files; without collecting them
# the dark theme silently falls back to the stock gray ttk look in the build.
datas += collect_data_files("sv_ttk")
# rank-torrent-name + its parser parsett carry regex/pattern data files that
# the analysis misses; collect them so the selection engine parses in the build.
datas += collect_data_files("RTN")
# "PTT" is the import name of the parsett distribution; its keywords/*.txt are
# read on every parse, so collecting only "parsett" ships a parser that cannot
# parse anything (see rtn_compat).
datas += collect_data_files("PTT")
datas += collect_data_files("parsett")
# The Node webtorrent downloader: ship the script + manifests as the
# read-only seed. node_modules is NOT bundled — the app seeds a writable
# copy under the XDG data dir and `npm install` runs there (Node.js 20+
# must be on PATH).
for _rf in ("download.mjs", "package.json", "package-lock.json", "diag.mjs"):
    _src = project_dir / "torrent_runner" / _rf
    if _src.is_file():
        datas.append((str(_src), "torrent_runner"))

hiddenimports = (
    collect_submodules("telegram")
    + collect_submodules("pystray")
    + collect_submodules("RTN")
    + collect_submodules("PTT")
    + collect_submodules("parsett")
    + collect_submodules("pydantic")
    + ["pydantic_core", "orjson", "Levenshtein", "arrow", "pymediainfo"]
    # New modules are imported dynamically enough that we pin them explicitly.
    + ["sv_ttk", "send2trash", "shows_tab", "shows_store", "show_tracker",
       "downloads_store", "download_manager", "torrent_search", "torrent_routing",
       "auth_store", "db", "ui_helpers", "health", "watchlist_tab", "video_quality",
       "subtitles", "anime_db", "media_identity",
       "app_paths", "platform_adapter", "legacy_migration"]
)

# Same heavyweight excludes as the Windows spec — none are Sensarr deps.
_EXCLUDE_HEAVY = [
    "torch", "torchvision", "torchaudio", "torchao", "triton",
    "tensorflow", "tensorflow-plugins", "keras", "transformers", "timm",
    "sklearn", "scipy", "pandas", "matplotlib", "numpy", "numba", "llvmlite",
    "moviepy", "imageio", "imageio_ffmpeg", "librosa", "soundfile", "nltk",
    "onnxruntime", "cv2", "h5py", "boto3", "botocore", "duckdb", "sqlalchemy",
    "lxml", "openpyxl", "IPython", "jedi", "parso", "black", "blib2to3",
    "pytest", "_pytest", "py", "uvicorn", "websockets", "keyring", "fsspec",
    "lz4", "dns", "pythonnet", "clr_loader", "win32com",
]

a = Analysis(
    ["main.py"],
    pathex=[str(project_dir)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=_EXCLUDE_HEAVY,
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Sensarr",
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
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="Sensarr",
)
