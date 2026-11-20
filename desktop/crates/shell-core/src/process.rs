//! 子进程：启动时接好标准输入管道，停止时先关闭管道请它有序退出，超时再强制结束。
//!
//! 主程序设置 `VIDEO_TURBO_EXIT_ON_STDIN_CLOSE=1` 后，读到标准输入结束就有序退出。外壳崩溃时
//! 管道同样会关闭，子进程不会留在后台。Windows 上以“不显示窗口”启动，子进程再启动的 ffmpeg 也不会弹窗。

use std::ffi::OsString;
use std::io::{self, BufRead, BufReader, Read, Write};
use std::net::{SocketAddr, TcpListener, TcpStream};
use std::path::PathBuf;
use std::process::{Child, ChildStdin, Command, ExitStatus, Stdio};
use std::sync::mpsc;
use std::thread;
use std::time::{Duration, Instant};

use crate::logfile;

#[derive(Clone, Debug)]
pub struct Spec {
    pub program: PathBuf,
    pub args: Vec<OsString>,
    pub cwd: Option<PathBuf>,
    /// 在继承的环境变量之上覆盖这些。
    pub env: Vec<(String, OsString)>,
    /// 子进程的标准输出和错误追加到这个文件。
    pub output: PathBuf,
}

pub struct Running {
    child: Child,
    stdin: Option<ChildStdin>,
}

#[cfg(windows)]
fn hide_window(command: &mut Command) {
    use std::os::windows::process::CommandExt;
    const CREATE_NO_WINDOW: u32 = 0x0800_0000;
    command.creation_flags(CREATE_NO_WINDOW);
}

#[cfg(not(windows))]
fn hide_window(_command: &mut Command) {}

pub fn spawn(spec: &Spec) -> io::Result<Running> {
    let output = logfile::open_append(&spec.output)?;
    let errors = output.try_clone()?;
    let mut command = Command::new(&spec.program);
    command
        .args(&spec.args)
        .envs(spec.env.iter().map(|(key, value)| (key, value)))
        .stdin(Stdio::piped())
        .stdout(Stdio::from(output))
        .stderr(Stdio::from(errors));
    if let Some(cwd) = &spec.cwd {
        command.current_dir(cwd);
    }
    hide_window(&mut command);
    let mut child = command.spawn()?;
    let stdin = child.stdin.take();
    Ok(Running { child, stdin })
}

/// 运行到结束，逐行交给 `on_line`（标准输出和错误都算），同时追加到 `spec.output`。
pub fn run_lines(spec: &Spec, mut on_line: impl FnMut(&str)) -> io::Result<ExitStatus> {
    let mut log = logfile::open_append(&spec.output)?;
    let mut command = Command::new(&spec.program);
    command
        .args(&spec.args)
        .envs(spec.env.iter().map(|(key, value)| (key, value)))
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    if let Some(cwd) = &spec.cwd {
        command.current_dir(cwd);
    }
    hide_window(&mut command);
    let mut child = command.spawn()?;
    let (sender, receiver) = mpsc::channel::<String>();
    let mut readers = Vec::new();
    for stream in [
        child.stdout.take().map(|s| Box::new(s) as Box<dyn Read + Send>),
        child.stderr.take().map(|s| Box::new(s) as Box<dyn Read + Send>),
    ]
    .into_iter()
    .flatten()
    {
        let sender = sender.clone();
        readers.push(thread::spawn(move || {
            for line in BufReader::new(stream).split(b'\n').map_while(Result::ok) {
                let text = String::from_utf8_lossy(&line).trim_end_matches('\r').to_string();
                if sender.send(text).is_err() {
                    break;
                }
            }
        }));
    }
    drop(sender);
    for line in receiver {
        let _ = writeln!(log, "{line}");
        on_line(&line);
    }
    for reader in readers {
        let _ = reader.join();
    }
    child.wait()
}

impl Running {
    pub fn id(&self) -> u32 {
        self.child.id()
    }

    /// 已退出时返回退出状态。
    pub fn exited(&mut self) -> Option<ExitStatus> {
        self.child.try_wait().ok().flatten()
    }

