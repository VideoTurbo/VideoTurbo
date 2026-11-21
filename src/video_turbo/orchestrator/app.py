from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import threading
from collections.abc import Callable
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI, Query, Request
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles

from .. import __version__
from ..config import ServerConfig, deployment_settings
from ..config_store import ConfigStore
from ..database import Database
from ..deployment import deployment, is_desktop
from ..environment import getenv
from ..i18n import LocalizedFormatter, msg
from ..migrations import SchemaError
from ..plugins import installer as plugin_installer
from . import chunked_upload
from .auth import AdminAuth, AdminAuthMiddleware
from .auth import router as auth_router
from .cloud_launcher import CloudLauncher
from .cloud_service import LOOP_SECONDS as CLOUD_LOOP_SECONDS
from .cloud_service import CloudService
from .cloud_tunnel import TunnelManager
from .compute_api import router as compute_router
from .config_api import router as config_router
from .context import app_state
from .dashboard import ASSETS_DIR, FAVICON_SVG, dashboard_response
from .diagnostics import LOG_FORMAT, install_log_buffer
from .diagnostics import router as diagnostics_router
from .i18n_middleware import I18nMiddleware
from .integrations import ConnectionStatus, MediaRefresher, NoticeService
from .jobs_api import router as jobs_router
from .libraries_api import router as libraries_router
from .local_worker import LocalWorkerStatus
from .local_worker import router as local_worker_router
from .log_files import close_log_file, install_log_file
from .plugins_api import router as plugins_router
from .poster_service import PosterService
from .processor import Orchestrator
from .rate_limit import RateLimiter, limit_bytes
from .restore_api import router as restore_router
from .restore_service import RestoreService
from .settings_api import router as settings_router
from .worker_api import router as worker_router
from .worker_gateway import WorkerGateway

logger = logging.getLogger(__name__)
WORKER_CLEANUP_SECONDS = 60
# 设为 1 时，标准输入关闭即有序退出。桌面版外壳退出（包括崩溃）时管道随之关闭，主程序不会留在后台；
# Windows 没有可用的终止信号，也靠它有序退出。容器里标准输入通常一开始就是关闭的，所以默认不开启。
EXIT_ON_STDIN_CLOSE_ENV = "VIDEO_TURBO_EXIT_ON_STDIN_CLOSE"


def _paths() -> tuple[Path, Path]:
    database_path = Path(getenv("VIDEO_TURBO_DATABASE", "/app/data/app.db"))
    # 缓存根目录：未设置时用当前目录下的 cache（容器内即 /app/cache）。
    cache_path = Path(getenv("VIDEO_TURBO_CACHE", "") or "cache").resolve()
    return database_path, cache_path


def _load_runtime() -> tuple:
    database_path, cache_path = _paths()
    database = Database(database_path)
    database.initialize()
    store = ConfigStore(database)
    config = store.build()
    recovered_jobs = database.recover_interrupted_jobs()
    if recovered_jobs:
        logger.warning("主程序启动时恢复了 %d 个中断任务。", recovered_jobs)
    recovered_restore = database.recover_interrupted_restore_jobs()
    if recovered_restore:
        logger.warning("主程序启动时有 %d 个影片修复任务在保存途中中断，需人工确认。", recovered_restore)
    return config, store, database, Orchestrator(config, database, cache_path), database.worker_token()


async def _pipeline_loop(app: FastAPI) -> None:
    orchestrator: Orchestrator = app.state.orchestrator
    while True:
        try:
            app.state.database.release_expired_leases()
            if orchestrator.paused:
                await asyncio.sleep(1)
                continue
            did_work = await orchestrator.process_once()
            await asyncio.sleep(0.2 if did_work else 5)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("自动处理出错。")
            await asyncio.sleep(5)


async def _restore_loop(app: FastAPI) -> None:
    service: RestoreService = app.state.restore
    while True:
        try:
            app.state.database.release_expired_restore_leases(
                app.state.config.restore.resume_reservation_minutes
            )
            did_work = await service.process_once()
            await asyncio.sleep(0.5 if did_work else 5)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("影片修复自动处理出错。")
            await asyncio.sleep(5)


