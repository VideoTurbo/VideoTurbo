//! 主程序子进程：按外壳设置拼好环境变量启动，等它就绪，意外退出时有限次地自动重启。

use std::ffi::OsString;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant};

use serde::Serialize;
use shell_core::logfile::{self, Logger};
use shell_core::process::{self, Running, Spec};
use shell_core::settings::ServerSettings;

use crate::texts::Texts;

/// 首次启动时 macOS 会先检查新程序，可能要十几秒。
const START_TIMEOUT: Duration = Duration::from_secs(120);
const STOP_TIMEOUT: Duration = Duration::from_secs(15);
/// 五分钟内最多自动重启三次，再失败就停下来等用户处理。
const RESTART_WINDOW: Duration = Duration::from_secs(300);
const MAX_RESTARTS: usize = 3;

#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
#[serde(tag = "phase", rename_all = "lowercase")]
pub enum Phase {
    Starting,
    Running { url: String },
    Failed { message: String, detail: String },
    Stopped,
}

/// 外壳用到的位置。数据和日志都在本机（不随 Windows 漫游配置同步）。
#[derive(Clone, Debug)]
pub struct Paths {
    /// 数据文件夹：Windows 是 %LOCALAPPDATA%\VideoTurbo，macOS 是 ~/Library/Application Support/VideoTurbo。
    pub home: PathBuf,
    /// 安装包里的资源（server/、ffmpeg/、worker-bundle/）。
    pub resources: PathBuf,
    /// 用户目录（macOS 上可浏览的媒体根目录之一）。
    pub user_home: Option<PathBuf>,
}

impl Paths {
    pub fn logs(&self) -> PathBuf {
        self.home.join("logs")
    }
    pub fn settings_file(&self) -> PathBuf {
        self.home.join("desktop.json")
    }
    pub fn server_program(&self) -> PathBuf {
        let name = if cfg!(windows) { "videoturbo-server.exe" } else { "videoturbo-server" };
        self.resources.join("server").join(name)
    }
    fn console_output(&self) -> PathBuf {
        self.logs().join("server-console.log")
    }
}

fn path_env(dir: &Path) -> OsString {
    let mut paths = vec![dir.to_path_buf()];
    if let Some(existing) = std::env::var_os("PATH") {
        paths.extend(std::env::split_paths(&existing));
    }
    std::env::join_paths(paths).unwrap_or_default()
}

/// 主程序的环境变量：Docker 版写在 docker-compose.yml 里的项，桌面版由外壳按设置给出。
/// 每次启动主程序时新生成：应用内窗口凭它免口令登录（/api/auth/desktop），其他程序拿不到。
fn new_token() -> String {
    let mut bytes = [0u8; 32];
    getrandom::fill(&mut bytes).expect("系统随机数不可用");
    bytes.iter().map(|byte| format!("{byte:02x}")).collect()
}

pub fn server_env(
    paths: &Paths,
    settings: &ServerSettings,
    language: &str,
    token: &str,
) -> Vec<(String, OsString)> {
    let home = &paths.home;
    let backup = settings.backup_dir.clone().unwrap_or_else(|| home.join("backups"));
    let mut env: Vec<(String, OsString)> = vec![
        ("VIDEO_TURBO_DEPLOYMENT".into(), "desktop".into()),
        ("VIDEO_TURBO_EXIT_ON_STDIN_CLOSE".into(), "1".into()),
        ("VIDEO_TURBO_DESKTOP_TOKEN".into(), token.into()),
        ("VIDEO_TURBO_DATABASE".into(), home.join("data").join("app.db").into()),
        ("VIDEO_TURBO_CACHE".into(), home.join("cache").into()),
        ("VIDEO_TURBO_LOG_DIR".into(), paths.logs().into()),
        ("VIDEO_TURBO_RESTORE_BACKUP".into(), backup.into()),
        ("VIDEO_TURBO_HOST".into(), (if settings.lan_console { "0.0.0.0" } else { "127.0.0.1" }).into()),
        ("VIDEO_TURBO_WORKER_HOST".into(), "0.0.0.0".into()),
        ("VIDEO_TURBO_PORT".into(), settings.console_port.to_string().into()),
        ("VIDEO_TURBO_WORKER_PORT".into(), settings.worker_port.to_string().into()),
        ("VIDEO_TURBO_BUNDLE_ROOT".into(), paths.resources.join("worker-bundle").into()),
        ("PATH".into(), path_env(&paths.resources.join("ffmpeg"))),
        ("PYTHONUNBUFFERED".into(), "1".into()),
    ];
    if std::env::var_os("VIDEO_TURBO_LANGUAGE").is_none() {
        env.push(("VIDEO_TURBO_LANGUAGE".into(), language.into()));
    }
    if settings.safe_mode {
        env.push(("VIDEO_TURBO_SAFE_MODE".into(), "1".into()));
    }
    // Windows 上不设置：主程序默认可以浏览本机全部盘符。
    if cfg!(target_os = "macos") {
        let mut roots = vec![PathBuf::from("/Volumes")];
        roots.extend(paths.user_home.clone());
        if let Ok(value) = std::env::join_paths(roots) {
            env.push(("VIDEO_TURBO_MEDIA_ROOTS".into(), value));
        }
    }
    env
}

