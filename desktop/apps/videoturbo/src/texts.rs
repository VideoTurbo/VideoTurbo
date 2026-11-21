//! 托盘菜单和提示的文字。外壳页面（ui/）按浏览器语言自己选文字。

use shell_core::locale::Lang;

pub struct Texts {
    pub lang: Lang,
}

impl Texts {
    pub fn new(lang: Lang) -> Texts {
        Texts { lang }
    }

    fn pick(&self, zh: &'static str, en: &'static str) -> &'static str {
        self.lang.pick(zh, en)
    }

    pub fn status_starting(&self) -> &'static str {
        self.pick("主程序：正在启动…", "Main app: starting…")
    }
    pub fn status_running(&self) -> &'static str {
        self.pick("主程序：运行中", "Main app: running")
    }
    pub fn status_failed(&self) -> &'static str {
        self.pick("主程序：没能启动", "Main app: failed to start")
    }
    pub fn status_stopped(&self) -> &'static str {
        self.pick("主程序：已停止", "Main app: stopped")
    }
    pub fn open_console(&self) -> &'static str {
        self.pick("打开控制台", "Open console")
    }
    pub fn settings(&self) -> &'static str {
        self.pick("设置…", "Settings…")
    }
    pub fn settings_title(&self) -> &'static str {
        self.pick("VideoTurbo 设置", "VideoTurbo Settings")
    }
    pub fn autostart(&self) -> &'static str {
        self.pick("开机时自动启动", "Start at login")
    }
    pub fn open_data(&self) -> &'static str {
        self.pick("打开数据文件夹", "Open data folder")
    }
    pub fn open_logs(&self) -> &'static str {
        self.pick("打开日志文件夹", "Open logs folder")
    }
    pub fn restart(&self) -> &'static str {
        self.pick("重新启动主程序", "Restart main app")
    }
    pub fn local_worker(&self) -> &'static str {
        self.pick("本机识别…", "Local recognition…")
    }
    pub fn local_worker_title(&self) -> &'static str {
        self.pick("VideoTurbo 本机识别", "VideoTurbo Local Recognition")
    }
    pub fn worker_status(&self, phase: &crate::local_worker::WorkerPhase) -> String {
        use crate::local_worker::WorkerPhase as P;
        let text = match phase {
            P::Off => self.pick("本机识别：未开启", "Local recognition: off"),
            P::Waiting => self.pick("本机识别：正在连接…", "Local recognition: connecting…"),
            P::Installing { index, total, .. } => {
                return match self.lang {
                    Lang::Zh => format!("本机识别：正在安装（{index}/{total}）…"),
                    Lang::En => format!("Local recognition: installing ({index}/{total})…"),
                }
            }
            P::NeedTools => {
                self.pick("本机识别：需要安装命令行工具", "Local recognition: command line tools needed")
            }
            P::SelfTest { .. } => {
                self.pick("本机识别：正在下载模型并自检…", "Local recognition: downloading models…")
            }
            P::Running => self.pick("本机识别：运行中", "Local recognition: running"),
            P::Failed { .. } => self.pick("本机识别：出错了", "Local recognition: error"),
        };
        text.to_string()
    }
    pub fn quit(&self) -> &'static str {
        self.pick("退出 VideoTurbo", "Quit VideoTurbo")
    }

    pub fn port_in_use(&self, port: u16) -> String {
        match self.lang {
            Lang::Zh => format!("端口 {port} 已被其他程序占用。请关闭占用它的程序，或在设置里换一个端口。"),
            Lang::En => format!("Port {port} is already used by another program. Close that program or choose another port in Settings."),
        }
    }
    pub fn missing_server(&self) -> String {
        self.pick(
            "安装不完整：找不到主程序文件，请重新安装 VideoTurbo。",
            "The installation is incomplete: the main app files are missing. Reinstall VideoTurbo.",
        )
        .to_string()
    }
    pub fn spawn_failed(&self, error: &std::io::Error) -> String {
        match self.lang {
            Lang::Zh => format!("无法启动主程序：{error}"),
            Lang::En => format!("Couldn't start the main app: {error}"),
        }
    }
    pub fn exited(&self, code: Option<i32>) -> String {
        let code = code.map(|value| value.to_string()).unwrap_or_else(|| "-".into());
        match self.lang {
            Lang::Zh => format!("主程序意外退出（退出码 {code}）。"),
            Lang::En => format!("The main app quit unexpectedly (exit code {code})."),
        }
    }
    pub fn start_timeout(&self) -> String {
        self.pick("主程序启动超时。", "The main app took too long to start.").to_string()
    }
    pub fn invalid_settings(&self, invalid: &shell_core::settings::Invalid) -> String {
        use shell_core::settings::Invalid;
        match invalid {
            Invalid::PortRange => {
                self.pick("端口要在 1024 到 65535 之间。", "Ports must be between 1024 and 65535.")
            }
            Invalid::PortClash => self.pick(
                "控制台端口和接入端口不能相同。",
                "The console port and the connection port must differ.",
            ),
            Invalid::BackupDir => {
                self.pick("备份文件夹要写完整路径。", "The backup folder must be a full path.")
            }
        }
        .to_string()
    }
}
