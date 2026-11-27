//! 识别程序的管理：主程序应用的“本机识别”和识别机应用共用。
//!
//! 开启后依次：取接入信息（由调用方给出，例如等主程序就绪后从本机接口取）→ 需要时安装运行环境 →
//! 写配置 → 下载当前识别模式需要的模型（或用户在清单里选的模型）→ 自检已下载的模型 →
//! 启动识别程序并守护（意外退出 15 秒后重启）。已安装、已下载、已自检的步骤会跳过。
//! 当前识别模式换了、需要的模型没下载时不自动下载，只在清单里标出来，等用户点下载。同一台电脑上只允许一个识别程序用显卡：两个应用都开着时，
//! 后启动的一方显示“本机已有识别在运行”，等对方停下后自动接手。

use std::fs::File;
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::Duration;

use serde::Serialize;

use crate::connection::Connection;
use crate::logfile::{self, Logger};
use crate::models::{self, ModeInfo, ModelStatus};
use crate::process::{self, Cancel, Running};
use crate::worker_env::{Event, InstallError, WorkerEnv};

const RESTART_DELAY: Duration = Duration::from_secs(15);
const CONFLICT_RETRY: Duration = Duration::from_secs(20);
/// 识别程序不监听标准输入，停止时基本是直接结束；正在处理的任务由主程序重新排队。
const STOP_TIMEOUT: Duration = Duration::from_secs(2);
/// 模型清单（含主程序的当前识别模式）多久刷新一次。
const MODELS_REFRESH: Duration = Duration::from_secs(30);

