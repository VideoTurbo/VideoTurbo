//! VideoTurbo 桌面版：托盘常驻，管理主程序子进程，在应用内窗口里打开控制台。
//!
//! - 主窗口先显示启动页（ui/index.html），主程序就绪后跳到控制台 http://127.0.0.1:端口/。
//!   控制台是主程序提供的网页，不在 capabilities 里，拿不到外壳的接口。
//! - 关闭主窗口只是隐藏，主程序继续在后台处理；从托盘菜单“退出”才会停止主程序。
//! - 开机自启时带 --autostart 参数，只在托盘里运行，不弹出窗口。

#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod server;
mod texts;

use std::path::PathBuf;
use std::sync::Arc;

use serde::{Deserialize, Serialize};
use shell_core::locale::Lang;
use shell_core::logfile::Logger;
use shell_core::settings::ServerSettings;
use tauri::menu::{CheckMenuItem, Menu, MenuItem, PredefinedMenuItem};
use tauri::tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent};
use tauri::{AppHandle, Manager, RunEvent, WebviewUrl, WebviewWindowBuilder, WindowEvent, Wry};
use tauri_plugin_autostart::{MacosLauncher, ManagerExt as _};
use tauri_plugin_opener::OpenerExt as _;

use server::{Paths, Phase, Server};
use texts::Texts;

const AUTOSTART_ARG: &str = "--autostart";
const MAIN: &str = "main";
const SETTINGS: &str = "settings";

struct Shell {
    server: Arc<Server>,
    texts: Arc<Texts>,
    logger: Arc<Logger>,
    status_item: MenuItem<Wry>,
    autostart_item: CheckMenuItem<Wry>,
}

fn shell(app: &AppHandle) -> tauri::State<'_, Shell> {
    app.state::<Shell>()
}

fn status_text(texts: &Texts, phase: &Phase) -> &'static str {
    match phase {
        Phase::Starting => texts.status_starting(),
        Phase::Running { .. } => texts.status_running(),
        Phase::Failed { .. } => texts.status_failed(),
        Phase::Stopped => texts.status_stopped(),
    }
}

/// 显示主窗口；还没有就新建（启动页会自己等主程序就绪后跳到控制台）。
fn show_main(app: &AppHandle) {
    if let Some(window) = app.get_webview_window(MAIN) {
        let _ = window.unminimize();
        let _ = window.show();
        let _ = window.set_focus();
        return;
    }
    let _ = create_main(app, true);
}

fn create_main(app: &AppHandle, visible: bool) -> tauri::Result<()> {
    WebviewWindowBuilder::new(app, MAIN, WebviewUrl::App("index.html".into()))
        .title("VideoTurbo")
        .inner_size(1280.0, 820.0)
        .min_inner_size(900.0, 600.0)
        .visible(visible)
        .build()?;
    Ok(())
}

/// 主程序重新启动后，已打开的控制台连不上了：主窗口回到启动页，等新进程就绪。
fn reload_main(app: &AppHandle) {
    if let Some(window) = app.get_webview_window(MAIN) {
        if let Ok(url) = window.url() {
            if url.scheme().starts_with("http") && url.host_str() == Some("127.0.0.1") {
                let visible = window.is_visible().unwrap_or(false);
                let _ = window.destroy();
                let _ = create_main(app, visible);
            }
        }
    }
}

fn show_settings(app: &AppHandle) {
    if let Some(window) = app.get_webview_window(SETTINGS) {
        let _ = window.show();
        let _ = window.set_focus();
        return;
    }
    let title = shell(app).texts.settings_title();
    let _ = WebviewWindowBuilder::new(app, SETTINGS, WebviewUrl::App("settings.html".into()))
        .title(title)
        .inner_size(560.0, 620.0)
        .resizable(false)
        .build();
}

fn open_folder(app: &AppHandle, folder: PathBuf) {
    let _ = std::fs::create_dir_all(&folder);
    if let Err(error) = app.opener().open_path(folder.to_string_lossy(), None::<&str>) {
        shell(app).logger.info(&format!("无法打开文件夹 {}：{error}", folder.display()));
    }
}

