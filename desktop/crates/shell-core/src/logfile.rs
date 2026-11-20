//! 外壳日志：追加写到日志文件夹，超过上限时把旧文件改名为 `.1` 保留一份。

use std::fs::{self, File, OpenOptions};
use std::io::{self, Write};
use std::path::{Path, PathBuf};
use std::sync::Mutex;
use std::time::{SystemTime, UNIX_EPOCH};

const MAX_BYTES: u64 = 5 * 1024 * 1024;

/// 打开追加写入的日志文件；文件过大时先轮转。子进程的标准输出和错误也写到这里。
pub fn open_append(path: &Path) -> io::Result<File> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)?;
    }
    if fs::metadata(path).map(|meta| meta.len() > MAX_BYTES).unwrap_or(false) {
        let mut rotated = path.as_os_str().to_owned();
        rotated.push(".1");
        let _ = fs::rename(path, PathBuf::from(rotated));
    }
    OpenOptions::new().create(true).append(true).open(path)
}

/// 读取文件末尾最多 `max_lines` 行，用来在出错页面上显示原因。
pub fn tail(path: &Path, max_lines: usize) -> String {
    let Ok(text) = fs::read(path) else {
        return String::new();
    };
    let start = text.len().saturating_sub(64 * 1024);
    let text = String::from_utf8_lossy(&text[start..]);
    let lines: Vec<&str> = text.lines().collect();
    lines[lines.len().saturating_sub(max_lines)..].join("\n")
}

pub struct Logger {
    path: PathBuf,
    lock: Mutex<()>,
}

impl Logger {
    pub fn new(path: PathBuf) -> Logger {
        Logger { path, lock: Mutex::new(()) }
    }

    pub fn path(&self) -> &Path {
        &self.path
    }

    pub fn info(&self, message: &str) {
        let _guard = self.lock.lock().unwrap_or_else(|error| error.into_inner());
        let seconds = SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_secs()).unwrap_or(0);
        if let Ok(mut file) = open_append(&self.path) {
            let _ = writeln!(file, "[{seconds}] {message}");
        }
        eprintln!("[VideoTurbo] {message}");
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn tail_returns_the_last_lines() {
        let dir = std::env::temp_dir().join(format!("vt-log-{}", std::process::id()));
        let path = dir.join("a.log");
        let logger = Logger::new(path.clone());
        for index in 0..5 {
            logger.info(&format!("line {index}"));
        }
        let text = tail(&path, 2);
        assert!(text.ends_with("line 4"), "{text}");
        assert_eq!(text.lines().count(), 2);
        let _ = fs::remove_dir_all(dir);
    }
}
