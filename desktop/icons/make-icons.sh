#!/usr/bin/env bash
# 重新生成两个应用的图标（需要 macOS：swiftc、iconutil）：
#   bash desktop/icons/make-icons.sh
# 主程序写到 desktop/apps/videoturbo/icons，VideoTurbo Worker（带显卡角标）写到 desktop/apps/worker/icons。
# Windows 的 .ico 和各尺寸 PNG 用较窄的留白，macOS 的 .icns 按系统图标规范留白。托盘的 tray.png 不在这里生成。
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
apps="$here/../apps"
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
swiftc -O -o "$work/draw" "$here/draw-icon.swift"

render() { # 输出目录 角标 留白
  mkdir -p "$1"
  for size in 16 24 32 48 64 128 256 512 1024; do
    flag=""
    [ "$size" -le 32 ] && flag=small
    VT_BADGE=$2 VT_MARGIN=$3 "$work/draw" "$1/$size.png" "$size" $flag >/dev/null
  done
}

for pair in "videoturbo 0" "worker 1"; do
  set -- $pair
  app=$1 badge=$2 out="$apps/$1/icons"
  render "$work/$app-win" "$badge" 56
  render "$work/$app-mac" "$badge" 100
  mkdir -p "$out"
  cp "$work/$app-win/32.png" "$out/32x32.png"
  cp "$work/$app-win/64.png" "$out/64x64.png"
  cp "$work/$app-win/128.png" "$out/128x128.png"
  cp "$work/$app-win/256.png" "$out/128x128@2x.png"
  cp "$work/$app-win/512.png" "$out/icon.png"
  cp "$work/$app-mac/256.png" "$apps/$app/ui/logo.png"
  iconset="$work/$app.iconset"
  mkdir -p "$iconset"
  for spec in "16 16" "16@2x 32" "32 32" "32@2x 64" "128 128" "128@2x 256" "256 256" "256@2x 512" "512 512" "512@2x 1024"; do
    set -- $spec
    name=${1%@2x} suffix=${1#"$name"}
    cp "$work/$app-mac/$2.png" "$iconset/icon_${name}x${name}${suffix}.png"
  done
  iconutil -c icns -o "$out/icon.icns" "$iconset"
  # .ico：每个尺寸一张 PNG。
  python3 - "$work/$app-win" "$out/icon.ico" <<'PY'
import struct, sys
from pathlib import Path
source, target = Path(sys.argv[1]), Path(sys.argv[2])
images = [(size, (source / f"{size}.png").read_bytes()) for size in (16, 24, 32, 48, 64, 128, 256)]
entries, body, offset = b"", b"", 6 + 16 * len(images)
for size, data in images:
    side = 0 if size >= 256 else size
    entries += struct.pack("<BBBBHHII", side, side, 0, 0, 1, 32, len(data), offset + len(body))
    body += data
target.write_bytes(struct.pack("<HHH", 0, 1, len(images)) + entries + body)
PY
  echo "已生成 $out"
done
