"""控制台页面。

前端源码在仓库根目录的 ``web/``（Vue 3 + TypeScript + Vite + Naive UI），
``npm --prefix web run build`` 把产物输出到 ``static/dashboard/``，主控把它当作静态文件提供。
产物不进版本库：Docker 镜像在构建阶段生成，本地从源码运行前需要先构建一次。
"""

from pathlib import Path

from fastapi.responses import FileResponse, HTMLResponse, Response

DIST_DIR = Path(__file__).with_name("static") / "dashboard"
ASSETS_DIR = DIST_DIR / "assets"

FAVICON_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
    '<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">'
    '<stop offset="0" stop-color="#9097ff"/><stop offset="1" stop-color="#22d3ee"/>'
    "</linearGradient></defs>"
    '<rect width="64" height="64" rx="14" fill="#141418"/>'
    '<path fill="url(#g)" d="M9 54V29l12 8v-8l12 8v-8l12 8V12h10v42z"/>'
    '<rect x="15" y="44" width="20" height="4" rx="2" fill="#141418"/>'
    '<rect x="38" y="44" width="10" height="4" rx="2" fill="#141418"/>'
    "</svg>"
)

NOT_BUILT_HTML = (
    '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>VideoTurbo</title>'
    '<body style="font:14px/1.7 system-ui;max-width:560px;margin:80px auto;padding:0 20px">'
    "<h1>控制台前端尚未构建</h1>"
    "<p>主程序已正常运行，但没有找到控制台页面的构建产物。在项目根目录执行：</p>"
    "<pre>npm --prefix web ci\nnpm --prefix web run build</pre>"
    "<p>然后刷新本页。使用 Docker 部署时镜像会自动构建，无需手动执行。</p>"
    "</body></html>"
)


def dashboard_response() -> Response:
    """返回控制台入口页。每次请求时读取，前端重新构建后无需重启主控。"""
    index = DIST_DIR / "index.html"
    if not index.is_file():
        return HTMLResponse(NOT_BUILT_HTML, status_code=503)
    # 入口页不缓存，才能在升级后立即引用到带新哈希的脚本和样式。
    return FileResponse(index, media_type="text/html", headers={"Cache-Control": "no-cache"})
