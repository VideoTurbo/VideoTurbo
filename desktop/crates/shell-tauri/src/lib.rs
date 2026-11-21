//! VideoTurbo 桌面版两个应用共用、依赖 Tauri 的部分。
//!
//! - [`updater`]：检查和安装新版本（GitHub Releases 上的 latest.json，更新包用发布时的私钥签名）。

pub mod updater;
