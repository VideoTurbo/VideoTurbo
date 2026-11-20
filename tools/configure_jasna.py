"""在 config/worker.yaml 中启用 Jasna 影片修复（deploy/worker/linux/worker.sh install restore 调用）。

只用标准库，按行改写 ``jasna:`` 段，保留文件中其他配置和注释。
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

BLOCK_HEADER = re.compile(r"^jasna:[ \t]*(#.*)?$", re.MULTILINE)
DEFAULT_BLOCK = """
# 影片修复（Jasna）。由 deploy/worker/linux/worker.sh install restore 写入，可手动修改。
jasna:
  enabled: {enabled}
  # null 使用 <项目>/jasna/current/jasna。
  executable: null
  # 许可证激活信息（XDG_CONFIG_HOME）；null 使用 <项目>/jasna/config。不要分享该目录。
  config_home: null
  # 已用许可证激活后设为 true，才会领取 LTX 系列任务。
  ltx: {ltx}
  # LTX 快速模式（--ltx-fast）：约快 1.4 倍、细节略少，仅 RTX 50 系可用；开启后运行 worker.sh self-test restore 下载约 8 GB 模型。
  ltx_fast: false
  # 显存不足时调小，例如 60；null 使用 Jasna 默认 90。
  max_clip_size: null
  # 每部影片最长运行小时数。
  timeout_hours: 48
  # 回传分块大小（MB）。
  upload_chunk_mb: 64
  # 下载原片的连接数（1–32），占用 NAS 家宽上行；1 为单连接。
  download_connections: 16
  # 回传结果的连接数（1–32），占用云服务器上行和 NAS 下行；1 为单连接。
  upload_connections: 32
  # 修复当前影片时预取下一部原片、后台回传上一部结果；本机磁盘需容纳约 3 部影片。
  pipeline: true
  # 中断任务的本地文件保留小时数，期间同一任务回到本机可续传。
  keep_partial_hours: 72
"""


def _set_key(block: str, key: str, value: str) -> str:
    pattern = re.compile(rf"^([ \t]+{key}:)[ \t]*[^\n#]*?([ \t]+#.*)?$", re.MULTILINE)
    if pattern.search(block):
        return pattern.sub(lambda m: f"{m.group(1)} {value}{m.group(2) or ''}", block, count=1)
    return block.rstrip("\n") + f"\n  {key}: {value}\n"


def update(text: str, *, enabled: bool, ltx: bool | None) -> str:
    match = BLOCK_HEADER.search(text)
    if not match:
        return text.rstrip("\n") + "\n" + DEFAULT_BLOCK.format(
            enabled=str(enabled).lower(), ltx=str(bool(ltx)).lower()
        )
    start = match.end()
    following = re.search(r"^\S", text[start + 1 :], re.MULTILINE)
    end = start + 1 + following.start() if following else len(text)
    block = text[start:end]
    block = _set_key(block, "enabled", str(enabled).lower())
    if ltx is not None:
        block = _set_key(block, "ltx", str(ltx).lower())
    if not block.endswith("\n"):
        block += "\n"
    return text[:start] + block + text[end:]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--disable", action="store_true")
    parser.add_argument("--ltx", choices=["true", "false"])
    args = parser.parse_args()
    if not args.config.is_file():
        print(f"找不到 {args.config}，请先运行 bash deploy/worker/linux/worker.sh install。", file=sys.stderr)
        return 1
    original = args.config.read_text(encoding="utf-8")
    updated = update(
        original,
        enabled=not args.disable,
        ltx=None if args.ltx is None else args.ltx == "true",
    )
    if updated != original:
        args.config.write_text(updated, encoding="utf-8")
    print(f"已更新 {args.config} 的 jasna 配置。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
