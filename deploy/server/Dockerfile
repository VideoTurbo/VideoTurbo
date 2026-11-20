# 第一阶段：构建控制台前端（Vue 3 + Vite），产物输出到 Python 包内的 static/dashboard/。
# 前端产物与 CPU 架构无关：多架构构建时只在构建机的原生架构上编译一次。
FROM --platform=$BUILDPLATFORM node:22-bookworm-slim AS web
WORKDIR /build/web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web ./
RUN npm run build

# 第二阶段：从 uv.lock 导出主程序依赖的精确版本（带哈希），镜像与 CI 测试使用同一组依赖。
# 导出结果带平台标记，在构建机的原生架构上导出一次即可。
FROM --platform=$BUILDPLATFORM python:3.11-slim-bookworm AS deps
COPY --from=ghcr.io/astral-sh/uv:0.9.29 /uv /usr/local/bin/uv
WORKDIR /build
COPY pyproject.toml uv.lock ./
RUN uv export --frozen --no-dev --extra orchestrator --no-emit-project -o requirements.txt

FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    VIDEO_TURBO_DEPLOYMENT=docker \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
# 先装依赖再复制源码：只改代码时复用依赖层的缓存。
COPY --from=deps /build/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --require-hashes -r /tmp/requirements.txt && rm /tmp/requirements.txt
COPY pyproject.toml README.md ./
COPY src ./src
COPY --from=web /build/src/video_turbo/orchestrator/static/dashboard ./src/video_turbo/orchestrator/static/dashboard
RUN pip install --no-cache-dir --no-deps .

# 开云机自动部署时，主控把这些文件打包推送到云实例（Linux Worker 源码、配置模板、安装脚本）。
COPY pyproject.toml README.md LICENSE /app/bundle/
COPY src /app/bundle/src
COPY tools /app/bundle/tools
COPY deploy/worker/linux /app/bundle/deploy/worker/linux
COPY deploy/worker/requirements /app/bundle/deploy/worker/requirements
COPY config/*.example.yaml /app/bundle/config/
ENV VIDEO_TURBO_BUNDLE_ROOT=/app/bundle

EXPOSE 8765
CMD ["video-turbo-server"]
