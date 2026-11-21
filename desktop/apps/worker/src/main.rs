//! VideoTurbo Worker：让这台显卡电脑为别处的 VideoTurbo 主程序识别字幕。
//!
//! - 第一次打开时粘贴控制台“云主机 → 添加显卡电脑”给出的接入串，之后自动安装运行环境、下载模型并开始接单。
//! - 托盘常驻，关闭窗口只是隐藏；开机自启时带 --autostart，只在托盘里运行。
//! - 运行环境与主程序应用的“本机识别”共用（%LOCALAPPDATA%\VideoTurbo\worker），配置各自一份（config-worker）。
//!   同一台电脑只允许一个识别程序用显卡，见 shell_core::worker_manager。

#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::path::PathBuf;
use std::sync::Arc;

use serde::{Deserialize, Serialize};
use shell_core::connection;
use shell_core::locale::Lang;
use shell_core::logfile::Logger;
use shell_core::worker_env::{self, WorkerEnv};
use shell_core::worker_manager::{WorkerManager, WorkerPhase};
use shell_tauri::updater::{self, UpdateMenu};
use tauri::menu::{CheckMenuItem, Menu, MenuItem, PredefinedMenuItem};
use tauri::tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent};
use tauri::{AppHandle, Manager, RunEvent, WebviewUrl, WebviewWindowBuilder, WindowEvent, Wry};
use tauri_plugin_autostart::{MacosLauncher, ManagerExt as _};
use tauri_plugin_opener::OpenerExt as _;

const AUTOSTART_ARG: &str = "--autostart";
const MAIN: &str = "main";

/// 识别机应用自己的设置（与主程序应用的 desktop.json 分开）。
#[derive(Clone, Default, Serialize, Deserialize)]
#[serde(default)]
struct AppSettings {
    /// 暂停接单：识别程序停着，下次打开应用也不自动启动。
    paused: bool,
    /// 粘贴后还没写进配置的接入串（选了稍后下载，或下载还没完成）。写进配置后清掉。
    pending_code: Option<String>,
    /// 用户同意下载运行环境和模型。
    download_approved: bool,
}

struct App {
    worker: Arc<WorkerManager>,
    lang: Lang,
    logger: Arc<Logger>,
    settings_file: PathBuf,
    pause_item: MenuItem<Wry>,
    autostart_item: CheckMenuItem<Wry>,
    update_item: MenuItem<Wry>,
    updates: Arc<UpdateMenu>,
}

fn app(handle: &AppHandle) -> tauri::State<'_, App> {
    handle.state::<App>()
}

fn load_settings(path: &PathBuf) -> AppSettings {
    std::fs::read(path).ok().and_then(|data| serde_json::from_slice(&data).ok()).unwrap_or_default()
}

fn save_settings(path: &PathBuf, settings: &AppSettings) {
    if let Some(parent) = path.parent() {
        let _ = std::fs::create_dir_all(parent);
    }
    if let Ok(data) = serde_json::to_vec_pretty(settings) {
        let _ = std::fs::write(path, data);
    }
}

fn status_text(lang: Lang, phase: &WorkerPhase) -> String {
    let text = match phase {
        WorkerPhase::Off => lang.pick("已暂停接单", "Paused"),
        WorkerPhase::Pending => lang.pick("等待下载", "Waiting to download"),
        WorkerPhase::Waiting => lang.pick("正在连接主程序…", "Connecting…"),
        WorkerPhase::Installing { index, total, .. } => {
            return match lang {
                Lang::Zh => format!("正在安装（{index}/{total}）…"),
                Lang::En => format!("Installing ({index}/{total})…"),
            }
        }
        WorkerPhase::NeedTools => lang.pick("需要安装命令行工具", "Command line tools needed"),
        WorkerPhase::Models { done_mb, total_mb, .. } => {
            return match lang {
                Lang::Zh => format!("正在下载模型（{done_mb}/{total_mb} MB）…"),
                Lang::En => format!("Downloading models ({done_mb}/{total_mb} MB)…"),
            }
        }
        WorkerPhase::SelfTest { .. } => lang.pick("正在自检…", "Running the self-test…"),
        WorkerPhase::Running => lang.pick("运行中", "Running"),
        WorkerPhase::Conflict => lang
            .pick("主程序应用的本机识别正在使用显卡", "Local recognition in the main app is using the GPU"),
        WorkerPhase::Failed { .. } => lang.pick("出错了", "Error"),
    };
    text.to_string()
}

