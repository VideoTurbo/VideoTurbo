//! 识别程序（Worker）的运行环境：主程序应用的“本机识别”和独立识别机应用共用。
//!
//! 安装包只带 uv 和识别程序的源码（worker-bundle），几 GB 的 Python 依赖、显卡运行库和模型在第一次开启时下载：
//! 1. uv 把 Python 3.11 装到 `<root>/python`（官方源不通时换镜像）。
//! 2. 用这个 Python 运行 `worker-bundle/tools/desktop_worker.py install`，由它选下载源、准备 Git、
//!    建环境、按锁定版本装依赖和 WhisperJAV。进度以 `@@VT ` 开头的 JSON 行报告。
//! 3. 版本号写在 `<root>/state.json`；安装包升级后版本不同，再装一次。
//!
//! 布局见 tools/desktop_worker.py。

use std::ffi::OsString;
use std::path::{Path, PathBuf};

use serde::Deserialize;

use crate::connection::Connection;
use crate::process::{self, Cancel, Spec};

const PROGRESS: &str = "@@VT ";
/// 官方源（GitHub）下载 Python 失败时依次尝试的镜像。
const PYTHON_MIRRORS: &[&str] = &["https://registry.npmmirror.com/-/binary/python-build-standalone"];

#[derive(Clone, Debug)]
pub struct WorkerEnv {
    /// 运行环境的根目录，例如 `%LOCALAPPDATA%\VideoTurbo\worker`。
    pub root: PathBuf,
    /// 安装包里识别程序的源码（worker-bundle）。
    pub bundle: PathBuf,
    /// 安装包附带的 uv。
    pub uv: PathBuf,
    /// 安装包附带的 FFmpeg 所在目录。
    pub ffmpeg_dir: PathBuf,
    /// 子进程的界面语言（zh-Hans / en）。
    pub language: String,
}

/// 安装过程中的事件。
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Event {
    /// 第 index 步（共 total 步）开始。
    Step { index: u32, total: u32, zh: String, en: String },
    /// 安装程序的一行输出（写进安装日志，界面上显示最后一行）。
    Log(String),
}

#[derive(Debug, PartialEq, Eq)]
pub enum InstallError {
    /// macOS 上缺少命令行开发工具（含 Git），要先运行 `xcode-select --install`。
    NeedCommandLineTools,
    /// 其他失败：给用户看的原因，详细输出在安装日志里。
    Failed(String),
}

#[derive(Deserialize)]
#[serde(tag = "event", rename_all = "lowercase")]
enum Message {
    Step { index: u32, total: u32, zh: String, en: String },
    Done,
    Configured,
    Error { code: String, message: String },
}

#[derive(Deserialize)]
struct State {
    version: String,
}

fn python_in(venv: &Path) -> PathBuf {
    if cfg!(windows) {
        venv.join("Scripts").join("python.exe")
    } else {
        venv.join("bin").join("python")
    }
}

impl WorkerEnv {
    pub fn config_file(&self) -> PathBuf {
        self.root.join("config").join("worker.yaml")
    }
    pub fn logs(&self) -> PathBuf {
        self.root.join("logs")
    }
    pub fn install_log(&self) -> PathBuf {
        self.logs().join("install.log")
    }
    pub fn worker_log(&self) -> PathBuf {
        self.logs().join("worker.log")
    }
    fn python_dir(&self) -> PathBuf {
        self.root.join("python")
    }
    fn script(&self) -> PathBuf {
        self.bundle.join("tools").join("desktop_worker.py")
    }
    pub fn venv_python(&self) -> PathBuf {
        python_in(&self.root.join(".venv"))
    }

    /// 安装包里识别程序的版本。
    pub fn bundle_version(&self) -> Option<String> {
        let text =
            std::fs::read_to_string(self.bundle.join("src").join("video_turbo").join("__init__.py")).ok()?;
        text.lines()
            .find_map(|line| line.strip_prefix("__version__ = \""))
            .and_then(|rest| rest.strip_suffix('"'))
            .map(str::to_string)
    }

    /// 已安装的版本；没装完或装到一半失败时为 None。
    pub fn installed_version(&self) -> Option<String> {
        let data = std::fs::read(self.root.join("state.json")).ok()?;
        let state: State = serde_json::from_slice(&data).ok()?;
        (self.venv_python().is_file() && python_in(&self.root.join(".venv-whisperjav")).is_file())
            .then_some(state.version)
    }

    /// 没装过或安装包升级了，需要（重新）安装。
    pub fn needs_install(&self) -> bool {
        self.installed_version().is_none() || self.installed_version() != self.bundle_version()
    }

    pub fn configured(&self) -> bool {
        std::fs::read_to_string(self.config_file())
            .map(|text| {
                let filled = |key: &str| {
                    text.lines().any(|line| {
                        line.strip_prefix(key)
                            .and_then(|rest| rest.strip_prefix(':'))
                            .map(|value| !matches!(value.trim().trim_matches('"'), "" | "null" | "~"))
                            .unwrap_or(false)
                    })
                };
                filled("server_url") && filled("worker_token")
            })
            .unwrap_or(false)
    }