fn build_tray(app: &AppHandle, state: &Shell) -> tauri::Result<()> {
    let texts = &state.texts;
    let open = MenuItem::with_id(app, "open", texts.open_console(), true, None::<&str>)?;
    let settings = MenuItem::with_id(app, "settings", texts.settings(), true, None::<&str>)?;
    let data = MenuItem::with_id(app, "data", texts.open_data(), true, None::<&str>)?;
    let logs = MenuItem::with_id(app, "logs", texts.open_logs(), true, None::<&str>)?;
    let restart = MenuItem::with_id(app, "restart", texts.restart(), true, None::<&str>)?;
    let quit = MenuItem::with_id(app, "quit", texts.quit(), true, None::<&str>)?;
    let menu = Menu::with_items(
        app,
        &[
            &state.status_item,
            &PredefinedMenuItem::separator(app)?,
            &open,
            &settings,
            &state.autostart_item,
            &PredefinedMenuItem::separator(app)?,
            &data,
            &logs,
            &restart,
            &PredefinedMenuItem::separator(app)?,
            &quit,
        ],
    )?;
    let mut builder = TrayIconBuilder::with_id("main").tooltip("VideoTurbo").menu(&menu);
    // macOS 菜单栏用单色模板图标，随深浅色自动反色；Windows 托盘用彩色应用图标。
    let template = app.path().resource_dir().ok().map(|dir| dir.join("icons").join("tray.png"));
    match template.and_then(|path| tauri::image::Image::from_path(path).ok()) {
        Some(icon) if cfg!(target_os = "macos") => builder = builder.icon(icon).icon_as_template(true),
        _ => {
            if let Some(icon) = app.default_window_icon() {
                builder = builder.icon(icon.clone());
            }
        }
    }
    builder
        // Windows 习惯左键打开窗口、右键弹出菜单；macOS 点一下就弹出菜单。
        .show_menu_on_left_click(cfg!(target_os = "macos"))
        .on_menu_event(|app, event| match event.id.as_ref() {
            "open" => show_main(app),
            "settings" => show_settings(app),
            "autostart" => toggle_autostart(app),
            "data" => open_folder(app, shell(app).server.paths().home.clone()),
            "logs" => open_folder(app, shell(app).server.paths().logs()),
            "restart" => {
                let server = Arc::clone(&shell(app).server);
                std::thread::spawn(move || server.start(true));
            }
            "quit" => app.exit(0),
            _ => {}
        })
        .on_tray_icon_event(|tray, event| {
            if let TrayIconEvent::Click {
                button: MouseButton::Left,
                button_state: MouseButtonState::Up,
                ..
            } = event
            {
                if !cfg!(target_os = "macos") {
                    show_main(tray.app_handle());
                }
            }
        })
        .build(app)?;
    Ok(())
}

fn toggle_autostart(app: &AppHandle) {
    let launcher = app.autolaunch();
    let enabled = launcher.is_enabled().unwrap_or(false);
    let result = if enabled { launcher.disable() } else { launcher.enable() };
    if let Err(error) = result {
        shell(app).logger.info(&format!("设置开机自启失败：{error}"));
    }
    let _ = shell(app).autostart_item.set_checked(launcher.is_enabled().unwrap_or(false));
}

// ---------------------------------------------------------------- 外壳页面调用的命令

#[tauri::command]
fn server_state(app: AppHandle) -> Phase {
    shell(&app).server.phase()
}

#[tauri::command]
fn restart_server(app: AppHandle) {
    let server = Arc::clone(&shell(&app).server);
    std::thread::spawn(move || server.start(true));
}

#[tauri::command]
fn open_logs(app: AppHandle) {
    let folder = shell(&app).server.paths().logs();
    open_folder(&app, folder);
}

#[derive(Serialize)]
struct SettingsView {
    settings: ServerSettings,
    default_backup_dir: PathBuf,
    data_dir: PathBuf,
}

#[tauri::command]
fn get_settings(app: AppHandle) -> SettingsView {
    let server = &shell(&app).server;
    SettingsView {
        settings: server.settings(),
        default_backup_dir: server.paths().home.join("backups"),
        data_dir: server.paths().home.clone(),
    }
}

#[derive(Deserialize)]
struct SettingsInput {
    console_port: u16,
    worker_port: u16,
    lan_console: bool,
    backup_dir: Option<String>,
    safe_mode: bool,
}