struct Inner {
    phase: Phase,
    running: Option<Running>,
    /// 每次启动加一，旧的监视线程看到不一致就退出。
    generation: u64,
    restarts: Vec<Instant>,
    settings: ServerSettings,
}

pub struct Server {
    paths: Paths,
    texts: Arc<Texts>,
    logger: Arc<Logger>,
    inner: Mutex<Inner>,
    notify: Box<dyn Fn(&Phase) + Send + Sync>,
}

impl Server {
    pub fn new(
        paths: Paths,
        settings: ServerSettings,
        texts: Arc<Texts>,
        logger: Arc<Logger>,
        notify: Box<dyn Fn(&Phase) + Send + Sync>,
    ) -> Arc<Server> {
        Arc::new(Server {
            paths,
            texts,
            logger,
            inner: Mutex::new(Inner {
                phase: Phase::Stopped,
                running: None,
                generation: 0,
                restarts: Vec::new(),
                settings,
            }),
            notify,
        })
    }

    pub fn paths(&self) -> &Paths {
        &self.paths
    }

    fn lock(&self) -> std::sync::MutexGuard<'_, Inner> {
        self.inner.lock().unwrap_or_else(|error| error.into_inner())
    }

    pub fn phase(&self) -> Phase {
        self.lock().phase.clone()
    }

    pub fn settings(&self) -> ServerSettings {
        self.lock().settings.clone()
    }

    fn set_phase(&self, inner: &mut Inner, phase: Phase) {
        if inner.phase != phase {
            inner.phase = phase.clone();
            (self.notify)(&phase);
        }
    }

    fn fail(&self, inner: &mut Inner, message: String) {
        self.logger.info(&format!("主程序没能运行：{message}"));
        let detail = logfile::tail(&self.paths.console_output(), 30);
        self.set_phase(inner, Phase::Failed { message, detail });
    }

    /// 启动主程序（已在运行时先停止）。用户主动启动时清零自动重启计数。
    pub fn start(self: &Arc<Self>, manual: bool) {
        let previous = {
            let mut inner = self.lock();
            inner.generation += 1;
            if manual {
                inner.restarts.clear();
            }
            inner.running.take()
        };
        if let Some(running) = previous {
            self.logger.info("停止主程序（重新启动）");
            running.stop(STOP_TIMEOUT);
        }
        let mut inner = self.lock();
        let generation = inner.generation;
        let settings = inner.settings.clone();
        let program = self.paths.server_program();
        if !program.is_file() {
            let message = self.texts.missing_server();
            self.fail(&mut inner, message);
            return;
        }
        let console_host = if settings.lan_console { [0, 0, 0, 0] } else { [127, 0, 0, 1] };
        for (host, port) in [(console_host, settings.console_port), ([0, 0, 0, 0], settings.worker_port)] {
            if !process::port_free(host, port) {
                let message = self.texts.port_in_use(port);
                self.fail(&mut inner, message);
                return;
            }
        }
        for dir in ["data", "cache", "logs"] {
            let _ = std::fs::create_dir_all(self.paths.home.join(dir));
        }
        let token = new_token();
        let spec = Spec {
            program,
            args: vec![],
            cwd: Some(self.paths.home.clone()),
            env: server_env(&self.paths, &settings, self.texts.lang.server_code(), &token),
            output: self.paths.console_output(),
        };
        match process::spawn(&spec) {
            Ok(running) => {
                self.logger.info(&format!("主程序已启动（进程 {}）", running.id()));
                inner.running = Some(running);
                self.set_phase(&mut inner, Phase::Starting);
            }
            Err(error) => {
                let message = self.texts.spawn_failed(&error);
                self.fail(&mut inner, message);
                return;
            }
        }
        drop(inner);
        let server = Arc::clone(self);
        thread::spawn(move || server.watch(generation, settings.console_port, token));
    }

    /// 等主程序就绪，之后每秒检查一次是否意外退出。
    fn watch(self: Arc<Self>, generation: u64, port: u16, token: String) {
        let started = Instant::now();
        // 先登录再进入控制台；登录接口只接受本机请求和这次启动的令牌。
        let url = format!("http://127.0.0.1:{port}/api/auth/desktop?token={token}");
        loop {
            thread::sleep(Duration::from_millis(if self.phase() == Phase::Starting { 250 } else { 1000 }));
            let mut inner = self.lock();
            if inner.generation != generation {
                return;
            }
            let exited = match inner.running.as_mut() {
                Some(running) => running.exited(),
                None => return,
            };
            if let Some(status) = exited {
                inner.running = None;
                let message = self.texts.exited(status.code());
                self.fail(&mut inner, message);
                let now = Instant::now();
                inner.restarts.retain(|time| now.duration_since(*time) < RESTART_WINDOW);
                if inner.restarts.len() < MAX_RESTARTS {
                    inner.restarts.push(now);
                    drop(inner);
                    self.logger.info("自动重新启动主程序");
                    thread::sleep(Duration::from_secs(3));
                    if self.lock().generation == generation {
                        self.start(false);
                    }
                }
                return;
            }
            if inner.phase == Phase::Starting {
                drop(inner);
                let ready = process::http_ok(port, "/health", Duration::from_secs(2));
                let mut inner = self.lock();
                if inner.generation != generation {
                    return;
                }
                if ready {
                    self.logger.info("主程序已就绪");
                    self.set_phase(&mut inner, Phase::Running { url: url.clone() });
                } else if started.elapsed() > START_TIMEOUT {
                    if let Some(running) = inner.running.take() {
                        running.stop(Duration::from_secs(5));
                    }
                    let message = self.texts.start_timeout();
                    self.fail(&mut inner, message);
                    return;
                }
            }
        }
    }

    /// 保存新设置并按新设置重新启动。
    pub fn apply(self: &Arc<Self>, settings: ServerSettings) -> std::io::Result<()> {
        settings.save(&self.paths.settings_file())?;
        self.lock().settings = settings;
        self.start(true);
        Ok(())
    }

    /// 外壳退出前调用：请主程序有序退出。
    pub fn shutdown(&self) {
        let running = {
            let mut inner = self.lock();
            inner.generation += 1;
            inner.running.take()
        };
        if let Some(running) = running {
            self.logger.info("退出：停止主程序");
            running.stop(STOP_TIMEOUT);
        }
        let mut inner = self.lock();
        self.set_phase(&mut inner, Phase::Stopped);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn value<'a>(env: &'a [(String, OsString)], key: &str) -> Option<&'a OsString> {
        env.iter().find(|(name, _)| name == key).map(|(_, value)| value)
    }

    #[test]
    fn env_follows_settings() {
        let paths = Paths {
            home: PathBuf::from("/data/VideoTurbo"),
            resources: PathBuf::from("/app/resources"),
            user_home: Some(PathBuf::from("/Users/me")),
        };
        let settings = ServerSettings::default();
        let env = server_env(&paths, &settings, "zh-Hans", "abc");
        assert_eq!(value(&env, "VIDEO_TURBO_DESKTOP_TOKEN").unwrap(), "abc");
        assert_eq!(new_token().len(), 64);
        assert_ne!(new_token(), new_token());
        assert_eq!(value(&env, "VIDEO_TURBO_HOST").unwrap(), "127.0.0.1");
        assert_eq!(value(&env, "VIDEO_TURBO_WORKER_HOST").unwrap(), "0.0.0.0");
        assert_eq!(value(&env, "VIDEO_TURBO_DEPLOYMENT").unwrap(), "desktop");
        assert_eq!(
            PathBuf::from(value(&env, "VIDEO_TURBO_RESTORE_BACKUP").unwrap()),
            PathBuf::from("/data/VideoTurbo/backups")
        );
        assert!(value(&env, "VIDEO_TURBO_SAFE_MODE").is_none());
        let first_path = std::env::split_paths(value(&env, "PATH").unwrap()).next().unwrap();
        assert_eq!(first_path, PathBuf::from("/app/resources/ffmpeg"));

        let settings =
            ServerSettings { lan_console: true, safe_mode: true, console_port: 9000, ..Default::default() };
        let env = server_env(&paths, &settings, "en", "abc");
        assert_eq!(value(&env, "VIDEO_TURBO_HOST").unwrap(), "0.0.0.0");
        assert_eq!(value(&env, "VIDEO_TURBO_PORT").unwrap(), "9000");
        assert_eq!(value(&env, "VIDEO_TURBO_SAFE_MODE").unwrap(), "1");
    }
}
