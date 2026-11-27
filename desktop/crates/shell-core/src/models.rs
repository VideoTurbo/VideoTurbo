//! 识别模型清单：在识别程序的环境里运行 `video_turbo.worker.model_catalog`，列出、下载、删除模型。
//!
//! 每种识别模式要的模型不同（WhisperJAV：Qwen3-ASR，双遍再加 anime-whisper）。
//! 清单从主程序读取当前识别模式，标出“当前模式需要”的模型；默认只下载这些，其余由用户在清单里自己下载。

use std::ffi::OsString;

use serde::{Deserialize, Serialize};

use crate::process::{self, Cancel};
use crate::worker_env::{InstallError, WorkerEnv, PROGRESS};

const MODULE: &str = "video_turbo.worker.model_catalog";

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct ModeInfo {
    pub mode: String,
    pub whisperjav_ensemble: bool,
    /// 刚从主程序读到的（false：连不上主程序，用的是上次读到的）。
    pub live: bool,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct ModelInfo {
    pub id: String,
    pub zh: String,
    pub en: String,
    pub purpose_zh: String,
    pub purpose_en: String,
    pub size_mb: Option<u64>,
    pub downloaded: bool,
    /// 当前识别模式需要。
    pub needed: bool,
    /// 当前用的版本（worker.yaml 里的值）；没有版本可选时为 None。
    #[serde(default)]
    pub variant: Option<String>,
    #[serde(default)]
    pub variants: Vec<VariantInfo>,
}

/// 模型的一个版本（例如 Qwen3-ASR 1.7B / 0.6B）。
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct VariantInfo {
    pub value: String,
    pub label: String,
    pub size_mb: Option<u64>,
    #[serde(default)]
    pub downloaded: bool,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct ModelStatus {
    pub mode: Option<ModeInfo>,
    pub models: Vec<ModelInfo>,
    /// 当前识别模式需要、还没下载的模型。
    pub missing: Vec<String>,
}

impl ModelStatus {
    /// 已下载的模型（排好序，带版本，例如 qwen3-asr@Qwen/Qwen3-ASR-0.6B），用来判断自检是否覆盖了所有模型。
    pub fn downloaded(&self) -> Vec<String> {
        let mut ids: Vec<String> = self
            .models
            .iter()
            .filter(|model| model.downloaded)
            .map(|model| match &model.variant {
                Some(variant) => format!("{}@{variant}", model.id),
                None => model.id.clone(),
            })
            .collect();
        ids.sort();
        ids
    }

    pub fn can_transcribe(&self) -> bool {
        self.find("qwen3-asr").is_some_and(|model| model.downloaded)
    }

    pub fn needs_download(&self, selection: &str) -> bool {
        // 已保存的选择可能包含旧版模型。有效的带版本选择交给下载器切换版本。
        match selection.split_once('@') {
            Some((id, _variant)) => self.find(id).is_some(),
            None => self.find(selection).is_some_and(|model| !model.downloaded),
        }
    }

    pub fn find(&self, id: &str) -> Option<&ModelInfo> {
        self.models.iter().find(|model| model.id == id)
    }
}

/// 运行环境装好之前用的默认清单（与 Python 的模型清单一致，tests/test_model_catalog.py 检查）。
const DEFAULT_MODELS: &str = include_str!("default_models.json");

#[derive(Deserialize)]
struct DefaultModel {
    id: String,
    zh: String,
    en: String,
    purpose_zh: String,
    purpose_en: String,
    size_mb: u64,
    variant: Option<String>,
    variants: Vec<VariantInfo>,
}

/// 识别模式需要的模型。
pub fn needed(mode: Option<&ModeInfo>) -> Vec<&'static str> {
    match mode {
        Some(mode) if mode.mode == "whisperjav" && mode.whisperjav_ensemble => {
            vec!["qwen3-asr", "anime-whisper"]
        }
        Some(mode) if mode.mode == "whisperjav" => vec!["qwen3-asr"],
        _ => Vec::new(),
    }
}

/// 运行环境还没装好时的模型清单（都没下载）：按当前识别模式标出需要的模型，让用户在下载前选择。
/// 不知道当前识别模式时按主程序的默认模式（WhisperJAV 双遍）勾选。
pub fn default_catalog(mode: Option<ModeInfo>) -> ModelStatus {
    let assumed = mode.clone().unwrap_or(ModeInfo {
        mode: "whisperjav".into(),
        whisperjav_ensemble: true,
        live: false,
    });
    let wanted = needed(Some(&assumed));
    let defaults: Vec<DefaultModel> = serde_json::from_str(DEFAULT_MODELS).unwrap_or_default();
    let models: Vec<ModelInfo> = defaults
        .into_iter()
        .map(|model| ModelInfo {
            needed: wanted.contains(&model.id.as_str()),
            id: model.id,
            zh: model.zh,
            en: model.en,
            purpose_zh: model.purpose_zh,
            purpose_en: model.purpose_en,
            size_mb: Some(model.size_mb),
            downloaded: false,
            variant: model.variant,
            variants: model.variants,
        })
        .collect();
    let missing = models.iter().filter(|model| model.needed).map(|model| model.id.clone()).collect();
    ModelStatus { mode, models, missing }
}

/// 下载进度。
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ModelProgress {
    pub id: String,
    pub zh: String,
    pub en: String,
    pub index: u32,
    pub count: u32,
    pub done_mb: u64,
    pub total_mb: u64,
}

#[derive(Deserialize)]
#[serde(tag = "event", rename_all = "snake_case")]
enum Message {
    Model { id: String, zh: String, en: String, index: u32, count: u32, done_mb: u64, total_mb: u64 },
    ModelDone,
    Deleted,
    Done,
    Error { message: String },
}

#[derive(Deserialize)]
struct AsrSettings {
    mode: String,
    whisperjav_ensemble: bool,
}

impl WorkerEnv {
    /// 连接前从主程序读当前识别模式（接入地址是 http 时）；读不到返回 None。
    pub fn fetch_mode(&self, server_url: &str, token: &str) -> Option<ModeInfo> {
        let protocol = self.protocol_version()?;
        let url = format!("{}/api/worker/asr-settings", server_url.trim_end_matches('/'));
        let headers = [("X-Worker-Token", token), ("X-VideoTurbo-Protocol", protocol.as_str())];
        let (status, body) = process::http_get_url(&url, &headers, std::time::Duration::from_secs(5))?;
        let settings: AsrSettings = (status == 200).then(|| serde_json::from_str(&body).ok()).flatten()?;
        Some(ModeInfo { mode: settings.mode, whisperjav_ensemble: settings.whisperjav_ensemble, live: true })
    }

    fn catalog_args(&self, command: &[&str]) -> Vec<OsString> {
        let mut args: Vec<OsString> =
            vec!["-m".into(), MODULE.into(), "--config".into(), self.config_file().into()];
        args.extend(command.iter().map(OsString::from));
        args
    }

    /// 模型清单；运行环境没装好或还没写配置时返回 None。
    pub fn model_status(&self) -> Option<ModelStatus> {
        if self.needs_install() || !self.config_file().is_file() {
            return None;
        }
        let mut spec = self.spec(self.venv_python(), self.catalog_args(&["status"]), &[]);
        spec.output = self.logs().join("models.log");
        let mut status = None;
        let result = process::run_lines(&spec, |line| {
            if let Ok(parsed) = serde_json::from_str::<ModelStatus>(line) {
                status = Some(parsed);
            }
        });
        result.ok().filter(|exit| exit.success()).and(status)
    }

    /// 依次下载模型，转发进度。
    pub fn download_models(
        &self,
        ids: &[String],
        cancel: &Cancel,
        mut on_progress: impl FnMut(ModelProgress),
    ) -> Result<(), InstallError> {
        let mut command = vec!["download"];
        command.extend(ids.iter().map(String::as_str));
        let mut spec = self.spec(self.venv_python(), self.catalog_args(&command), &[]);
        spec.output = self.logs().join("models.log");
        let mut error = None;
        let status = process::run_lines_cancellable(&spec, cancel, |line| {
            let Some(json) = line.strip_prefix(PROGRESS) else { return };
            match serde_json::from_str::<Message>(json) {
                Ok(Message::Model { id, zh, en, index, count, done_mb, total_mb }) => {
                    on_progress(ModelProgress { id, zh, en, index, count, done_mb, total_mb })
                }
                Ok(Message::Error { message }) => error = Some(message),
                Ok(Message::ModelDone | Message::Deleted | Message::Done) | Err(_) => {}
            }
        })
        .map_err(|error| InstallError::Failed(error.to_string()))?;
        match error {
            Some(message) => Err(InstallError::Failed(format!("下载模型失败：{message}"))),
            None if status.success() => Ok(()),
            None => {
                Err(InstallError::Failed(format!("下载模型失败（退出码 {}）", status.code().unwrap_or(-1))))
            }
        }
    }

    /// 改用模型的另一个版本（写进这台电脑的 worker.yaml）。
    pub fn select_model(&self, id: &str, value: &str) -> Result<(), InstallError> {
        let mut spec = self.spec(self.venv_python(), self.catalog_args(&["select", id, value]), &[]);
        spec.output = self.logs().join("models.log");
        let status =
            process::run_lines(&spec, |_| {}).map_err(|error| InstallError::Failed(error.to_string()))?;
        if status.success() {
            Ok(())
        } else {
            Err(InstallError::Failed(format!("切换模型版本失败（退出码 {}）", status.code().unwrap_or(-1))))
        }
    }

    pub fn delete_model(&self, id: &str) -> Result<(), InstallError> {
        let mut spec = self.spec(self.venv_python(), self.catalog_args(&["delete", id]), &[]);
        spec.output = self.logs().join("models.log");
        let status =
            process::run_lines(&spec, |_| {}).map_err(|error| InstallError::Failed(error.to_string()))?;
        if status.success() {
            Ok(())
        } else {
            Err(InstallError::Failed(format!("删除模型失败（退出码 {}）", status.code().unwrap_or(-1))))
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn mode_is_read_from_the_main_app_before_connecting() {
        use std::io::{Read, Write};
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        let server = std::thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            let mut request = [0u8; 1024];
            let size = stream.read(&mut request).unwrap();
            let request = String::from_utf8_lossy(&request[..size]).to_string();
            let body = r#"{"mode": "whisperjav", "whisperjav_ensemble": false}"#;
            let _ = stream.write_all(format!("HTTP/1.1 200 OK\r\n\r\n{body}").as_bytes());
            request
        });
        let env = WorkerEnv {
            root: std::env::temp_dir(),
            bundle: std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../../.."),
            uv: "uv".into(),
            ffmpeg_dir: "/ffmpeg".into(),
            language: "zh-Hans".into(),
            config_dir: "config".into(),
        };
        let mode = env.fetch_mode(&format!("http://127.0.0.1:{port}/"), "tok").unwrap();
        assert_eq!((mode.mode.as_str(), mode.whisperjav_ensemble, mode.live), ("whisperjav", false, true));
        let request = server.join().unwrap();
        assert!(request.starts_with("GET /api/worker/asr-settings HTTP/1.0"));
        assert!(request.contains("X-Worker-Token: tok") && request.contains("X-VideoTurbo-Protocol: 2"));
        // https 和连不上的地址：不知道模式。
        assert!(env.fetch_mode("https://example.invalid", "tok").is_none());
    }

    #[test]
    fn defaults_follow_the_recognition_mode() {
        let mode = |mode: &str, ensemble| {
            Some(ModeInfo { mode: mode.into(), whisperjav_ensemble: ensemble, live: true })
        };
        assert_eq!(default_catalog(mode("whisperjav", true)).missing, vec!["qwen3-asr", "anime-whisper"]);
        assert_eq!(default_catalog(mode("whisperjav", false)).missing, vec!["qwen3-asr"]);
        // 不知道当前模式：按默认的双遍勾选，但不假装知道模式。
        let unknown = default_catalog(None);
        assert_eq!(unknown.missing, vec!["qwen3-asr", "anime-whisper"]);
        assert!(unknown.mode.is_none());
        assert_eq!(unknown.models.len(), 2);
        assert!(unknown.models.iter().all(|model| !model.downloaded && model.size_mb.is_some()));
        let labels: Vec<&str> = unknown.models[0].variants.iter().map(|v| v.label.as_str()).collect();
        assert_eq!(labels, ["1.7B", "0.6B"]);
        assert_eq!(unknown.models[0].variant.as_deref(), Some("Qwen/Qwen3-ASR-1.7B"));
    }

    #[test]
    fn status_json_from_the_catalog_parses() {
        let json = r#"{"mode": {"mode": "whisperjav", "whisperjav_ensemble": true, "live": true},
            "models": [{"id": "qwen3-asr", "zh": "Qwen3-ASR", "en": "Qwen3-ASR", "purpose_zh": "第一遍",
                        "purpose_en": "pass 1", "size_mb": 4492, "downloaded": true, "needed": true},
                       {"id": "anime-whisper", "zh": "anime-whisper", "en": "anime-whisper",
                        "purpose_zh": "anime-whisper", "purpose_en": "anime-whisper", "size_mb": null,
                        "downloaded": false, "needed": false}],
            "missing": ["anime-whisper"]}"#;
        let status: ModelStatus = serde_json::from_str(json).unwrap();
        assert!(status.can_transcribe());
        assert!(!status.needs_download("faster-whisper@large-v3"));
        assert!(!status.needs_download("qwen3-asr"));
        assert!(status.needs_download("qwen3-asr@Qwen/Qwen3-ASR-0.6B"));
        assert!(status.needs_download("anime-whisper"));
        let mut anime_only = status.clone();
        anime_only.models[0].downloaded = false;
        anime_only.models[1].downloaded = true;
        assert!(!anime_only.can_transcribe());
        assert_eq!(status.downloaded(), vec!["qwen3-asr".to_string()]);
        let mut with_variant = status.clone();
        with_variant.models[0].variant = Some("Qwen/Qwen3-ASR-0.6B".into());
        assert_eq!(with_variant.downloaded(), vec!["qwen3-asr@Qwen/Qwen3-ASR-0.6B".to_string()]);
        assert_eq!(status.missing, vec!["anime-whisper".to_string()]);
        assert_eq!(status.find("anime-whisper").unwrap().size_mb, None);
    }
}
