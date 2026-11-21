//! 本机识别：这台电脑也作为显卡电脑处理识别任务。
//!
//! 开启后依次：等主程序就绪 → 取接入信息（本机接口，凭这次启动的令牌）→ 需要时安装运行环境 →
//! 写配置 → 首次运行前自检（下载模型）→ 启动识别程序并守护（意外退出 15 秒后重启）。
//! 主程序重启（例如改了端口）后重新走一遍，已安装、已自检的步骤会跳过。

use std::path::PathBuf;
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::Duration;

use serde::{Deserialize, Serialize};
use shell_core::connection::Connection;
use shell_core::logfile::{self, Logger};
use shell_core::process::{self, Cancel, Running};
use shell_core::worker_env::{Event, InstallError, WorkerEnv};

use crate::server::Server;

const RESTART_DELAY: Duration = Duration::from_secs(15);
const STOP_TIMEOUT: Duration = Duration::from_secs(5);
const TOKEN_HEADER: &str = "X-VideoTurbo-Desktop-Token";

#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
#[serde(tag = "phase", rename_all = "snake_case")]
pub enum WorkerPhase {
    Off,
    /// 等主程序就绪。
    Waiting,
    Installing {
        index: u32,
        total: u32,
        zh: String,
        en: String,
        line: String,
    },
    /// macOS 缺少命令行开发工具（含 Git）。
    NeedTools,
    /// 第一次运行前下载模型并自检。
    SelfTest {
        line: String,
    },
    Running,
    Failed {
        message: String,
        detail: String,
    },
}

#[derive(Deserialize)]
struct WorkerAccess {
    worker_token: String,
    worker_port: u16,
}

struct Inner {
    phase: WorkerPhase,
    running: Option<Running>,
    /// 每次开启、关闭加一，旧线程看到不一致就退出。
    generation: u64,
    /// 结束这一轮正在运行的安装或自检（连同 uv、pip 等子进程）。
    cancel: Arc<Cancel>,
}

pub struct LocalWorker {
    env: WorkerEnv,
    logger: Arc<Logger>,
    inner: Mutex<Inner>,
    notify: Box<dyn Fn(&WorkerPhase) + Send + Sync>,
}

impl LocalWorker {
    pub fn new(
        env: WorkerEnv,
        logger: Arc<Logger>,
        notify: Box<dyn Fn(&WorkerPhase) + Send + Sync>,
    ) -> Arc<Self> {
        Arc::new(LocalWorker {
            env,
            logger,
            inner: Mutex::new(Inner {
                phase: WorkerPhase::Off,
                running: None,
                generation: 0,
                cancel: Arc::new(Cancel::default()),
            }),
            notify,
        })
    }

    pub fn logs(&self) -> PathBuf {
        self.env.logs()
    }

