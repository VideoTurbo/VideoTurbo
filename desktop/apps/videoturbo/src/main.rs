//! VideoTurbo 桌面版：托盘常驻，管理主程序子进程，在应用内窗口里打开控制台。
//!
//! - 主窗口先显示启动页（ui/index.html），主程序就绪后跳到控制台 http://127.0.0.1:端口/。
//!   控制台是主程序提供的网页，只能调用外壳的 set_theme（见 capabilities/console.json），
//!   让窗口标题栏和外壳自己的页面跟控制台用同一种深浅色。
//! - macOS 主窗口用透明标题栏：控制台顶栏铺到窗口顶端，窗口按钮叠在顶栏左侧。
//! - 关闭主窗口只是隐藏，主程序继续在后台处理；从托盘菜单“退出”才会停止主程序。
//! - 开机自启时带 --autostart 参数，只在托盘里运行，不弹出窗口。

#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod server;
mod texts;

use std::path::PathBuf;
use std::sync::{Arc, Mutex};

use serde::{Deserialize, Serialize};
use shell_core::locale::Lang;
use shell_core::logfile::Logger;
use shell_core::settings::ServerSettings;
use tauri::menu::{CheckMenuItem, Menu, MenuItem, PredefinedMenuItem};
use tauri::tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent};
use tauri::{AppHandle, Manager, RunEvent, Theme, WebviewUrl, WebviewWindowBuilder, WindowEvent, Wry};
use tauri_plugin_autostart::{MacosLauncher, ManagerExt as _};
use tauri_plugin_opener::OpenerExt as _;

use server::{Paths, Phase, Server};
use shell_core::connection::Connection;
use shell_core::worker_env::WorkerEnv;
use shell_core::worker_manager::{Connect, WorkerManager, WorkerPhase};
use texts::Texts;

const AUTOSTART_ARG: &str = "--autostart";
const MAIN: &str = "main";
const SETTINGS: &str = "settings";
const WORKER: &str = "worker";

struct Shell {
    server: Arc<Server>,
    worker: Arc<WorkerManager>,
    texts: Arc<Texts>,
    logger: Arc<Logger>,
    status_item: MenuItem<Wry>,
    worker_item: MenuItem<Wry>,
    autostart_item: CheckMenuItem<Wry>,
    /// 控制台选的主题：None 为跟随系统。记在数据文件夹里，下次打开时窗口一出现就是这个颜色。
    theme: Mutex<Option<Theme>>,
    theme_file: PathBuf,
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
    let theme = current_theme(app);
    // 每次加载页面前执行：告诉控制台外壳记住的主题（换了端口、浏览器存储为空时沿用），
    // 以及 macOS 上顶栏要给窗口按钮让位。
    let mut script = format!("window.__VIDEO_TURBO_SHELL_THEME__ = '{}';", theme_name(theme));
    if cfg!(target_os = "macos") {
        script.push_str("document.documentElement.dataset.titlebar = 'overlay';");
    }
    let builder = WebviewWindowBuilder::new(app, MAIN, WebviewUrl::App("index.html".into()))
        .title("VideoTurbo")
        .inner_size(1280.0, 820.0)
        .min_inner_size(900.0, 600.0)
        .theme(theme)
        .initialization_script(script)
        .visible(visible);
    // 窗口按钮摆在 52px 高的顶栏中线上。
    #[cfg(target_os = "macos")]
    let builder = builder
        .title_bar_style(tauri::TitleBarStyle::Overlay)
        .hidden_title(true)
        .traffic_light_position(tauri::LogicalPosition::new(18.0, 24.0));
    builder.build()?;
    Ok(())
}

fn current_theme(app: &AppHandle) -> Option<Theme> {
    *shell(app).theme.lock().unwrap()
}

fn theme_name(theme: Option<Theme>) -> &'static str {
    match theme {
        Some(Theme::Light) => "light",
        Some(Theme::Dark) => "dark",
        _ => "system",
    }
}

/// 读记住的主题；文件不存在或内容不认识时跟随系统。
fn load_theme(file: &std::path::Path) -> Option<Theme> {
    match std::fs::read_to_string(file).unwrap_or_default().trim() {
        "light" => Some(Theme::Light),
        "dark" => Some(Theme::Dark),
        _ => None,
    }
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
        .theme(current_theme(app))
        .inner_size(560.0, 620.0)
        .resizable(false)
        .build();
}