fn show_main(handle: &AppHandle) {
    if let Some(window) = handle.get_webview_window(MAIN) {
        let _ = window.unminimize();
        let _ = window.show();
        let _ = window.set_focus();
        return;
    }
    let _ = create_main(handle, true);
}

fn create_main(handle: &AppHandle, visible: bool) -> tauri::Result<()> {
    let lang = Lang::detect();
    WebviewWindowBuilder::new(handle, MAIN, WebviewUrl::App("index.html".into()))
        .title(lang.pick("VideoTurbo Worker", "VideoTurbo Worker"))
        .inner_size(620.0, 760.0)
        .resizable(false)
        .visible(visible)
        .build()?;
    Ok(())
}

fn open_logs_folder(handle: &AppHandle) {
    let folder = app(handle).worker.env().logs();
    let _ = std::fs::create_dir_all(&folder);
    let _ = handle.opener().open_path(folder.to_string_lossy(), None::<&str>);
}

/// 按保存的状态启动：有还没写进配置的接入串就用它，否则沿用已写好的配置。
/// 用户还没同意下载时，需要下载就停在“待下载”。
fn resume(worker: &Arc<WorkerManager>, settings: &AppSettings) {
    if let Some(connection) = settings.pending_code.as_deref().and_then(connection::parse) {
        worker.start(
            Some(Box::new(move |_alive: &dyn Fn() -> bool| Some(connection))),
            settings.download_approved,
        );
    } else if worker.env().configured() {
        worker.start(None, true);
    }
}

fn set_paused(handle: &AppHandle, paused: bool) {
    let state = app(handle);
    let mut settings = load_settings(&state.settings_file);
    settings.paused = paused;
    save_settings(&state.settings_file, &settings);
    let _ = state.pause_item.set_text(if paused {
        state.lang.pick("继续接单", "Resume")
    } else {
        state.lang.pick("暂停接单", "Pause")
    });
    let worker = Arc::clone(&state.worker);
    if paused {
        std::thread::spawn(move || worker.stop());
    } else {
        resume(&worker, &settings);
    }
}

