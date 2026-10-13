# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：生成 onedir 形态的桌面程序。

在项目根目录执行：

    uv run --with pyinstaller pyinstaller packaging/me31_scope.spec --noconfirm --clean

产物为 ``dist/me31-scope/`` 目录（Windows 下里面有 ``me31-scope.exe``）。
整个目录拷到目标机器上双击 exe 即可运行，目标机器无需安装 Python。

注意：PyInstaller 不支持交叉编译，Windows 包必须在 Windows 上构建
（见 ``.github/workflows/build-windows.yml``）。
"""

from pathlib import Path

PROJECT_ROOT = Path(SPECPATH).resolve().parent
APP_NAME = "me31-scope"

# PySide6 完整安装约 1 GB，这里把用不到的 Qt 模块从依赖图里摘掉。
# 程序真正用到的只有 QtCore / QtGui / QtWidgets / QtSerialPort，
# 另外 QtSvg 留给 pyqtgraph 导出 SVG 时按需使用。
EXCLUDES = [
    # pyqtgraph 会尝试导入这些备选 Qt 绑定，本机装的是 PySide6
    "PyQt5",
    "PyQt6",
    "PySide2",
    # 体积大且与本程序无关的 Qt 模块
    "PySide6.Qt3DAnimation",
    "PySide6.Qt3DCore",
    "PySide6.Qt3DExtras",
    "PySide6.Qt3DInput",
    "PySide6.Qt3DLogic",
    "PySide6.Qt3DRender",
    "PySide6.QtBluetooth",
    "PySide6.QtCharts",
    "PySide6.QtDataVisualization",
    "PySide6.QtDesigner",
    "PySide6.QtGraphs",
    "PySide6.QtGraphsWidgets",
    "PySide6.QtHelp",
    "PySide6.QtHttpServer",
    "PySide6.QtLabsStyleKit",
    "PySide6.QtLocation",
    "PySide6.QtMultimedia",
    "PySide6.QtMultimediaWidgets",
    "PySide6.QtNetworkAuth",
    "PySide6.QtNfc",
    "PySide6.QtPdf",
    "PySide6.QtPdfWidgets",
    "PySide6.QtPositioning",
    "PySide6.QtQml",
    "PySide6.QtQmlFeatures",
    "PySide6.QtQuick",
    "PySide6.QtQuick3D",
    "PySide6.QtQuickControls2",
    "PySide6.QtQuickTest",
    "PySide6.QtQuickWidgets",
    "PySide6.QtRemoteObjects",
    "PySide6.QtScxml",
    "PySide6.QtSensors",
    "PySide6.QtSerialBus",
    "PySide6.QtSpatialAudio",
    "PySide6.QtSql",
    "PySide6.QtStateMachine",
    "PySide6.QtTest",
    "PySide6.QtTextToSpeech",
    "PySide6.QtUiTools",
    "PySide6.QtWebChannel",
    "PySide6.QtWebEngineCore",
    "PySide6.QtWebEngineQuick",
    "PySide6.QtWebEngineWidgets",
    "PySide6.QtWebSockets",
    "PySide6.QtWebView",
    "PySide6.QtXml",
    # 用不到的标准库 / 第三方
    "tkinter",
    "pytest",
]

a = Analysis(
    [str(Path(SPECPATH).resolve() / "launcher.py")],
    pathex=[str(PROJECT_ROOT / "src")],
    binaries=[],
    datas=[],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=EXCLUDES,
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=APP_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,  # GUI 程序不弹黑色控制台窗口
    disable_windowed_traceback=False,  # 崩溃时仍然弹窗提示，方便现场排查
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
    name=APP_NAME,
)
