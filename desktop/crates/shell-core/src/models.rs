//! 识别模型清单：在识别程序的环境里运行 `video_turbo.worker.model_catalog`，列出、下载、删除模型。
//!
//! 每种识别模式要的模型不同（WhisperJAV：Qwen3-ASR，双遍再加 anime-whisper；faster-whisper：Whisper large-v3）。
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
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct ModelStatus {
    pub mode: Option<ModeInfo>,
    pub models: Vec<ModelInfo>,
    /// 当前识别模式需要、还没下载的模型。
    pub missing: Vec<String>,
    /// Windows：faster-whisper 要的显卡运行库是否找得到。
    pub runtime_ready: bool,
}

impl ModelStatus {
    /// 已下载的模型（排好序），用来判断自检是否覆盖了所有模型。
    pub fn downloaded(&self) -> Vec<String> {
        let mut ids: Vec<String> =
            self.models.iter().filter(|model| model.downloaded).map(|model| model.id.clone()).collect();
        ids.sort();
        ids
    }

    pub fn find(&self, id: &str) -> Option<&ModelInfo> {
        self.models.iter().find(|model| model.id == id)
    }
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

impl WorkerEnv {
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
    fn status_json_from_the_catalog_parses() {
        let json = r#"{"mode": {"mode": "whisperjav", "whisperjav_ensemble": true, "live": true},
            "models": [{"id": "qwen3-asr", "zh": "Qwen3-ASR", "en": "Qwen3-ASR", "purpose_zh": "第一遍",
                        "purpose_en": "pass 1", "size_mb": 4492, "downloaded": true, "needed": true},
                       {"id": "faster-whisper", "zh": "Whisper large-v3", "en": "Whisper large-v3",
                        "purpose_zh": "faster-whisper", "purpose_en": "faster-whisper", "size_mb": null,
                        "downloaded": false, "needed": false}],
            "missing": ["anime-whisper"], "runtime_ready": true}"#;
        let status: ModelStatus = serde_json::from_str(json).unwrap();
        assert_eq!(status.downloaded(), vec!["qwen3-asr".to_string()]);
        assert_eq!(status.missing, vec!["anime-whisper".to_string()]);
        assert_eq!(status.find("faster-whisper").unwrap().size_mb, None);
    }
}
