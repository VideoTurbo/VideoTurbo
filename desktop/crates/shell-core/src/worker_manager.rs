//! 识别程序的管理：主程序应用的“本机识别”和识别机应用共用。
//!
//! 开启后依次：取接入信息（由调用方给出，例如等主程序就绪后从本机接口取）→ 需要时安装运行环境 →
//! 写配置 → 首次运行前自检（下载模型）→ 启动识别程序并守护（意外退出 15 秒后重启）。
//! 已安装、已自检的步骤会跳过。同一台电脑上只允许一个识别程序用显卡：两个应用都开着时，
//! 后启动的一方显示“本机已有识别在运行”，等对方停下后自动接手。

use std::fs::File;
use std::path::PathBuf;
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::Duration;

use serde::Serialize;

use crate::connection::Connection;
use crate::logfile::{self, Logger};
use crate::process::{self, Cancel, Running};
use crate::worker_env::{Event, InstallError, WorkerEnv};

const RESTART_DELAY: Duration = Duration::from_secs(15);
const CONFLICT_RETRY: Duration = Duration::from_secs(20);
/// 识别程序不监听标准输入，停止时基本是直接结束；正在处理的任务由主程序重新排队。
const STOP_TIMEOUT: Duration = Duration::from_secs(2);

#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
#[serde(tag = "phase", rename_all = "snake_case")]
pub enum WorkerPhase {
    Off,
    /// 等接入信息（例如主程序还没就绪）。
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
    /// 这台电脑上另一个应用的识别程序正在运行。
    Conflict,
    Failed {
        message: String,
        detail: String,
    },
}

/// 取接入信息：参数是“这一轮是否还有效”的检查，可以阻塞等待；返回 None 表示放弃（已关闭）。
pub type Connect = Box<dyn FnOnce(&dyn Fn() -> bool) -> Option<Connection> + Send>;

struct Inner {
    phase: WorkerPhase,
    running: Option<Running>,
    /// 识别程序运行期间持有的锁文件，保证同一台电脑只有一个识别程序。
    lock: Option<File>,
    /// 每次开启、关闭加一，旧线程看到不一致就退出。
    generation: u64,
    /// 结束这一轮正在运行的安装或自检（连同 uv、pip 等子进程）。
    cancel: Arc<Cancel>,
}

pub struct WorkerManager {
    env: WorkerEnv,
    logger: Arc<Logger>,
    inner: Mutex<Inner>,
    notify: Box<dyn Fn(&WorkerPhase) + Send + Sync>,
}

impl WorkerManager {
    pub fn new(
        env: WorkerEnv,
        logger: Arc<Logger>,
        notify: Box<dyn Fn(&WorkerPhase) + Send + Sync>,
    ) -> Arc<Self> {
        Arc::new(WorkerManager {
            env,
            logger,
            inner: Mutex::new(Inner {
                phase: WorkerPhase::Off,
                running: None,
                lock: None,
                generation: 0,
                cancel: Arc::new(Cancel::default()),
            }),
            notify,
        })
    }

    pub fn env(&self) -> &WorkerEnv {
        &self.env
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
        self.logger.info(&format!("识别程序没能运行：{message}"));
        self.set(generation, WorkerPhase::Failed { message, detail: logfile::tail(&log, 30) });
    }

    fn current(&self, generation: u64) -> bool {
        self.lock().generation == generation
    }

    /// 开启（或按新的接入信息重新开始）。`connect` 为 None 时沿用已写好的配置。
    pub fn start(self: &Arc<Self>, connect: Option<Connect>) {
        let (generation, previous, cancel) = {
            let mut inner = self.lock();
            inner.generation += 1;
            inner.cancel.cancel();
            inner.cancel = Arc::new(Cancel::default());
            inner.lock = None;
            (inner.generation, inner.running.take(), Arc::clone(&inner.cancel))
        };
        if let Some(running) = previous {
            running.stop(STOP_TIMEOUT);
        }
        self.set(generation, WorkerPhase::Waiting);
        let manager = Arc::clone(self);
        thread::spawn(move || manager.run(generation, connect, cancel));
    }

    /// 关闭：停止识别程序和正在进行的安装。运行环境留着，下次开启不用重装。
    pub fn stop(&self) {
        let running = {
            let mut inner = self.lock();
            inner.generation += 1;
            inner.phase = WorkerPhase::Off;
            inner.cancel.cancel();
            inner.lock = None;
            inner.running.take()
        };
        (self.notify)(&WorkerPhase::Off);
        if let Some(running) = running {
            self.logger.info("停止识别程序");
            running.stop(STOP_TIMEOUT);
        }
    }