fn show_worker(app: &AppHandle) {
    if let Some(window) = app.get_webview_window(WORKER) {
        let _ = window.show();
        let _ = window.set_focus();
        return;
    }
    let title = shell(app).texts.local_worker_title();
    let _ = WebviewWindowBuilder::new(app, WORKER, WebviewUrl::App("worker.html".into()))
        .title(title)
        .theme(current_theme(app))
        .inner_size(600.0, 520.0)
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
    let worker = MenuItem::with_id(app, "worker", texts.local_worker(), true, None::<&str>)?;
    let data = MenuItem::with_id(app, "data", texts.open_data(), true, None::<&str>)?;
    let logs = MenuItem::with_id(app, "logs", texts.open_logs(), true, None::<&str>)?;
    let restart = MenuItem::with_id(app, "restart", texts.restart(), true, None::<&str>)?;
    let quit = MenuItem::with_id(app, "quit", texts.quit(), true, None::<&str>)?;
    let menu = Menu::with_items(
        app,
        &[
            &state.status_item,
            &state.worker_item,
            &PredefinedMenuItem::separator(app)?,
            &open,
            &worker,
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
            "worker" => show_worker(app),
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

/// 控制台切换主题时调用（这是控制台唯一能调用的命令）：system / light / dark。
#[tauri::command]
fn set_theme(app: AppHandle, theme: String) -> Result<(), String> {
    let choice = match theme.as_str() {
        "system" => None,
        "light" => Some(Theme::Light),
        "dark" => Some(Theme::Dark),
        _ => return Err(format!("unknown theme: {theme}")),
    };
    let state = shell(&app);
    {
        let mut current = state.theme.lock().unwrap();
        if *current == choice {
            return Ok(());
        }
        *current = choice;
    }
    if let Some(dir) = state.theme_file.parent() {
        let _ = std::fs::create_dir_all(dir);
    }
    if let Err(error) = std::fs::write(&state.theme_file, theme_name(choice)) {
        state.logger.info(&format!("无法保存主题：{error}"));
    }
    for window in app.webview_windows().values() {
        let _ = window.set_theme(choice);
    }
    Ok(())
}

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
        // 本机识别在它自己的窗口里开关，设置页不改它。
        local_worker: state.server.settings().local_worker,
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

/// 本机识别的接入信息：等主程序就绪后，凭这次启动的令牌从本机接口取接入密钥和端口。
fn local_connection(server: Arc<Server>) -> Connect {
    #[derive(Deserialize)]
    struct Access {
        worker_token: String,
        worker_port: u16,
    }
    Box::new(move |alive: &dyn Fn() -> bool| loop {
        if !alive() {
            return None;
        }
        if let Some((port, token)) = server.ready() {
            let reply = shell_core::process::http_get(
                port,
                "/api/auth/desktop/worker",
                &[("X-VideoTurbo-Desktop-Token", &token)],
                std::time::Duration::from_secs(5),
            );
            if let Some((200, body)) = reply {
                if let Ok(access) = serde_json::from_str::<Access>(&body) {
                    return Some(Connection {
                        server_url: format!("http://127.0.0.1:{}", access.worker_port),
                        token: access.worker_token,
                    });
                }
            }
        }
        std::thread::sleep(std::time::Duration::from_secs(1));
    })
}

#[derive(Serialize)]
struct WorkerView {
    #[serde(flatten)]
    phase: WorkerPhase,
    enabled: bool,
    platform: &'static str,
    /// Windows 上是否检测到 NVIDIA 显卡（没有时只能用 CPU，很慢）；macOS 为 None。
    nvidia: Option<bool>,
}

#[tauri::command]
fn worker_state(app: AppHandle) -> WorkerView {
    let state = shell(&app);
    WorkerView {
        phase: state.worker.phase(),
        enabled: state.server.settings().local_worker,
        platform: if cfg!(target_os = "macos") { "macos" } else { "windows" },
        nvidia: shell_core::worker_env::has_nvidia(),
    }
}

/// 开启或关闭本机识别；失败后再点开启即重试。
#[tauri::command]
fn worker_set(app: AppHandle, enabled: bool) -> Result<(), String> {
    let state = shell(&app);
    state.server.set_local_worker(enabled).map_err(|error| error.to_string())?;
    if enabled {
        state.worker.start(Some(local_connection(Arc::clone(&state.server))));
    } else {
        let worker = Arc::clone(&state.worker);
        std::thread::spawn(move || worker.stop());
    }
    Ok(())
}

#[tauri::command]
fn worker_open_logs(app: AppHandle) {
    let folder = shell(&app).worker.env().logs();
    open_folder(&app, folder);
}

/// macOS：打开 Apple 的命令行开发工具安装程序（含 Git）。
#[tauri::command]
fn worker_install_tools() -> Result<(), String> {
    std::process::Command::new("xcode-select")
        .arg("--install")
        .spawn()
        .map(|_| ())
        .map_err(|error| error.to_string())
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
    let theme_file = home.join("desktop-theme");
    let theme = Mutex::new(load_theme(&theme_file));

    let status_item = MenuItem::with_id(app, "status", texts.status_stopped(), false, None::<&str>)?;
    let worker_item =
        MenuItem::with_id(app, "worker_status", texts.worker_status(&WorkerPhase::Off), false, None::<&str>)?;
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
            // 主程序（重新）就绪后接上本机识别：端口可能变了。这里还持有主程序的锁，放到线程里做。
            if matches!(phase, Phase::Running { .. }) {
                let handle = handle.clone();
                std::thread::spawn(move || {
                    if let Some(state) = handle.try_state::<Shell>() {
                        if state.server.settings().local_worker {
                            state.worker.start(Some(local_connection(Arc::clone(&state.server))));
                        }
                    }
                });
            }
        })
    };
    let worker_env = WorkerEnv {
        root: home.join("worker"),
        bundle: paths.resources.join("worker-bundle"),
        uv: paths.resources.join("uv").join(if cfg!(windows) { "uv.exe" } else { "uv" }),
        ffmpeg_dir: paths.resources.join("ffmpeg"),
        language: texts.lang.server_code().into(),
        config_dir: "config".into(),
    };
    let worker_notify = {
        let worker_item = worker_item.clone();
        let texts = Arc::clone(&texts);
        Box::new(move |phase: &WorkerPhase| {
            let _ = worker_item.set_text(texts.worker_status(phase));
        })
    };
    let worker = WorkerManager::new(worker_env, Arc::clone(&logger), worker_notify);
    let server = Server::new(paths, settings, Arc::clone(&texts), Arc::clone(&logger), notify);
    let state = Shell {
        server: Arc::clone(&server),
        worker,
        texts,
        logger,
        status_item,
        worker_item,
        autostart_item,
        theme,
        theme_file,
    };
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
            set_theme,
            server_state,
            restart_server,
            open_logs,
            get_settings,
            save_settings,
            worker_state,
            worker_set,
            worker_open_logs,
            worker_install_tools
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
                state.worker.stop();
                state.server.shutdown();
                state.logger.info("VideoTurbo 桌面版退出");
            }
        }
        _ => {}
    });
}