/// 保存设置并重新启动主程序；返回的错误文字直接显示在设置页上。
#[tauri::command]
fn save_settings(app: AppHandle, input: SettingsInput) -> Result<(), String> {
    let state = shell(&app);
    let settings = ServerSettings {
        console_port: input.console_port,
        worker_port: input.worker_port,
        lan_console: input.lan_console,
        backup_dir: input
            .backup_dir
            .map(|dir| dir.trim().to_string())
            .filter(|dir| !dir.is_empty())
            .map(PathBuf::from),
        safe_mode: input.safe_mode,
    };
    settings.validate().map_err(|invalid| state.texts.invalid_settings(&invalid))?;
    if settings == state.server.settings() {
        return Ok(());
    }
    let server = Arc::clone(&state.server);
    let logger = Arc::clone(&state.logger);
    std::thread::spawn(move || {
        if let Err(error) = server.apply(settings) {
            logger.info(&format!("保存设置失败：{error}"));
        }
    });
    Ok(())
}

// ---------------------------------------------------------------- 启动

fn setup(app: &mut tauri::App) -> Result<(), Box<dyn std::error::Error>> {
    let handle = app.handle().clone();
    let home = app.path().local_data_dir()?.join("VideoTurbo");
    let paths = Paths {
        home: home.clone(),
        resources: app.path().resource_dir()?,
        user_home: app.path().home_dir().ok(),
    };
    let logger = Arc::new(Logger::new(paths.logs().join("desktop.log")));
    logger.info(&format!(
        "VideoTurbo 桌面版 {} 启动，数据文件夹 {}",
        env!("CARGO_PKG_VERSION"),
        home.display()
    ));
    let texts = Arc::new(Texts::new(Lang::detect()));
    let settings = ServerSettings::load(&paths.settings_file());

    let status_item = MenuItem::with_id(app, "status", texts.status_stopped(), false, None::<&str>)?;
    let autostart_enabled = app.autolaunch().is_enabled().unwrap_or(false);
    let autostart_item =
        CheckMenuItem::with_id(app, "autostart", texts.autostart(), true, autostart_enabled, None::<&str>)?;

    let notify = {
        let handle = handle.clone();
        let status_item = status_item.clone();
        let texts = Arc::clone(&texts);
        Box::new(move |phase: &Phase| {
            let text = status_text(&texts, phase);
            let _ = status_item.set_text(text);
            if matches!(phase, Phase::Starting) {
                let handle = handle.clone();
                let _ = handle.clone().run_on_main_thread(move || reload_main(&handle));
            }
        })
    };
    let server = Server::new(paths, settings, Arc::clone(&texts), Arc::clone(&logger), notify);
    let state = Shell { server: Arc::clone(&server), texts, logger, status_item, autostart_item };
    build_tray(&handle, &state)?;
    app.manage(state);

    // 主窗口始终存在（自启时隐藏），这样关闭设置窗口不会被当成“最后一个窗口关闭”而退出。
    let autostarted = std::env::args().any(|arg| arg == AUTOSTART_ARG);
    create_main(&handle, !autostarted)?;
    std::thread::spawn(move || server.start(true));
    Ok(())
}

fn main() {
    let app = tauri::Builder::default()
        // 必须最先注册：再次打开应用时只把已运行的实例的窗口调出来。
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| show_main(app)))
        .plugin(tauri_plugin_autostart::init(MacosLauncher::LaunchAgent, Some(vec![AUTOSTART_ARG])))
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_opener::init())
        .invoke_handler(tauri::generate_handler![
            server_state,
            restart_server,
            open_logs,
            get_settings,
            save_settings
        ])
        .on_window_event(|window, event| {
            if let WindowEvent::CloseRequested { api, .. } = event {
                if window.label() == MAIN {
                    api.prevent_close();
                    let _ = window.hide();
                }
            }
        })
        .setup(setup)
        .build(tauri::generate_context!())
        .expect("VideoTurbo 桌面版启动失败");

    app.run(|app, event| match event {
        // macOS：点 Dock 图标时重新显示主窗口。
        #[cfg(target_os = "macos")]
        RunEvent::Reopen { .. } => show_main(app),
        RunEvent::Exit => {
            if let Some(state) = app.try_state::<Shell>() {
                state.server.shutdown();
                state.logger.info("VideoTurbo 桌面版退出");
            }
        }
        _ => {}
    });
}
