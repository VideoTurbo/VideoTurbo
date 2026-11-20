"""Create or update config/worker.yaml interactively during Worker installs.

Only the standard library is used: the installer runs this with the system
Python before any virtual environment or PyYAML exists. Values are rewritten
line by line so the template's comments and the remaining settings stay intact.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from pathlib import Path

# Top-level keys start at column 0; download_root lives under the asr section.
FIELDS = {
    "server_url": re.compile(r"^(server_url:)[ \t]*(.*)$", re.MULTILINE),
    "worker_token": re.compile(r"^(worker_token:)[ \t]*(.*)$", re.MULTILINE),
    "download_root": re.compile(r"^([ \t]+download_root:)[ \t]*(.*)$", re.MULTILINE),
}


def _scalar(raw: str) -> str:
    raw = raw.strip()
    if raw[:1] == '"':
        try:
            value, _ = json.JSONDecoder().raw_decode(raw)
            return str(value)
        except ValueError:
            return raw.strip('"')
    if raw[:1] == "'":
        end = raw.find("'", 1)
        return raw[1:end] if end > 0 else raw.strip("'")
    value = raw.split(" #", 1)[0].strip()
    return "" if value in {"null", "~"} else value


def read_values(text: str) -> dict[str, str]:
    values = {}
    for key, pattern in FIELDS.items():
        match = pattern.search(text)
        values[key] = _scalar(match.group(2)) if match else ""
    return values


def write_values(text: str, values: dict[str, str]) -> str:
    for key, value in values.items():
        pattern = FIELDS[key]
        # JSON strings are valid YAML double-quoted scalars, so any token is safe.
        line = json.dumps(value, ensure_ascii=False)
        # Callable replacements keep backslashes in values literal.
        if pattern.search(text):
            text = pattern.sub(lambda match, line=line: f"{match.group(1)} {line}", text, count=1)
        elif key == "download_root":
            if re.search(r"^asr:[ \t]*$", text, re.MULTILINE):
                text = re.sub(r"^(asr:[ \t]*)$",
                              lambda match, line=line: f"{match.group(1)}\n  download_root: {line}",
                              text, count=1, flags=re.MULTILINE)
            else:
                text = text.rstrip("\n") + f"\nasr:\n  download_root: {line}\n"
        else:
            text = f"{key}: {line}\n" + text
    return text


_IDLE_ENABLED = re.compile(
    r"^(idle:[ \t]*(?:\r?\n(?:[ \t]+#.*|[ \t]*))*\r?\n[ \t]+enabled:)[ \t]*false", re.MULTILINE
)


def enable_idle_detection(text: str) -> str:
    """把 idle.enabled 改为 true：与桌面、游戏共用显卡的电脑默认只在空闲时接单。"""
    return _IDLE_ENABLED.sub(lambda match: f"{match.group(1)} true", text, count=1)


def missing_required(values: dict[str, str]) -> list[str]:
    missing = []
    if not values["server_url"]:
        missing.append("server_url")
    if not values["worker_token"]:
        missing.append("worker_token")
    return missing


def _ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    try:
        answer = input(f"{prompt}{suffix}: ").strip()
    except EOFError:
        answer = ""
    return answer or default


def prompt_values(values: dict[str, str], default_root: str) -> dict[str, str]:
    result = dict(values)
    print("\n请填写本机的服务配置；方括号内为默认值，直接回车即可使用。", flush=True)

    default = values["server_url"]
    while True:
        url = _ask("主程序地址 server_url（例如 http://192.168.1.10:8765）", default)
        if re.match(r"^https?://\S+$", url):
            result["server_url"] = url.rstrip("/")
            break
        print("  地址需以 http:// 或 https:// 开头。", flush=True)

    while True:
        hint = "（已设置，回车保留）" if values["worker_token"] else "（从主程序控制台“设置 → 云主机 → 接入密钥”复制）"
        token = _ask(f"接入密钥 worker_token{hint}")
        token = token or values["worker_token"]
        if token:
            result["worker_token"] = token
            break
        print("  worker_token 必填。", flush=True)

    result["download_root"] = _ask("模型下载目录 download_root", values["download_root"] or default_root)
    return result


def resolve_root(config: Path, value: str, default_root: str) -> Path:
    root = Path(value or default_root).expanduser()
    return root if root.is_absolute() else (config.parent / root).resolve()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="交互式填写本机的服务配置")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--template", type=Path, required=True)
    parser.add_argument("--default-download-root", required=True)
    parser.add_argument(
        "--shared-machine", action="store_true",
        help="新建配置时开启空闲检测（与日常使用共用显卡的电脑，Windows 安装脚本使用）",
    )
    parser.add_argument(
        "--non-interactive", action="store_true",
        help="不提问，仅在配置缺失时复制模板并提示需要补填的字段",
    )
    parser.add_argument(
        "--apply-json", type=Path,
        help="不提问，从 JSON 文件写入 server_url、worker_token（主程序自动安装时使用）",
    )
    parser.add_argument(
        "--download-root",
        help="与 --apply-json 一起使用：把模型目录写成这个值（主程序自动安装时使用）",
    )
    parser.add_argument(
        "--print-download-root", action="store_true",
        help="只输出配置中的模型目录（绝对路径），不修改配置",
    )
    args = parser.parse_args(argv)
    config = args.config

    if args.print_download_root:
        text = config.read_text(encoding="utf-8") if config.is_file() else ""
        print(resolve_root(config, read_values(text)["download_root"], args.default_download_root))
        return 0

    if not config.is_file():
        shutil.copyfile(args.template, config)
        # 模板里的模型目录是通用的相对路径；新建配置时换成本平台安装脚本给出的默认目录。
        created = write_values(
            config.read_text(encoding="utf-8"), {"download_root": args.default_download_root}
        )
        if args.shared_machine:
            created = enable_idle_detection(created)
        config.write_text(created, encoding="utf-8")
        print(f"已创建 {config}", flush=True)
    text = config.read_text(encoding="utf-8")
    values = read_values(text)

    if args.apply_json:
        payload = json.loads(args.apply_json.read_text(encoding="utf-8"))
        if args.download_root:
            payload["download_root"] = args.download_root
        changed = {
            key: str(payload[key]).strip()
            for key in ("server_url", "worker_token", "download_root")
            if payload.get(key) and str(payload[key]).strip() != values[key]
        }
        if changed:
            config.write_text(write_values(text, changed), encoding="utf-8")
        print(f"已写入 {config}：{'、'.join(changed) or '无变化'}", flush=True)
        return 0

    missing = missing_required(values)

    if args.non_interactive or not sys.stdin.isatty():
        if missing:
            print(f"提示：{config} 尚未填写 {'、'.join(missing)}，启动前请补填。", flush=True)
        return 0

    if not missing:
        answer = _ask(f"{config} 已填写 server_url 和 worker_token，是否修改？(y/N)", "N")
        if answer.lower() not in {"y", "yes"}:
            return 0

    updated = prompt_values(values, args.default_download_root)
    changed = {key: value for key, value in updated.items() if value != values[key]}
    if changed:
        config.write_text(write_values(text, changed), encoding="utf-8")
    print(f"已保存 {config}（download_root={updated['download_root']}）", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
