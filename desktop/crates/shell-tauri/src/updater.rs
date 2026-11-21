//! 自动更新：启动一分钟后和之后每 12 小时检查一次 GitHub Releases 上的新版本。
//!
//! 有新版时只在托盘菜单里显示“更新到 vX.Y.Z”，用户点了才下载安装：先下载并校验签名，再停下主程序和
//! 识别程序（`before_install`），然后安装并重启。不会在处理任务时自己重启。

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use shell_core::locale::Lang;
use tauri::menu::MenuItem;
use tauri::{AppHandle, Wry};
use tauri_plugin_updater::{Update, UpdaterExt};

const FIRST_CHECK: Duration = Duration::from_secs(60);
const CHECK_EVERY: Duration = Duration::from_secs(12 * 3600);

pub struct UpdateMenu {
    item: MenuItem<Wry>,
    lang: Lang,
    pending: Mutex<Option<Update>>,
    busy: AtomicBool,
    before_install: Box<dyn Fn() + Send + Sync>,
    log: Box<dyn Fn(&str) + Send + Sync>,
}

/// 托盘菜单项一开始的文字。
pub fn check_label(lang: Lang) -> &'static str {
    lang.pick("检查更新", "Check for updates")
}

impl UpdateMenu {
    pub fn new(
        item: MenuItem<Wry>,
        lang: Lang,
        before_install: Box<dyn Fn() + Send + Sync>,
        log: Box<dyn Fn(&str) + Send + Sync>,
    ) -> Arc<Self> {
        Arc::new(UpdateMenu {
            item,
            lang,
            pending: Mutex::new(None),
            busy: AtomicBool::new(false),
            before_install,
            log,
        })
    }

    fn text(&self, text: &str) {
        let _ = self.item.set_text(text);
    }

    fn available_text(&self, version: &str) -> String {
        match self.lang {
            Lang::Zh => format!("更新到 v{version}"),
            Lang::En => format!("Update to v{version}"),
        }
    }

    /// 后台定期检查（只更新菜单文字，不弹窗、不安装）。
    pub fn start_background_checks(self: &Arc<Self>, app: AppHandle) {
        let menu = Arc::clone(self);
        tauri::async_runtime::spawn(async move {
            tokio_sleep(FIRST_CHECK).await;
            loop {
                if menu.pending.lock().map(|pending| pending.is_none()).unwrap_or(true) {
                    menu.check(&app, false).await;
                }
                tokio_sleep(CHECK_EVERY).await;
            }
        });
    }

    async fn check(&self, app: &AppHandle, manual: bool) {
        let result = match app.updater() {
            Ok(updater) => updater.check().await.map_err(|error| error.to_string()),
            Err(error) => Err(error.to_string()),
        };
        match result {
            Ok(Some(update)) => {
                (self.log)(&format!("发现新版本 {}", update.version));
                self.text(&self.available_text(&update.version));
                *self.pending.lock().unwrap_or_else(|error| error.into_inner()) = Some(update);
            }
            Ok(None) => {
                if manual {
                    self.text(self.lang.pick("已是最新版本", "Up to date"));
                }
            }
            Err(error) => {
                (self.log)(&format!("检查更新失败：{error}"));
                if manual {
                    self.text(
                        self.lang.pick("检查更新失败，点此重试", "Update check failed; click to retry"),
                    );
                }
            }
        }
    }

    /// 托盘菜单项被点击：有新版就安装，没有就立即检查一次。
    pub fn on_click(self: &Arc<Self>, app: AppHandle) {
        if self.busy.swap(true, Ordering::SeqCst) {
            return;
        }
        let menu = Arc::clone(self);
        tauri::async_runtime::spawn(async move {
            let pending = menu.pending.lock().unwrap_or_else(|error| error.into_inner()).take();
            match pending {
                Some(update) => menu.install(&app, update).await,
                None => {
                    menu.text(menu.lang.pick("正在检查更新…", "Checking for updates…"));
                    menu.check(&app, true).await;
                }
            }
            menu.busy.store(false, Ordering::SeqCst);
        });
    }

    async fn install(&self, app: &AppHandle, update: Update) {
        self.text(self.lang.pick("正在下载更新…", "Downloading the update…"));
        let version = update.version.clone();
        let bytes = match update.download(|_, _| {}, || {}).await {
            Ok(bytes) => bytes,
            Err(error) => {
                (self.log)(&format!("下载更新失败：{error}"));
                self.text(self.lang.pick("下载更新失败，点此重试", "Download failed; click to retry"));
                *self.pending.lock().unwrap_or_else(|error| error.into_inner()) = Some(update);
                return;
            }
        };
        (self.log)(&format!("安装新版本 {version}"));
        self.text(self.lang.pick("正在安装更新…", "Installing the update…"));
        // 安装程序会替换程序文件，先停下主程序和识别程序。
        (self.before_install)();
        if let Err(error) = update.install(bytes) {
            (self.log)(&format!("安装更新失败：{error}"));
            self.text(
                self.lang
                    .pick("安装更新失败，请从官网下载安装", "Update failed; download it from the website"),
            );
            return;
        }
        app.restart();
    }
}

async fn tokio_sleep(duration: Duration) {
    let (sender, receiver) = std::sync::mpsc::channel::<()>();
    std::thread::spawn(move || {
        std::thread::sleep(duration);
        let _ = sender.send(());
    });
    let _ = tauri::async_runtime::spawn_blocking(move || receiver.recv()).await;
}

/// 更新插件；两个应用都要在 Builder 上注册。
pub fn plugin<R: tauri::Runtime>() -> tauri::plugin::TauriPlugin<R, tauri_plugin_updater::Config> {
    tauri_plugin_updater::Builder::new().build()
}