    /// 关闭标准输入请子进程有序退出；`timeout` 内没退出就强制结束。
    pub fn stop(mut self, timeout: Duration) -> Option<ExitStatus> {
        if let Some(mut stdin) = self.stdin.take() {
            let _ = stdin.flush();
            drop(stdin);
        }
        let deadline = Instant::now() + timeout;
        while Instant::now() < deadline {
            if let Some(status) = self.exited() {
                return Some(status);
            }
            thread::sleep(Duration::from_millis(100));
        }
        let _ = self.child.kill();
        self.child.wait().ok()
    }
}

/// 本机端口上的服务对 `GET path` 返回 200。
pub fn http_ok(port: u16, path: &str, timeout: Duration) -> bool {
    let address = SocketAddr::from(([127, 0, 0, 1], port));
    let Ok(mut stream) = TcpStream::connect_timeout(&address, timeout) else {
        return false;
    };
    let _ = stream.set_read_timeout(Some(timeout));
    let _ = stream.set_write_timeout(Some(timeout));
    let request = format!("GET {path} HTTP/1.0\r\nHost: 127.0.0.1:{port}\r\nConnection: close\r\n\r\n");
    if stream.write_all(request.as_bytes()).is_err() {
        return false;
    }
    let mut head = [0u8; 16];
    let mut read = 0;
    while read < head.len() {
        match stream.read(&mut head[read..]) {
            Ok(0) | Err(_) => break,
            Ok(count) => read += count,
        }
    }
    let line = String::from_utf8_lossy(&head[..read]);
    line.starts_with("HTTP/1.") && line[8..].trim_start().starts_with("200")
}

/// 端口能否在这个地址上监听（启动前检查，给出“端口被占用”的明确提示）。
pub fn port_free(host: [u8; 4], port: u16) -> bool {
    TcpListener::bind(SocketAddr::from((host, port))).is_ok()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn http_ok_reads_the_status_line() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        let server = thread::spawn(move || {
            for status in ["200 OK", "401 Unauthorized"] {
                let (mut stream, _) = listener.accept().unwrap();
                let mut buffer = [0u8; 256];
                let _ = stream.read(&mut buffer);
                let _ = stream.write_all(format!("HTTP/1.1 {status}\r\n\r\n").as_bytes());
            }
        });
        assert!(http_ok(port, "/health", Duration::from_secs(2)));
        assert!(!http_ok(port, "/health", Duration::from_secs(2)));
        server.join().unwrap();
        assert!(!http_ok(port, "/health", Duration::from_millis(200)));
    }

    #[cfg(unix)]
    #[test]
    fn run_lines_streams_both_outputs() {
        let dir = std::env::temp_dir().join(format!("vt-lines-{}", std::process::id()));
        let spec = Spec {
            program: "sh".into(),
            args: vec!["-c".into(), "echo one; echo two >&2; printf 'three'".into()],
            cwd: None,
            env: vec![],
            output: dir.join("out.log"),
        };
        let mut lines = Vec::new();
        let status = run_lines(&spec, |line| lines.push(line.to_string())).unwrap();
        assert!(status.success());
        lines.sort();
        assert_eq!(lines, ["one", "three", "two"]);
        assert!(std::fs::read_to_string(dir.join("out.log")).unwrap().contains("three"));
        let _ = std::fs::remove_dir_all(dir);
    }

    #[cfg(unix)]
    #[test]
    fn stop_closes_stdin_and_waits() {
        let dir = std::env::temp_dir().join(format!("vt-proc-{}", std::process::id()));
        let spec = Spec {
            program: "sh".into(),
            args: vec!["-c".into(), "cat > /dev/null; echo bye".into()],
            cwd: None,
            env: vec![],
            output: dir.join("out.log"),
        };
        let mut running = spawn(&spec).unwrap();
        assert!(running.exited().is_none());
        let status = running.stop(Duration::from_secs(5)).unwrap();
        assert!(status.success());
        assert!(std::fs::read_to_string(dir.join("out.log")).unwrap().contains("bye"));
        let _ = std::fs::remove_dir_all(dir);
    }
}