    fn lock(&self) -> std::sync::MutexGuard<'_, Inner> {
        self.inner.lock().unwrap_or_else(|error| error.into_inner())
    }

    pub fn phase(&self) -> WorkerPhase {
        self.lock().phase.clone()
    }

    fn set(&self, generation: u64, phase: WorkerPhase) -> bool {
        let mut inner = self.lock();
        if inner.generation != generation {
            return false;
        }
        if inner.phase != phase {
            inner.phase = phase.clone();
            drop(inner);
            (self.notify)(&phase);
        }
        true
    }

    fn fail(&self, generation: u64, message: String, log: PathBuf) {
        self.logger.info(&format!("本机识别没能运行：{message}"));
        self.set(generation, WorkerPhase::Failed { message, detail: logfile::tail(&log, 30) });
    }

    /// 开启（或在主程序重启后重新接上）。已在运行的识别程序先停下，再按新的接入信息启动。
    pub fn start(self: &Arc<Self>, server: Arc<Server>) {
        let (generation, previous, cancel) = {
            let mut inner = self.lock();
            inner.generation += 1;
            inner.cancel.cancel();
            inner.cancel = Arc::new(Cancel::default());
            (inner.generation, inner.running.take(), Arc::clone(&inner.cancel))
        };
        if let Some(running) = previous {
            running.stop(STOP_TIMEOUT);
        }
        self.set(generation, WorkerPhase::Waiting);
        let worker = Arc::clone(self);
        thread::spawn(move || worker.run(generation, server, cancel));
    }

    /// 关闭：停止识别程序。运行环境留着，下次开启不用重装。
    pub fn stop(&self) {
        let running = {
            let mut inner = self.lock();
            inner.generation += 1;
            inner.phase = WorkerPhase::Off;
            inner.cancel.cancel();
            inner.running.take()
        };
        (self.notify)(&WorkerPhase::Off);
        if let Some(running) = running {
            self.logger.info("停止本机识别");
            running.stop(STOP_TIMEOUT);
        }
    }

    fn current(&self, generation: u64) -> bool {
        self.lock().generation == generation
    }

    fn access(&self, generation: u64, server: &Server) -> Option<WorkerAccess> {
        loop {
            if !self.current(generation) {
                return None;
            }
            if let Some((port, token)) = server.ready() {
                let reply = process::http_get(
                    port,
                    "/api/auth/desktop/worker",
                    &[(TOKEN_HEADER, &token)],
                    Duration::from_secs(5),
                );
                if let Some((200, body)) = reply {
                    if let Ok(access) = serde_json::from_str::<WorkerAccess>(&body) {
                        return Some(access);
                    }
                }
            }
            thread::sleep(Duration::from_secs(1));
        }
    }

    fn run(self: Arc<Self>, generation: u64, server: Arc<Server>, cancel: Arc<Cancel>) {
        let Some(access) = self.access(generation, &server) else { return };
        if self.env.needs_install() {
            self.logger.info("安装本机识别的运行环境");
            // 有 NVIDIA 显卡时用显卡；macOS 和没有显卡的电脑用 CPU。
            let device = if cfg!(target_os = "macos") { "cpu" } else { "auto" };
            let mut current = (0, 0, String::new(), String::new());
            let result = self.env.install(device, &cancel, |event| {
                match event {
                    Event::Step { index, total, zh, en } => current = (index, total, zh, en),
                    Event::Log(line) => {
                        let (index, total, zh, en) = current.clone();
                        self.set(generation, WorkerPhase::Installing { index, total, zh, en, line });
                        return;
                    }
                }
                let (index, total, zh, en) = current.clone();
                self.set(generation, WorkerPhase::Installing { index, total, zh, en, line: String::new() });
            });
            match result {
                Ok(()) => {}
                Err(InstallError::NeedCommandLineTools) => {
                    self.set(generation, WorkerPhase::NeedTools);
                    return;
                }
                // 关闭或重新开启时结束了安装，不算失败。
                Err(_) if cancel.is_cancelled() => return,
                Err(error) => return self.fail(generation, error.message(), self.env.install_log()),
            }
        }
        if !self.current(generation) {
            return;
        }
        let connection = Connection {
            server_url: format!("http://127.0.0.1:{}", access.worker_port),
            token: access.worker_token,
        };
        // 与桌面、游戏共用显卡：新建配置时只在显卡空闲时接单。
        if let Err(error) = self.env.configure(&connection, true) {
            return self.fail(generation, error.message(), self.env.install_log());
        }
        if !self.self_test(generation, &cancel) {
            return;
        }
        self.supervise(generation);
    }

    /// 第一次运行前自检（提前下载模型）；同一版本通过过就跳过。
    fn self_test(&self, generation: u64, cancel: &Cancel) -> bool {
        let marker = self.env.root.join("self-test.ok");
        let version = self.env.installed_version().unwrap_or_default();
        if std::fs::read_to_string(&marker).map(|text| text.trim() == version).unwrap_or(false) {
            return true;
        }
        self.logger.info("本机识别：首次运行前自检");
        self.set(generation, WorkerPhase::SelfTest { line: String::new() });
        let spec = self.env.self_test_spec();
        let status = process::run_lines_cancellable(&spec, cancel, |line| {
            self.set(generation, WorkerPhase::SelfTest { line: line.to_string() });
        });
        if !self.current(generation) {
            return false;
        }
        match status {
            Ok(status) if status.success() => {
                let _ = std::fs::write(&marker, &version);
                true
            }
            Ok(status) => {
                let message = format!("自检没有通过（退出码 {}）。", status.code().unwrap_or(-1));
                self.fail(generation, message, spec.output.clone());
                false
            }
            Err(error) => {
                self.fail(generation, error.to_string(), spec.output.clone());
                false
            }
        }
    }

    /// 启动识别程序，意外退出后等一会儿再启动（主程序或网络暂时不可用时也会退出）。
    fn supervise(&self, generation: u64) {
        loop {
            let spec = self.env.worker_spec();
            match process::spawn(&spec) {
                Ok(running) => {
                    let mut inner = self.lock();
                    if inner.generation != generation {
                        drop(inner);
                        running.stop(STOP_TIMEOUT);
                        return;
                    }
                    self.logger.info(&format!("本机识别已启动（进程 {}）", running.id()));
                    inner.running = Some(running);
                    drop(inner);
                    self.set(generation, WorkerPhase::Running);
                }
                Err(error) => return self.fail(generation, error.to_string(), spec.output.clone()),
            }
            loop {
                thread::sleep(Duration::from_secs(1));
                let mut inner = self.lock();
                if inner.generation != generation {
                    return;
                }
                let exited = inner.running.as_mut().and_then(|running| running.exited());
                if let Some(status) = exited {
                    inner.running = None;
                    drop(inner);
                    self.logger.info(&format!(
                        "本机识别退出（退出码 {:?}），{} 秒后重启",
                        status.code(),
                        RESTART_DELAY.as_secs()
                    ));
                    self.set(generation, WorkerPhase::Waiting);
                    break;
                }
            }
            thread::sleep(RESTART_DELAY);
            if !self.current(generation) {
                return;
            }
        }
    }
}