async def _cloud_loop(app: FastAPI) -> None:
    service: CloudService = app.state.cloud
    while True:
        try:
            await service.process_once()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("云主机自动关机出错。")
        await asyncio.sleep(CLOUD_LOOP_SECONDS)


async def _worker_cleanup_loop(app: FastAPI) -> None:
    while True:
        try:
            minutes = app.state.config.workers.offline_cleanup_minutes
            if minutes > 0:
                deleted = await asyncio.to_thread(app.state.database.delete_offline_workers, minutes * 60)
                for name in deleted:
                    logger.info("机器 %s 离线超过 %s 分钟，已自动删除。", name, minutes)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("清理离线机器出错。")
        await asyncio.sleep(WORKER_CLEANUP_SECONDS)


async def _daily_scan_loop(app: FastAPI) -> None:
    orchestrator: Orchestrator = app.state.orchestrator
    last_date = None
    while True:
        try:
            # 每轮读取最新配置，控制台修改扫描时间后无需重启。
            config = app.state.config
            if config.scan.enabled and not orchestrator.paused:
                parts = config.scan.schedule.split()
                minute, hour = (int(parts[0]), int(parts[1])) if len(parts) == 5 else (0, 3)
                now = datetime.now().astimezone()
                if now.hour == hour and now.minute == minute and last_date != now.date():
                    await orchestrator.scan()
                    last_date = now.date()
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("定时扫描异常。")
            await asyncio.sleep(30)


def _report_plugin_load(notices: NoticeService, failures: list[tuple[str, str]]) -> None:
    if plugin_installer.safe_mode():
        if failures:
            notices.notify(
                "warning",
                msg("notice.plugins.safeModeTitle"),
                msg("notice.plugins.safeMode.desktop" if is_desktop() else "notice.plugins.safeMode", plugins=[plugin_id for plugin_id, _ in failures], variable=plugin_installer.SAFE_MODE_ENV),
            )
        return
    for plugin_id, reason in failures:
        notices.notify("warning", msg("notice.plugins.notLoaded", plugin_id=plugin_id), reason)


@asynccontextmanager
async def lifespan(app: FastAPI):
    config, store, database, orchestrator, token = _load_runtime()
    console = logging.StreamHandler()
    console.setFormatter(LocalizedFormatter(LOG_FORMAT))
    logging.basicConfig(
        level=getattr(logging, config.server.log_level.upper(), logging.INFO), handlers=[console]
    )
    install_log_buffer()
    log_file = install_log_file()
    logger.info("VideoTurbo %s 启动", __version__)
    if log_file:
        logger.info("日志同时写入 %s", log_file)
    app.state.config = config
    app.state.config_store = store
    app.state.config_lock = asyncio.Lock()
    app.state.database = database
    app.state.orchestrator = orchestrator
    app.state.worker_token = token
    app.state.admin_auth = AdminAuth(database)
    app.state.admin_auth_lock = asyncio.Lock()
    app.state.restore = RestoreService(config, database)
    # 本地预览不推送通知、不刷新媒体服务器；控制台里的“测试”仍可使用。
    live = getenv("VIDEO_TURBO_LOCAL_PREVIEW") != "1"
    app.state.plugin_transport = None
    app.state.notices = NoticeService(database, deliver=live)
    app.state.notices.start()
    _report_plugin_load(app.state.notices, plugin_installer.load_installed(database))
    app.state.media_refresher = MediaRefresher(database, app.state.notices, enabled=live)
    # 网络存储的配置列表沿用媒体服务器的接口，最近一次调用结果暂不记录。
    app.state.media_source_status = SimpleNamespace(status=ConnectionStatus())
    orchestrator.on_published = app.state.media_refresher.request
    app.state.restore.on_published = app.state.media_refresher.request
    app.state.cloud = CloudService(
        config, database, orchestrator, auto_enabled=live, notices=app.state.notices
    )
    app.state.upload_locks = chunked_upload.ChunkLocks()
    app.state.local_worker = LocalWorkerStatus()
    app.state.posters = PosterService(
        config, database, orchestrator.thumbnail_dir, orchestrator.sources
    )
    app.state.upload_limiter = RateLimiter(limit_bytes(database.transfer_settings()))
    tasks = [
        asyncio.create_task(_pipeline_loop(app)),
        asyncio.create_task(_restore_loop(app)),
        asyncio.create_task(_daily_scan_loop(app)),
        asyncio.create_task(_cloud_loop(app)),
        asyncio.create_task(_worker_cleanup_loop(app)),
    ]
    # 云 Worker 经 SSH 反向隧道连回本机的 Worker 专用端口，不需要公网地址。
    host = config.server.worker_listen_host
    app.state.cloud_tunnels = TunnelManager(
        app.state.cloud,
        database,
        local_host="127.0.0.1" if host in {"0.0.0.0", "::", ""} else host,
        local_port=config.server.worker_port,
    )
    app.state.cloud.tunnels = app.state.cloud_tunnels
    if app.state.cloud.auto_enabled:
        tasks.append(asyncio.create_task(app.state.cloud_tunnels.run()))
    app.state.cloud_launcher = CloudLauncher(
        app.state.cloud,
        database,
        worker_token=lambda: app.state.worker_token,
        tunnels=app.state.cloud_tunnels,
    )
    app.state.cloud_launcher.resume_all()
    try:
        yield
    finally:
        await app.state.cloud_launcher.close()
        await app.state.cloud_tunnels.close()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await app.state.media_refresher.close()
        await app.state.notices.close()
        await orchestrator.close()
        database.close()
        close_log_file()


