"""从 uv.lock 导出显卡电脑安装用的依赖清单（精确版本 + 哈希），提交在 deploy/worker/requirements/。

显卡电脑上没有 uv，安装脚本用 ``pip install --require-hashes -r <清单>`` 安装，与 CI 测试的版本一致。
清单带平台标记（例如 Windows 与 Linux 的 CUDA 库版本不同），一份清单适用于所有平台。

修改依赖（pyproject.toml / uv.lock）后运行：

    uv run python tools/export_requirements.py

CI 用 ``--check`` 检查清单与 uv.lock 是否一致。
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
OUTPUT_DIR = PROJECT_ROOT / "deploy" / "worker" / "requirements"
# 清单文件名 → pyproject.toml 中的可选依赖组。
PROFILES = {
    "asr.txt": "worker",
    "restore.txt": "restore",
}
HEADER = (
    "# 由 tools/export_requirements.py 从 uv.lock 生成，不要手动修改。\n"
    "# 依赖组：{extra}。显卡电脑安装脚本用 pip install --require-hashes -r 安装。\n"
)


def export(extra: str) -> str:
    uv = shutil.which("uv")
    if uv is None:
        raise SystemExit("找不到 uv，请先安装：https://docs.astral.sh/uv/")
    result = subprocess.run(
        [
            uv, "export", "--frozen", "--no-dev", "--no-header", "--no-emit-project",
            "--extra", extra,
        ],
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return HEADER.format(extra=extra) + result.stdout


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="只检查清单是否与 uv.lock 一致，不写文件")
    args = parser.parse_args(argv)
    stale = []
    for name, extra in PROFILES.items():
        path = OUTPUT_DIR / name
        content = export(extra)
        current = path.read_text(encoding="utf-8") if path.is_file() else None
        if current == content:
            continue
        if args.check:
            stale.append(path.relative_to(PROJECT_ROOT).as_posix())
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8", newline="\n")
            print(f"已更新 {path.relative_to(PROJECT_ROOT).as_posix()}")
    if stale:
        print(
            f"依赖清单与 uv.lock 不一致：{'、'.join(stale)}。"
            "请运行 uv run python tools/export_requirements.py 后提交。",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
