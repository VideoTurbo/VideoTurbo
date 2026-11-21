fn main() {
    // 列出外壳的命令，生成 allow-<命令> 权限；哪个页面能调用哪个命令写在 capabilities 里。
    let commands = &[
        "set_theme",
        "server_state",
        "restart_server",
        "open_logs",
        "get_settings",
        "save_settings",
        "worker_state",
        "worker_set",
        "worker_open_logs",
        "worker_install_tools",
    ];
    tauri_build::try_build(
        tauri_build::Attributes::new().app_manifest(tauri_build::AppManifest::new().commands(commands)),
    )
    .expect("failed to run tauri-build");
}