#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
#[serde(tag = "phase", rename_all = "snake_case")]
pub enum WorkerPhase {
    Off,
    /// 已开启，但用户选了“稍后下载”：运行环境或模型还没下载，点“开始下载”后继续。
    Pending,
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
    /// 下载模型：第 index/total 个，已下载 done_mb / 共 total_mb。
    Models {
        index: u32,
        total: u32,
        zh: String,
        en: String,
        done_mb: u64,
        total_mb: u64,
    },
    /// 运行前自检已下载的模型。
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
    /// 最近一次读到的模型清单。
    catalog: Mutex<Option<ModelStatus>>,
    refreshing: AtomicBool,
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
            catalog: Mutex::new(None),
            refreshing: AtomicBool::new(false),
        })
    }

    /// 最近一次读到的模型清单；运行环境没装好时为 None。
    pub fn models(&self) -> Option<ModelStatus> {
        self.catalog.lock().unwrap_or_else(|error| error.into_inner()).clone()
    }

    /// 重新读取模型清单（会从主程序读当前识别模式，可能要几秒）。
    pub fn refresh_models(&self) -> Option<ModelStatus> {
        let status = self.env.model_status();
        *self.catalog.lock().unwrap_or_else(|error| error.into_inner()) = status.clone();
        status
    }

    /// 在后台刷新模型清单；已经在刷新时不重复。
    pub fn refresh_models_later(self: &Arc<Self>) {
        if self.refreshing.swap(true, Ordering::SeqCst) {
            return;
        }
        let manager = Arc::clone(self);
        thread::spawn(move || {
            manager.refresh_models();
            manager.refreshing.store(false, Ordering::SeqCst);
        });
    }

    /// 定时刷新模型清单：控制台换了识别模式后，清单里能标出新模式缺的模型。
    pub fn watch_models(self: &Arc<Self>) {
        let manager = Arc::clone(self);
        thread::spawn(move || loop {
            manager.refresh_models_later();
            thread::sleep(MODELS_REFRESH);
        });
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

    /// 运行环境装好、至少下载了一个模型，开启后不用再下载任何东西（自检不下载）。
    pub fn ready_to_run(&self) -> bool {
        !self.env.needs_install() && self.has_models()
    }

    fn self_test_marker(&self) -> PathBuf {
        self.env.root.join("self-test.ok")
    }

    /// 自检记录：第一行是运行环境的版本，第二行是当时已下载的模型。
    fn self_test_record(&self, catalog: &ModelStatus) -> String {
        let version = self.env.installed_version().unwrap_or_default();
        format!("{version}\n{}\n", catalog.downloaded().join(","))
    }

    /// 当前已下载的模型都自检过（删掉模型不用重新自检，新下载的要）。
    fn self_test_covers(&self, catalog: &ModelStatus) -> bool {
        let downloaded = catalog.downloaded();
        let version = self.env.installed_version().unwrap_or_default();
        let Ok(text) = std::fs::read_to_string(self.self_test_marker()) else { return false };
        let mut lines = text.lines();
        let tested: Vec<&str> = lines.nth(1).unwrap_or_default().split(',').collect();
        !version.is_empty()
            && !downloaded.is_empty()
            && text.lines().next() == Some(version.as_str())
            && downloaded.iter().all(|id| tested.contains(&id.as_str()))
    }

    /// 开启（或按新的接入信息重新开始）。`connect` 为 None 时沿用已写好的配置。
    /// `allow_download` 为 false 时，还要下载运行环境或模型就停在“待下载”（用户选了稍后下载）。
    pub fn start(self: &Arc<Self>, connect: Option<Connect>, allow_download: bool) {
        self.start_with(connect, allow_download, None);
    }

    /// 同 start；`models` 是用户点了下载、或第一次设置时在下载前选的模型，允许下载时下载这些
    /// （没有给出时，第一次设置按当前识别模式下载）。
    pub fn start_with(
        self: &Arc<Self>,
        connect: Option<Connect>,
        allow_download: bool,
        models: Option<Vec<String>>,
    ) {
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
        thread::spawn(move || manager.run(generation, connect, cancel, allow_download, models));
    }

    /// 下载清单里选的模型：停下识别程序，下载、自检后重新启动。
    pub fn download_models(self: &Arc<Self>, connect: Option<Connect>, ids: Vec<String>) {
        self.start_with(connect, true, Some(ids));
    }

    /// 已具备识别所需的第一遍模型。
    pub fn has_models(&self) -> bool {
        self.models().is_some_and(|catalog| catalog.can_transcribe())
    }

    /// 让用户选模型用的清单：运行环境装好后是实际的清单，之前是按当前识别模式勾选的默认清单。
    pub fn choices(&self, mode: impl FnOnce() -> Option<ModeInfo>) -> ModelStatus {
        self.models().unwrap_or_else(|| models::default_catalog(mode()))
    }

    /// 改用模型的另一个已下载的版本：写配置后重新启动（新版本没自检过时先自检，不下载）。
    pub fn select_model(self: &Arc<Self>, connect: Option<Connect>, id: String, value: String) {
        if let Err(error) = self.env.select_model(&id, &value) {
            self.logger.info(&error.message());
        }
        self.logger.info(&format!("模型 {id} 改用 {value}"));
        self.refresh_models();
        self.start(connect, false);
    }

    /// 删除模型：停下识别程序（Windows 上正在用的文件删不掉），删除后不下载任何东西，重新启动。
    pub fn delete_model(self: &Arc<Self>, connect: Option<Connect>, id: String) {
        let (generation, previous) = {
            let mut inner = self.lock();
            inner.generation += 1;
            inner.cancel.cancel();
            inner.cancel = Arc::new(Cancel::default());
            inner.lock = None;
            (inner.generation, inner.running.take())
        };
        if let Some(running) = previous {
            running.stop(STOP_TIMEOUT);
        }
        self.set(generation, WorkerPhase::Waiting);
        let manager = Arc::clone(self);
        thread::spawn(move || {
            if let Err(error) = manager.env.delete_model(&id) {
                manager.logger.info(&error.message());
            }
            manager.logger.info(&format!("已删除模型 {id}"));
            manager.refresh_models();
            if manager.current(generation) {
                manager.start(connect, false);
            }
        });
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

    fn run(
        self: Arc<Self>,
        generation: u64,
        connect: Option<Connect>,
        cancel: Arc<Cancel>,
        allow_download: bool,
        requested: Option<Vec<String>>,
    ) {
        let installed = !self.env.needs_install();
        let ready = installed && self.refresh_models().is_some_and(|catalog| catalog.can_transcribe());
        if !allow_download && !ready {
            self.set(generation, WorkerPhase::Pending);
            return;
        }
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
        // 写好配置后重新读一次清单：这时才能从主程序读到当前识别模式。
        let Some(mut catalog) = self.refresh_models() else {
            return self.fail(generation, "无法读取模型清单。".into(), self.env.logs().join("models.log"));
        };
        // 用户点了下载就下载选的模型。首次准备第一遍模型且同意下载时，下载当前识别模式要的模型；
        // 之后换了识别模式缺模型时不自动下载，等用户在清单里点。
        let wanted: Vec<String> = match requested {
            Some(ids) => ids,
            None if allow_download && !catalog.can_transcribe() => catalog.missing.clone(),
            None => Vec::new(),
        }
        .into_iter()
        .filter(|id| catalog.needs_download(id))
        .collect();
        if !wanted.is_empty() {
            if !self.fetch_models(generation, &cancel, &wanted) {
                return;
            }
            match self.refresh_models() {
                Some(fresh) => catalog = fresh,
                None => {
                    return self.fail(
                        generation,
                        "无法读取模型清单。".into(),
                        self.env.logs().join("models.log"),
                    )
                }
            }
        }
        if !self.current(generation) {
            return;
        }
        // 第一遍模型尚未就绪时，等待用户在清单里下载。
        if !catalog.can_transcribe() {
            self.set(generation, WorkerPhase::Pending);
            return;
        }
        if !self.self_test(generation, &cancel, &catalog) {
            return;
        }
        self.supervise(generation);
    }

    fn fetch_models(&self, generation: u64, cancel: &Cancel, ids: &[String]) -> bool {
        self.logger.info(&format!("下载模型：{}", ids.join("、")));
        let result = self.env.download_models(ids, cancel, |progress| {
            self.set(
                generation,
                WorkerPhase::Models {
                    index: progress.index,
                    total: progress.count,
                    zh: progress.zh,
                    en: progress.en,
                    done_mb: progress.done_mb,
                    total_mb: progress.total_mb,
                },
            );
        });
        match result {
            Ok(()) => true,
            Err(_) if cancel.is_cancelled() => false,
            Err(error) => {
                self.fail(generation, error.message(), self.env.logs().join("models.log"));
                false
            }
        }
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

    /// 运行前自检已下载的模型（不下载）；这些模型都自检过就跳过。
    fn self_test(&self, generation: u64, cancel: &Cancel, catalog: &ModelStatus) -> bool {
        let marker = self.self_test_marker();
        if self.self_test_covers(catalog) {
            return true;
        }
        self.logger.info("自检已下载的模型");
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
                let _ = std::fs::write(&marker, self.self_test_record(catalog));
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
