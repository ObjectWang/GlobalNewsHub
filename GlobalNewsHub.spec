# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for GlobalNewsHub (PRD task P5.1).

onedir (not onefile): section 1.3 requires cold start <= 3 s, which a
self-extracting onefile bundle of ~300 MB cannot meet. Layout of the
resulting dist/GlobalNewsHub/:

    GlobalNewsHub.exe
    _internal/               <- python + native deps
    config/*.yaml            <- settings/sources templates
    ui/themes/*.qss          <- stylesheets
    resources/
        rsshub-server.exe    <- embedded RSSHub (P2.1 binary)
        models/              <- INT8 ONNX + tokenizer
        icons/
    data/                    <- created at runtime next to the exe

Build:  pyinstaller GlobalNewsHub.spec --noconfirm
Smoke:  set QT_QPA_PLATFORM=offscreen && dist\\GlobalNewsHub\\GlobalNewsHub.exe --smoke
"""

a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=[],
    datas=[
        ("config", "config"),
        ("ui/themes", "ui/themes"),
        ("resources/models", "resources/models"),
        ("resources/icons", "resources/icons"),
        ("resources/rsshub-server.exe", "resources"),
        # Schema migrations are runtime data consumed by
        # core.storage.database.Database.initialize via _MEIPASS.
        ("core/storage/migrations", "core/storage/migrations"),
    ],
    hiddenimports=[
        # APScheduler loads scheduler/trigger classes dynamically.
        "apscheduler.schedulers.background",
        "apscheduler.triggers.interval",
        "apscheduler.triggers.cron",
        # PySide6 platform/plugins are auto-collected, but keep the SVG
        # plugin explicit for themed icons.
        "PySide6.QtSvg",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Training-only heavyweights must never leak into the runtime
        # bundle (section 1.2; install size <= 300 MB, section 1.3).
        "torch",
        "torchvision",
        "torchaudio",
        "scipy",
        "pandas",
        "matplotlib",
        "tkinter",
        "IPython",
        "jupyter",
        "pytest",
        "PyQt5",
        "PyQt6",
        # Tokenizer loading falls back to the `tokenizers` adapter
        # (core.classifier.bert_classifier._load_tokenizer), so the whole
        # HF stack is dead weight at runtime (~55 MB + cold-start cost).
        "transformers",
        "huggingface_hub",
        "hf_xet",
        "sklearn",
        "sympy",
        "networkx",
    ],
    noarchive=False,
)

# --- prune dead weight from the bundle TOCs --------------------------------
# Pure-QtWidgets app: QML/Quick/Pdf/OpenGL stacks, software OpenGL, Qt
# network stack (aiohttp owns networking) and .qm translations are never
# loaded. Filtering the TOCs here is authoritative — a post-COLLECT
# filesystem prune would race PyInstaller's writer.
_PRUNE_NEEDLES = (
    "qt6quick",
    "qt6qml",
    "qt6pdf",
    "qt6opengl",
    "qt6network",
    "qtnetwork.pyd",
    "opengl32sw",
    "translations",
)


def _drop(toc: list) -> list:
    """Remove TOC entries whose destination path matches a needle."""
    return [
        entry
        for entry in toc
        if not any(needle in entry[0].lower() for needle in _PRUNE_NEEDLES)
    ]


a.datas = _drop(a.datas)
a.binaries = _drop(a.binaries)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="GlobalNewsHub",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,  # UPX trips antivirus false positives (section 10)
    console=False,  # windowed desktop app
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
    name="GlobalNewsHub",
)


