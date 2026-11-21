//! VideoTurbo 桌面版外壳的共用部分：与 Tauri 无关，主程序应用和识别机应用都用它。
//!
//! - [`process`]：启动、监视和有序停止子进程（主程序、识别程序）。
//! - [`settings`]：外壳自己的设置文件（端口、局域网访问等），与主程序数据库里的设置分开。
//! - [`logfile`]：外壳日志和子进程输出写到日志文件夹。
//! - [`locale`]：界面语言，中文以外一律英文。
//! - [`connection`]：解析控制台给出的接入串（主程序地址 + 接入密钥）。
//! - [`worker_env`]：识别程序的运行环境（安装、升级、配置、启动参数）。
//! - [`worker_manager`]：开启、关闭、守护识别程序，两个应用共用。

pub mod connection;
pub mod locale;
pub mod logfile;
pub mod process;
pub mod settings;
pub mod worker_env;
pub mod worker_manager;
