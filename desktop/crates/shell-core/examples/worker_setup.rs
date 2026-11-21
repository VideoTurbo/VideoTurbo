//! 手动试运行识别环境的安装：
//!
//!   cargo run -p shell-core --example worker_setup -- <运行环境目录> <resources 目录> [configure <接入串> | install <auto|cpu>]
//!
//! resources 目录由 deploy/desktop/build_sidecar.py 生成（desktop/apps/videoturbo/resources）。

use std::path::PathBuf;

use shell_core::worker_env::{Event, WorkerEnv};

fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let [root, resources, command, value] = &args[..] else {
        eprintln!(
            "用法：worker_setup <运行环境目录> <resources 目录> configure <接入串> | install <auto|cpu>"
        );
        std::process::exit(2);
    };
    let resources = PathBuf::from(resources).canonicalize().expect("找不到 resources 目录");
    let uv = resources.join("uv").join(if cfg!(windows) { "uv.exe" } else { "uv" });
    let env = WorkerEnv {
        root: root.into(),
        bundle: resources.join("worker-bundle"),
        uv,
        ffmpeg_dir: resources.join("ffmpeg"),
        language: "zh-Hans".into(),
        config_dir: "config".into(),
    };
    let result = match command.as_str() {
        "configure" => {
            let connection = shell_core::connection::parse(value).expect("接入串无效");
            env.configure(&connection, true)
        }
        "install" => env.install(value, &shell_core::process::Cancel::default(), |event| match event {
            Event::Step { index, total, zh, .. } => println!("[{index}/{total}] {zh}"),
            Event::Log(line) => println!("    {line}"),
        }),
        other => panic!("未知命令 {other}"),
    };
    match result {
        Ok(()) => println!("完成。configured={} installed={:?}", env.configured(), env.installed_version()),
        Err(error) => {
            eprintln!("失败：{}", error.message());
            std::process::exit(1);
        }
    }
}