# 不提供 Swagger / ReDoc / OpenAPI 文档。
app = FastAPI(
    title="VideoTurbo",
    version=__version__,
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
app.include_router(config_router)
app.include_router(plugins_router)
app.include_router(libraries_router)
app.include_router(auth_router)
app.include_router(diagnostics_router)
app.include_router(settings_router)
app.include_router(jobs_router)
app.include_router(worker_router)
app.include_router(compute_router)
app.include_router(local_worker_router)
app.include_router(restore_router)
app.add_middleware(AdminAuthMiddleware)
# 最后添加的在最外层：登录校验的拒绝信息也要翻译。
app.add_middleware(I18nMiddleware)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "version": __version__}


@app.get("/", include_in_schema=False)
async def dashboard() -> Response:
    return dashboard_response()


# 控制台的脚本和样式（文件名带内容哈希）。目录在前端构建后才存在。
app.mount("/assets", StaticFiles(directory=ASSETS_DIR, check_dir=False), name="dashboard-assets")


@app.get("/favicon.svg", include_in_schema=False)
@app.get("/favicon.ico", include_in_schema=False)
async def favicon() -> Response:
    return Response(FAVICON_SVG, media_type="image/svg+xml", headers={"Cache-Control": "public, max-age=86400"})


@app.get("/api/status")
async def status(request: Request) -> dict:
    db: Database = app_state(request).database
    orchestrator: Orchestrator = app_state(request).orchestrator
    config = app_state(request).config
    cloud = app_state(request).cloud

    def read_database() -> dict:
        return {
            "asr": db.asr_settings(config.asr).model_dump(),
            "ui": db.ui_settings().model_dump(),
            "media_counts": db.media_counts(),
            "workers": db.list_workers(),
            "cloud": cloud.status(),
        }

    # 数据库查询放到线程中，避免阻塞同一事件循环上的文件传输。
    stored = await asyncio.to_thread(read_database)
    return {
        **stored,
        "version": __version__,
        "deployment": deployment(),
        "scan_status": orchestrator.scan_status,
        "manual_scan_enabled": config.scan.manual_scan_enabled,
        "media_libraries": [
            {"id": library.id, "name": library.name, "enabled": library.enabled}
            for library in config.media_libraries
        ],
        "pipeline_paused": orchestrator.paused,
        "last_scan": orchestrator.last_scan,
        "warnings": config.warnings(),
        "translation": orchestrator.translation_status(),
        "heartbeat_timeout_seconds": config.workers.heartbeat_timeout_seconds,
        # 桌面版：本机识别在识别程序连上之前的进度（等待下载、安装中、出错）。
        "local_worker": app_state(request).local_worker.current(),
    }