    /// 所有子进程共用的环境变量。
    pub fn env(&self) -> Vec<(String, OsString)> {
        let mut paths = vec![self.ffmpeg_dir.clone()];
        if cfg!(windows) {
            paths.push(self.root.join("mingit").join("cmd"));
        }
        if let Some(existing) = std::env::var_os("PATH") {
            paths.extend(std::env::split_paths(&existing));
        }
        vec![
            ("PATH".into(), std::env::join_paths(paths).unwrap_or_default()),
            ("PYTHONUTF8".into(), "1".into()),
            ("PYTHONUNBUFFERED".into(), "1".into()),
            ("UV_PYTHON_INSTALL_DIR".into(), self.python_dir().into()),
            ("UV_PYTHON_PREFERENCE".into(), "only-managed".into()),
            ("VIDEO_TURBO_LANGUAGE".into(), self.language.clone().into()),
        ]
    }

    fn spec(&self, program: PathBuf, args: Vec<OsString>, extra: &[(&str, &str)]) -> Spec {
        let mut env = self.env();
        env.extend(extra.iter().map(|(key, value)| (key.to_string(), OsString::from(value))));
        Spec { program, args, cwd: Some(self.root.clone()), env, output: self.install_log() }
    }

    /// 找到 uv 管理的 Python 3.11，没有就下载（官方源失败时换镜像）。
    fn ensure_python(
        &self,
        cancel: &Cancel,
        on_event: &mut impl FnMut(Event),
    ) -> Result<PathBuf, InstallError> {
        let find = || -> Option<PathBuf> {
            let mut found = None;
            let spec = self.spec(self.uv.clone(), vec!["python".into(), "find".into(), "3.11".into()], &[]);
            let status = process::run_lines(&spec, |line| {
                let path = PathBuf::from(line.trim());
                if path.is_file() {
                    found = Some(path);
                }
            })
            .ok()?;
            status.success().then_some(found).flatten()
        };
        if let Some(path) = find() {
            return Ok(path);
        }
        let mut last_error = String::new();
        for mirror in std::iter::once(None).chain(PYTHON_MIRRORS.iter().map(|mirror| Some(*mirror))) {
            let extra: Vec<(&str, &str)> =
                mirror.map(|m| vec![("UV_PYTHON_INSTALL_MIRROR", m)]).unwrap_or_default();
            let spec =
                self.spec(self.uv.clone(), vec!["python".into(), "install".into(), "3.11".into()], &extra);
            match process::run_lines_cancellable(&spec, cancel, |line| on_event(Event::Log(line.to_string())))
            {
                Ok(status) if status.success() => {
                    if let Some(path) = find() {
                        return Ok(path);
                    }
                }
                Ok(status) => last_error = format!("uv 退出码 {}", status.code().unwrap_or(-1)),
                // 启动不了 uv（例如安装不完整）时换镜像也没用。
                Err(error) => {
                    return Err(InstallError::Failed(format!("无法运行 {}：{error}", self.uv.display())))
                }
            }
        }
        Err(InstallError::Failed(format!("无法下载 Python 3.11，请检查网络后重试（{last_error}）。")))
    }

    /// 运行安装脚本的一个子命令，转发进度，返回脚本报告的错误。
    fn run_script(
        &self,
        python: &Path,
        args: Vec<OsString>,
        cancel: &Cancel,
        on_event: &mut impl FnMut(Event),
    ) -> Result<(), InstallError> {
        let mut full = vec![self.script().into_os_string()];
        full.extend(args);
        let spec = self.spec(python.to_path_buf(), full, &[]);
        let mut error = None;
        let status =
            process::run_lines_cancellable(&spec, cancel, |line| match line.strip_prefix(PROGRESS) {
                Some(json) => match serde_json::from_str::<Message>(json) {
                    Ok(Message::Step { index, total, zh, en }) => {
                        on_event(Event::Step { index, total, zh, en })
                    }
                    Ok(Message::Error { code, message }) => {
                        error = Some(if code == "need_clt" {
                            InstallError::NeedCommandLineTools
                        } else {
                            InstallError::Failed(message)
                        })
                    }
                    Ok(Message::Done | Message::Configured) | Err(_) => {}
                },
                None => on_event(Event::Log(line.to_string())),
            })
            .map_err(|error| InstallError::Failed(error.to_string()))?;
        match error {
            Some(error) => Err(error),
            None if status.success() => Ok(()),
            None => Err(InstallError::Failed(format!("安装程序退出码 {}", status.code().unwrap_or(-1)))),
        }
    }

    /// 安装或升级运行环境。`device` 是 auto（有 NVIDIA 显卡时用显卡）或 cpu。
    pub fn install(
        &self,
        device: &str,
        cancel: &Cancel,
        mut on_event: impl FnMut(Event),
    ) -> Result<(), InstallError> {
        std::fs::create_dir_all(self.logs()).map_err(|error| InstallError::Failed(error.to_string()))?;
        let python = self.ensure_python(cancel, &mut on_event)?;
        let args: Vec<OsString> = vec![
            "install".into(),
            "--root".into(),
            self.root.clone().into(),
            "--bundle".into(),
            self.bundle.clone().into(),
            "--uv".into(),
            self.uv.clone().into(),
            "--python".into(),
            python.clone().into(),
            "--ffmpeg-dir".into(),
            self.ffmpeg_dir.clone().into(),
            "--device".into(),
            device.into(),
        ];
        self.run_script(&python, args, cancel, &mut on_event)
    }

