# PyInstaller 配置：把主程序打包成文件夹（onedir），由桌面版外壳作为子进程启动。
# 由 deploy/desktop/build_sidecar.py 调用，不要直接运行。
#
# - 单文件模式每次启动都要先解压，慢且占临时目录，所以用文件夹模式。
# - 打包成控制台程序：外壳在 Windows 上以“不显示窗口”启动它，它再启动 ffmpeg 时就不会弹出黑窗口。
# - 插件在主程序进程内运行，只能导入打包进来的模块，所以收入全部标准库（去掉图形界面、测试等用不到的部分）。
import importlib.util
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = Path(SPECPATH).resolve().parents[1]  # noqa: F821  SPECPATH 由 PyInstaller 提供

EXCLUDED_STDLIB = {
    "tkinter", "turtle", "turtledemo", "idlelib", "test", "unittest", "lib2to3", "ensurepip",
    "venv", "pydoc_data", "distutils", "msilib", "curses", "dbm", "readline", "antigravity", "this",
}
# 只收本平台存在的模块（winsound 只在 Windows，spwd 只在 Linux 等）。
stdlib = sorted(
    name for name in sys.stdlib_module_names
    if not name.startswith("_") and name not in EXCLUDED_STDLIB and importlib.util.find_spec(name)
)

hiddenimports = [
    *stdlib,
    # GPU 机器的代码（video_turbo.worker）不在主程序里运行；本机识别另装运行环境。
    *collect_submodules(
        "video_turbo",
        filter=lambda name: name != "video_turbo.worker" and not name.startswith("video_turbo.worker."),
    ),
    *collect_submodules("uvicorn"),
    *collect_submodules("asyncssh"),
    # 内置 Samba 来源在用到时才导入 smbclient；spnego 按认证方式动态加载模块。
    *collect_submodules("smbclient"),
    *collect_submodules("smbprotocol"),
    *collect_submodules("spnego"),
]

a = Analysis(  # noqa: F821
    [str(Path(SPECPATH) / "server_entry.py")],  # noqa: F821
    pathex=[str(ROOT / "src")],
    datas=collect_data_files("video_turbo", excludes=["**/__pycache__/**"]),
    hiddenimports=hiddenimports,
    excludes=[*EXCLUDED_STDLIB, "video_turbo.worker"],
    noarchive=False,
)
pyz = PYZ(a.pure)  # noqa: F821
exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    # 打包后的程序不读 PYTHON* 环境变量，UTF-8 模式要在这里打开：读写文件不受 Windows 系统代码页影响。
    [("X utf8_mode=1", None, "OPTION")],
    exclude_binaries=True,
    name="videoturbo-server",
    console=True,
    upx=False,
)
COLLECT(  # noqa: F821
    exe,
    a.binaries,
    a.datas,
    name="videoturbo-server",
    upx=False,
)
