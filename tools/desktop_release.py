"""整理桌面版发布文件：改成不含空格的文件名，生成两个应用的更新清单。只用标准库。

发布流程（.github/workflows/release.yml）下载桌面版构建（desktop.yml）上传的产物后运行：

    python tools/desktop_release.py --artifacts artifacts --tag v1.2.0 --output dist/desktop

输出（都上传到 GitHub Release）：

- ``VideoTurbo_<版本>_x64-setup.exe``：Windows 主程序应用
- ``VideoTurbo-Worker_<版本>_x64-setup.exe``：Windows 识别机应用
- ``VideoTurbo_<版本>_aarch64.dmg``：macOS 主程序应用
- ``VideoTurbo_<版本>_aarch64.app.tar.gz``：macOS 自动更新包
- ``latest.json`` / ``latest-worker.json``：两个应用的更新清单，应用从
  ``releases/latest/download/`` 读取（tauri.conf.json 的 plugins.updater.endpoints）

更新包的签名（``.sig``）由构建时的私钥生成，写进更新清单；应用用公钥校验。
"""

from __future__ import annotations

import argparse
import json
import shutil
from datetime import UTC, datetime
from pathlib import Path


def _find(artifacts: Path, pattern: str) -> Path:
    matches = sorted(artifacts.rglob(pattern))
    if len(matches) != 1:
        raise SystemExit(f"产物里应有且只有一个 {pattern}，实际找到 {len(matches)} 个。")
    return matches[0]


def collect(artifacts: Path, tag: str, repo: str, output: Path) -> dict[str, dict]:
    version = tag.removeprefix("v")
    output.mkdir(parents=True, exist_ok=True)
    base = f"https://github.com/{repo}/releases/download/{tag}"

    def publish(pattern: str, name: str, signed: bool) -> tuple[str, str]:
        source = _find(artifacts, pattern)
        shutil.copyfile(source, output / name)
        if not signed:
            return name, ""
        signature = Path(f"{source}.sig")
        if not signature.is_file():
            raise SystemExit(f"{source.name} 缺少签名 {signature.name}：发布构建要带上更新签名私钥。")
        return name, signature.read_text(encoding="utf-8").strip()

    main_exe = publish(f"VideoTurbo_{version}_x64-setup.exe", f"VideoTurbo_{version}_x64-setup.exe", True)
    worker_exe = publish(
        f"VideoTurbo Worker_{version}_x64-setup.exe", f"VideoTurbo-Worker_{version}_x64-setup.exe", True
    )
    publish(f"VideoTurbo_{version}_aarch64.dmg", f"VideoTurbo_{version}_aarch64.dmg", False)
    mac_update = publish("VideoTurbo.app.tar.gz", f"VideoTurbo_{version}_aarch64.app.tar.gz", True)

    now = datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
    notes = f"VideoTurbo {tag}：https://github.com/{repo}/releases/tag/{tag}"

    def manifest(platforms: dict[str, tuple[str, str]]) -> dict:
        return {
            "version": version,
            "notes": notes,
            "pub_date": now,
            "platforms": {
                key: {"signature": signature, "url": f"{base}/{name}"} for key, (name, signature) in platforms.items()
            },
        }

    manifests = {
        "latest.json": manifest({"windows-x86_64": main_exe, "darwin-aarch64": mac_update}),
        "latest-worker.json": manifest({"windows-x86_64": worker_exe}),
    }
    for name, content in manifests.items():
        (output / name).write_text(json.dumps(content, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifests


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--artifacts", type=Path, required=True, help="下载下来的桌面版构建产物")
    parser.add_argument("--tag", required=True, help="版本标签，例如 v1.2.0")
    parser.add_argument("--repo", default="VideoTurbo/VideoTurbo")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    collect(args.artifacts, args.tag, args.repo, args.output)
    for path in sorted(args.output.iterdir()):
        print(path.name)


if __name__ == "__main__":
    main()