    fn run(self: Arc<Self>, generation: u64, connect: Option<Connect>, cancel: Arc<Cancel>) {
        let connection = match connect {
            Some(connect) => {
                let alive = || self.current(generation);
                match connect(&alive) {
                    Some(connection) => Some(connection),
                    None => return,
                }
            }
            None => None,
        };
        if self.env.needs_install() && !self.install(generation, &cancel) {
            return;
        }
        if !self.current(generation) {
            return;
        }
        if let Some(connection) = connection {
            // 与桌面、游戏共用显卡：新建配置时只在显卡空闲时接单。
            if let Err(error) = self.env.configure(&connection, true) {
                return self.fail(generation, error.message(), self.env.install_log());
            }
        }
        if !self.env.configured() {
            return self.fail(generation, "还没有填写主程序的接入信息。".into(), self.env.install_log());
        }
        if !self.self_test(generation, &cancel) {
            return;
        }
        self.supervise(generation);
    }

    fn install(&self, generation: u64, cancel: &Cancel) -> bool {
        self.logger.info("安装识别程序的运行环境");
        // 有 NVIDIA 显卡时用显卡；macOS 和没有显卡的电脑用 CPU。
        let device = if cfg!(target_os = "macos") { "cpu" } else { "auto" };
        let mut current = (0, 0, String::new(), String::new());
        let result = self.env.install(device, cancel, |event| {
            let line = match event {
                Event::Step { index, total, zh, en } => {
                    current = (index, total, zh, en);
                    String::new()
                }
                Event::Log(line) => line,
            };
            let (index, total, zh, en) = current.clone();
            self.set(generation, WorkerPhase::Installing { index, total, zh, en, line });
        });
        match result {
            Ok(()) => true,
            Err(InstallError::NeedCommandLineTools) => {
                self.set(generation, WorkerPhase::NeedTools);
                false
            }
            // 关闭或重新开启时结束了安装，不算失败。
            Err(_) if cancel.is_cancelled() => false,
            Err(error) => {
                self.fail(generation, error.message(), self.env.install_log());
                false
            }
        }
    }

    /// 第一次运行前自检（提前下载模型）；同一版本通过过就跳过。
    fn self_test(&self, generation: u64, cancel: &Cancel) -> bool {
        let marker = self.env.root.join("self-test.ok");
        let version = self.env.installed_version().unwrap_or_default();
        if std::fs::read_to_string(&marker).map(|text| text.trim() == version).unwrap_or(false) {
            return true;
        }
        self.logger.info("首次运行前自检");
        self.set(generation, WorkerPhase::SelfTest { line: String::new() });
        let spec = self.env.self_test_spec();
        let status = process::run_lines_cancellable(&spec, cancel, |line| {
            self.set(generation, WorkerPhase::SelfTest { line: line.to_string() });
        });
        if !self.current(generation) || cancel.is_cancelled() {
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

    /// 拿到同一台电脑上唯一的识别锁；另一个应用的识别程序在运行时等它停下。
    fn acquire(&self, generation: u64) -> Option<File> {
        let path = self.env.root.join("worker.lock");
        loop {
            if !self.current(generation) {
                return None;
            }
            if let Ok(file) = File::options().create(true).truncate(false).write(true).open(&path) {
                if file.try_lock().is_ok() {
                    return Some(file);
                }
            }
            if self.phase() != WorkerPhase::Conflict && self.set(generation, WorkerPhase::Conflict) {
                self.logger.info("这台电脑上另一个应用的识别程序正在运行，等它停下");
            }
            thread::sleep(CONFLICT_RETRY);
        }
    }

    /// 启动识别程序，意外退出后等一会儿再启动（主程序或网络暂时不可用时也会退出）。
    fn supervise(&self, generation: u64) {
        let Some(lock) = self.acquire(generation) else { return };
        self.lock().lock = Some(lock);
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
                    self.logger.info(&format!("识别程序已启动（进程 {}）", running.id()));
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
                        "识别程序退出（退出码 {:?}），{} 秒后重启",
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

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn only_one_holder_of_the_worker_lock() {
        let root = std::env::temp_dir().join(format!("vt-lock-{}", std::process::id()));
        std::fs::create_dir_all(&root).unwrap();
        let path = root.join("worker.lock");
        let open = || File::options().create(true).truncate(false).write(true).open(&path).unwrap();
        let first = open();
        assert!(first.try_lock().is_ok());
        assert!(open().try_lock().is_err());
        drop(first);
        // 其他测试同时在启动子进程：fork 到 exec 之间子进程会短暂带着这个锁，稍等再试。
        let reacquired = (0..50).any(|_| {
            let ok = open().try_lock().is_ok();
            if !ok {
                thread::sleep(Duration::from_millis(20));
            }
            ok
        });
        assert!(reacquired);
        let _ = std::fs::remove_dir_all(root);
    }
}