@app.get("/api/notices")
def list_notices(
    request: Request,
    after_id: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
) -> dict:
    database: Database = app_state(request).database
    return {"items": database.list_notices(after_id=after_id, limit=limit), **database.notice_summary()}


@app.post("/api/notices/read")
def read_notices(request: Request) -> dict:
    app_state(request).database.mark_notices_read()
    return app_state(request).database.notice_summary()


def _watch_stdin(stream, loop: asyncio.AbstractEventLoop, stop: Callable[[], None]) -> None:
    """读到标准输入结束（对方关闭管道）后，在事件循环里调用 stop。内容一律丢弃。"""
    try:
        while stream is not None and stream.read(1024):
            pass
    except (OSError, ValueError):
        pass
    logger.info("标准输入已关闭，主程序退出。")
    loop.call_soon_threadsafe(stop)


# 桌面版外壳每 2 秒上报本机识别状态：不写访问日志，免得主程序运行期间日志一直变大。
QUIET_ACCESS_PATHS = frozenset({"/api/auth/desktop/local-worker"})


class _QuietAccessLog(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        # uvicorn 访问日志的参数：(客户端, 方法, 路径, HTTP 版本, 状态码)
        args = record.args
        return not (isinstance(args, tuple) and len(args) >= 3 and args[2] in QUIET_ACCESS_PATHS)


async def _serve(server: ServerConfig) -> None:
    import uvicorn

    log_level = server.log_level.lower()
    servers = [
        uvicorn.Server(
            uvicorn.Config(
                app,
                host=server.host,
                port=server.port,
                log_level=log_level,
                # 只有可信的反向代理转发的 X-Forwarded-For / X-Forwarded-Proto 才会被采用：
                # 登录限流按真实客户端地址计，HTTPS 下会话 Cookie 带 Secure。
                forwarded_allow_ips=server.trusted_proxies or "127.0.0.1",
            )
        ),
        # Worker 专用端口：公网映射时只映射这个端口，控制台和管理接口留在局域网。
        uvicorn.Server(
            uvicorn.Config(
                WorkerGateway(app),
                host=server.worker_listen_host,
                port=server.worker_port,
                log_level=log_level,
                lifespan="off",
                # 不在响应头中暴露服务端软件与时间。
                server_header=False,
                date_header=False,
            )
        ),
    ]

    # uvicorn.Config 会重新配置日志，过滤器在创建之后再加。
    logging.getLogger("uvicorn.access").addFilter(_QuietAccessLog())

    def stop() -> None:
        for item in servers:
            item.should_exit = True

    if getenv(EXIT_ON_STDIN_CLOSE_ENV) == "1":
        stream = sys.stdin.buffer if sys.stdin is not None else None
        threading.Thread(
            target=_watch_stdin,
            args=(stream, asyncio.get_running_loop(), stop),
            name="stdin-watch",
            daemon=True,
        ).start()
    tasks = [asyncio.create_task(item.serve()) for item in servers]
    # 每个 uvicorn 实例都会接管信号，只有最后一个能收到；任一结束时通知其余一起退出。
    await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    stop()
    await asyncio.gather(*tasks, return_exceptions=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="VideoTurbo 主程序")
    parser.add_argument("--version", action="version", version=f"VideoTurbo {__version__}")
    parser.parse_args()
    # 先在启动服务之前升级数据库：版本不符时只输出一行原因并退出，不打印 uvicorn 的启动失败堆栈。
    try:
        Database(_paths()[0]).initialize()
    except SchemaError as exc:
        print(f"主程序无法启动：{exc}", file=sys.stderr, flush=True)
        sys.exit(1)
    asyncio.run(_serve(ServerConfig.model_validate(deployment_settings()["server"])))