    /// 写入主程序地址和接入密钥。`shared_machine`：新建配置时开启空闲检测（与桌面、游戏共用显卡）。
    pub fn configure(&self, connection: &Connection, shared_machine: bool) -> Result<(), InstallError> {
        let python = self.venv_python();
        let cancel = Cancel::default();
        let python = if python.is_file() { python } else { self.ensure_python(&cancel, &mut |_| {})? };
        let mut args: Vec<OsString> = vec![
            "configure".into(),
            "--root".into(),
            self.root.clone().into(),
            "--bundle".into(),
            self.bundle.clone().into(),
            "--server-url".into(),
            connection.server_url.clone().into(),
            "--token".into(),
            connection.token.clone().into(),
        ];
        if shared_machine {
            args.push("--shared-machine".into());
        }
        self.run_script(&python, args, &cancel, &mut |_| {})
    }

    /// 启动识别程序；它自己写日志，标准输出只留作排查。
    pub fn worker_spec(&self) -> Spec {
        let config: OsString = self.config_file().into();
        let log: OsString = self.worker_log().into();
        let mut spec = self.spec(
            self.venv_python(),
            vec![
                "-m".into(),
                "video_turbo.worker.main".into(),
                "--config".into(),
                config,
                "--log-file".into(),
                log,
            ],
            &[],
        );
        spec.output = self.logs().join("worker-console.log");
        spec
    }

    /// 首次启动前的自检：提前下载模型并逐项检查。
    pub fn self_test_spec(&self) -> Spec {
        let config: OsString = self.config_file().into();
        let mut spec = self.spec(
            self.venv_python(),
            vec![
                "-m".into(),
                "video_turbo.worker.main".into(),
                "--config".into(),
                config,
                "--self-test".into(),
            ],
            &[],
        );
        spec.output = self.logs().join("self-test.log");
        spec
    }
}

impl InstallError {
    pub fn message(&self) -> String {
        match self {
            InstallError::NeedCommandLineTools => "需要先安装 Apple 的命令行开发工具（含 Git）。".into(),
            InstallError::Failed(message) => message.clone(),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn env(root: &Path) -> WorkerEnv {
        WorkerEnv {
            root: root.to_path_buf(),
            bundle: Path::new(env!("CARGO_MANIFEST_DIR")).join("../../.."),
            uv: "uv".into(),
            ffmpeg_dir: "/ffmpeg".into(),
            language: "zh-Hans".into(),
        }
    }

    #[test]
    fn version_state_decides_whether_to_install() {
        let root = std::env::temp_dir().join(format!("vt-env-{}", std::process::id()));
        let worker = env(&root);
        assert!(worker.bundle_version().is_some());
        assert!(worker.needs_install());
        for venv in [".venv", ".venv-whisperjav"] {
            let python = python_in(&root.join(venv));
            std::fs::create_dir_all(python.parent().unwrap()).unwrap();
            std::fs::write(&python, b"").unwrap();
        }
        std::fs::write(root.join("state.json"), br#"{"version":"0.0.0"}"#).unwrap();
        assert_eq!(worker.installed_version().as_deref(), Some("0.0.0"));
        assert!(worker.needs_install());
        let current = worker.bundle_version().unwrap();
        std::fs::write(root.join("state.json"), format!(r#"{{"version":"{current}"}}"#)).unwrap();
        assert!(!worker.needs_install());
        let _ = std::fs::remove_dir_all(root);
    }

    #[test]
    fn configured_needs_address_and_token() {
        let root = std::env::temp_dir().join(format!("vt-conf-{}", std::process::id()));
        let worker = env(&root);
        assert!(!worker.configured());
        std::fs::create_dir_all(root.join("config")).unwrap();
        std::fs::write(worker.config_file(), "server_url: \"http://a:8766\"\nworker_token: \"\"\n").unwrap();
        assert!(!worker.configured());
        std::fs::write(worker.config_file(), "server_url: \"http://a:8766\"\nworker_token: \"t\"\n").unwrap();
        assert!(worker.configured());
        let _ = std::fs::remove_dir_all(root);
    }

    #[test]
    fn progress_lines_are_parsed() {
        let message: Message = serde_json::from_str(
            r#"{"event":"step","index":2,"total":6,"key":"git","zh":"准备 Git","en":"Git"}"#,
        )
        .unwrap();
        assert!(matches!(message, Message::Step { index: 2, total: 6, .. }));
        let message: Message =
            serde_json::from_str(r#"{"event":"error","code":"need_clt","message":"x"}"#).unwrap();
        assert!(matches!(message, Message::Error { ref code, .. } if code == "need_clt"));
    }
}