fn build_tray(handle: &AppHandle, state: &App, status_item: &MenuItem<Wry>) -> tauri::Result<()> {
    let lang = state.lang;
    let open = MenuItem::with_id(
        handle,
        "open",
        lang.pick("打开 VideoTurbo Worker", "Open VideoTurbo Worker"),
        true,
        None::<&str>,
    )?;
    let logs = MenuItem::with_id(
        handle,
        "logs",
        lang.pick("打开日志文件夹", "Open logs folder"),
        true,
        None::<&str>,
    )?;
    let quit = MenuItem::with_id(handle, "quit", lang.pick("退出", "Quit"), true, None::<&str>)?;
    let menu = Menu::with_items(
        handle,
        &[
            status_item,
            &PredefinedMenuItem::separator(handle)?,
            &open,
            &state.pause_item,
            &state.autostart_item,
            &state.update_item,
            &logs,
            &PredefinedMenuItem::separator(handle)?,
            &quit,
        ],
    )?;
    let mut builder = TrayIconBuilder::with_id("main").tooltip("VideoTurbo Worker").menu(&menu);
    let template = handle.path().resource_dir().ok().map(|dir| dir.join("icons").join("tray.png"));
    match template.and_then(|path| tauri::image::Image::from_path(path).ok()) {
        Some(icon) if cfg!(target_os = "macos") => builder = builder.icon(icon).icon_as_template(true),
        _ => {
            if let Some(icon) = handle.default_window_icon() {
                builder = builder.icon(icon.clone());
            }
        }
    }
    builder
        .show_menu_on_left_click(cfg!(target_os = "macos"))
        .on_menu_event(|handle, event| match event.id.as_ref() {
            "open" => show_main(handle),
            "pause" => {
                let paused = !load_settings(&app(handle).settings_file).paused;
                set_paused(handle, paused);
            }
            "autostart" => {
                let launcher = handle.autolaunch();
                let enabled = launcher.is_enabled().unwrap_or(false);
                let _ = if enabled { launcher.disable() } else { launcher.enable() };
                let _ = app(handle).autostart_item.set_checked(launcher.is_enabled().unwrap_or(false));
            }
            "logs" => open_logs_folder(handle),
            "update" => app(handle).updates.on_click(handle.clone()),
            "quit" => handle.exit(0),
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
        .build(handle)?;
    Ok(())
}

// ---------------------------------------------------------------- 页面调用的命令

#[derive(Serialize)]
struct View {
    #[serde(flatten)]
    phase: WorkerPhase,
    configured: bool,
    server_url: Option<String>,
    paused: bool,
    platform: &'static str,
    nvidia: Option<bool>,
    /// 模型清单（含当前识别模式、缺哪些模型）；运行环境没装好时为 None。
    catalog: Option<shell_core::models::ModelStatus>,
}

#[tauri::command]
fn state(handle: AppHandle) -> View {
    let state = app(&handle);
    let env = state.worker.env();
    let mut settings = load_settings(&state.settings_file);
    let pending = settings.pending_code.as_deref().and_then(connection::parse);
    // 接入串已经写进配置（下载、写配置都完成了），不用再保存它。
    if let Some(connection) = &pending {
        if env.config_value("server_url").as_deref() == Some(connection.server_url.as_str())
            && env.config_value("worker_token").as_deref() == Some(connection.token.as_str())
        {
            settings.pending_code = None;
            save_settings(&state.settings_file, &settings);
        }
    }
    View {
        phase: state.worker.phase(),
        configured: env.configured() || pending.is_some(),
        server_url: pending
            .map(|connection| connection.server_url)
            .or_else(|| env.config_value("server_url")),
        paused: settings.paused,
        platform: if cfg!(target_os = "macos") { "macos" } else { "windows" },
        nvidia: worker_env::has_nvidia(),
        catalog: state.worker.models(),
    }
}

/// 下载清单里选的模型（会停下识别程序，下载、自检后重新启动）。
#[tauri::command]
fn models_download(handle: AppHandle, ids: Vec<String>) {
    let state = app(&handle);
    let mut settings = load_settings(&state.settings_file);
    settings.download_approved = true;
    settings.paused = false;
    save_settings(&state.settings_file, &settings);
    state.worker.download_models(None, ids);
}

#[tauri::command]
fn model_delete(handle: AppHandle, id: String) {
    let state = app(&handle);
    if load_settings(&state.settings_file).paused {
        let worker = Arc::clone(&state.worker);
        std::thread::spawn(move || {
            let _ = worker.env().delete_model(&id);
            worker.refresh_models();
        });
    } else {
        state.worker.delete_model(None, id);
    }
}

#[tauri::command]
fn models_refresh(handle: AppHandle) {
    app(&handle).worker.refresh_models_later();
}

/// 用接入串连接主程序（第一次设置或更换主程序）。`download` 为 true 时立即下载运行环境和模型，
/// 之后自检并开始接单；为 false 时先保存，停在“待下载”。
#[tauri::command]
fn connect(handle: AppHandle, code: String, download: bool) -> Result<(), String> {
    let state = app(&handle);
    let Some(connection) = connection::parse(&code) else {
        return Err(state
            .lang
            .pick(
                "这不是有效的接入串。请在主程序控制台“云主机 → 添加显卡电脑”里重新复制。",
                "This isn't a valid connection code. Copy it again from the console under Cloud hosts → Add GPU computer.",
            )
            .into());
    };
    state.logger.info(&format!("连接主程序 {}", connection.server_url));
    let previous = load_settings(&state.settings_file);
    let settings = AppSettings {
        paused: false,
        pending_code: Some(code.trim().to_string()),
        download_approved: previous.download_approved || download,
    };
    save_settings(&state.settings_file, &settings);
    let _ = state.pause_item.set_text(state.lang.pick("暂停接单", "Pause"));
    // 设置好以后默认开机自启，像系统服务一样常驻；用户可以在托盘里关掉。
    let _ = handle.autolaunch().enable();
    let _ = state.autostart_item.set_checked(handle.autolaunch().is_enabled().unwrap_or(false));
    let _ = connection;
    resume(&state.worker, &settings);
    Ok(())
}

/// “待下载”时点“开始下载”。
#[tauri::command]
fn download(handle: AppHandle) {
    let state = app(&handle);
    let mut settings = load_settings(&state.settings_file);
    settings.download_approved = true;
    settings.paused = false;
    save_settings(&state.settings_file, &settings);
    resume(&state.worker, &settings);
}

#[tauri::command]
fn pause(handle: AppHandle, paused: bool) {
    set_paused(&handle, paused);
}

#[tauri::command]
fn retry(handle: AppHandle) {
    download(handle);
}

#[tauri::command]
fn open_logs(handle: AppHandle) {
    open_logs_folder(&handle);
}

#[tauri::command]
fn install_tools() -> Result<(), String> {
    std::process::Command::new("xcode-select")
        .arg("--install")
        .spawn()
        .map(|_| ())
        .map_err(|error| error.to_string())
}

// ---------------------------------------------------------------- 启动

fn setup(tauri_app: &mut tauri::App) -> Result<(), Box<dyn std::error::Error>> {
    let handle = tauri_app.handle().clone();
    let home = tauri_app.path().local_data_dir()?.join("VideoTurbo");
    let resources = tauri_app.path().resource_dir()?;
    let lang = Lang::detect();
    let env = WorkerEnv {
        root: home.join("worker"),
        bundle: resources.join("worker-bundle"),
        uv: resources.join("uv").join(if cfg!(windows) { "uv.exe" } else { "uv" }),
        ffmpeg_dir: resources.join("ffmpeg"),
        language: lang.server_code().into(),
        config_dir: "config-worker".into(),
    };
    let logger = Arc::new(Logger::new(env.logs().join("worker-app.log")));
    logger.info(&format!("VideoTurbo Worker {} 启动", env!("CARGO_PKG_VERSION")));
    let settings_file = home.join("worker-app.json");
    let settings = load_settings(&settings_file);

    let status_item =
        MenuItem::with_id(tauri_app, "status", status_text(lang, &WorkerPhase::Off), false, None::<&str>)?;
    let pause_label = if settings.paused {
        lang.pick("继续接单", "Resume")
    } else {
        lang.pick("暂停接单", "Pause")
    };
    let pause_item = MenuItem::with_id(tauri_app, "pause", pause_label, true, None::<&str>)?;
    let autostart_enabled = tauri_app.autolaunch().is_enabled().unwrap_or(false);
    let autostart_label = lang.pick("开机时自动启动", "Start at login");
    let autostart_item = CheckMenuItem::with_id(
        tauri_app,
        "autostart",
        autostart_label,
        true,
        autostart_enabled,
        None::<&str>,
    )?;

    let notify = {
        let status_item = status_item.clone();
        Box::new(move |phase: &WorkerPhase| {
            let _ = status_item.set_text(status_text(lang, phase));
        })
    };
    let worker = WorkerManager::new(env, Arc::clone(&logger), notify);
    let update_item = MenuItem::with_id(tauri_app, "update", updater::check_label(lang), true, None::<&str>)?;
    let updates = {
        let (worker, log, log2) = (Arc::clone(&worker), Arc::clone(&logger), Arc::clone(&logger));
        UpdateMenu::new(
            update_item.clone(),
            lang,
            Box::new(move || {
                log.info("安装更新前停止识别程序");
                worker.stop();
            }),
            Box::new(move |message| log2.info(message)),
        )
    };
    updates.start_background_checks(handle.clone());
    let state = App {
        worker: Arc::clone(&worker),
        lang,
        logger,
        settings_file,
        pause_item,
        autostart_item,
        update_item,
        updates,
    };
    build_tray(&handle, &state, &status_item)?;
    app_manage(tauri_app, state);

    let configured = worker.env().configured() || settings.pending_code.is_some();
    // 还没设置时一定弹出窗口，即使是开机自启。
    let autostarted = std::env::args().any(|arg| arg == AUTOSTART_ARG);
    create_main(&handle, !autostarted || !configured)?;
    if !settings.paused {
        resume(&worker, &settings);
    }
    worker.watch_models();
    Ok(())
}

fn app_manage(tauri_app: &mut tauri::App, state: App) {
    tauri_app.manage(state);
}

fn main() {
    let tauri_app = tauri::Builder::default()
        .plugin(tauri_plugin_single_instance::init(|handle, _args, _cwd| show_main(handle)))
        .plugin(tauri_plugin_autostart::init(MacosLauncher::LaunchAgent, Some(vec![AUTOSTART_ARG])))
        .plugin(tauri_plugin_opener::init())
        .plugin(updater::plugin())
        .invoke_handler(tauri::generate_handler![
            state,
            connect,
            download,
            pause,
            retry,
            models_download,
            model_delete,
            models_refresh,
            open_logs,
            install_tools
        ])
        .on_window_event(|window, event| {
            if let WindowEvent::CloseRequested { api, .. } = event {
                api.prevent_close();
                let _ = window.hide();
            }
        })
        .setup(setup)
        .build(tauri::generate_context!())
        .expect("VideoTurbo Worker 启动失败");

    tauri_app.run(|handle, event| match event {
        #[cfg(target_os = "macos")]
        RunEvent::Reopen { .. } => show_main(handle),
        RunEvent::Exit => {
            if let Some(state) = handle.try_state::<App>() {
                state.worker.stop();
                state.logger.info("VideoTurbo Worker 退出");
            }
        }
        _ => {}
    });
}
