//! 外壳自己的设置（desktop.json）：只放启动子进程前要定下来的项，其余设置都在控制台里改。

use std::fs;
use std::io;
use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(default)]
pub struct ServerSettings {
    /// 控制台端口。
    pub console_port: u16,
    /// 机器接入端口：其他显卡电脑连这个端口。
    pub worker_port: u16,
    /// 控制台允许局域网里的其他设备访问；关闭时只能在本机打开。
    pub lan_console: bool,
    /// 影片修复的原片备份目录；留空时用数据文件夹下的 backups。
    pub backup_dir: Option<PathBuf>,
    /// 安全模式：启动时不加载控制台里安装的插件。
    pub safe_mode: bool,
}

impl Default for ServerSettings {
    fn default() -> Self {
        ServerSettings {
            console_port: 8765,
            worker_port: 8766,
            lan_console: false,
            backup_dir: None,
            safe_mode: false,
        }
    }
}

#[derive(Debug, PartialEq, Eq)]
pub enum Invalid {
    /// 端口不在 1024–65535。
    PortRange,
    /// 两个端口相同。
    PortClash,
    /// 备份目录不是绝对路径。
    BackupDir,
}

impl ServerSettings {
    pub fn validate(&self) -> Result<(), Invalid> {
        if self.console_port < 1024 || self.worker_port < 1024 {
            return Err(Invalid::PortRange);
        }
        if self.console_port == self.worker_port {
            return Err(Invalid::PortClash);
        }
        if let Some(dir) = &self.backup_dir {
            if !dir.is_absolute() {
                return Err(Invalid::BackupDir);
            }
        }
        Ok(())
    }

    /// 读取设置；文件不存在、损坏或不合法时用默认值。
    pub fn load(path: &Path) -> ServerSettings {
        fs::read(path)
            .ok()
            .and_then(|data| serde_json::from_slice::<ServerSettings>(&data).ok())
            .filter(|settings| settings.validate().is_ok())
            .unwrap_or_default()
    }

    /// 先写临时文件再替换，避免写到一半断电留下损坏的设置。
    pub fn save(&self, path: &Path) -> io::Result<()> {
        if let Some(parent) = path.parent() {
            fs::create_dir_all(parent)?;
        }
        let data = serde_json::to_vec_pretty(self).map_err(io::Error::other)?;
        let temporary = path.with_extension("json.tmp");
        fs::write(&temporary, data)?;
        fs::rename(temporary, path)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn defaults_and_round_trip() {
        let dir = std::env::temp_dir().join(format!("vt-settings-{}", std::process::id()));
        let path = dir.join("desktop.json");
        assert_eq!(ServerSettings::load(&path), ServerSettings::default());
        let settings = ServerSettings { console_port: 9000, lan_console: true, ..Default::default() };
        settings.save(&path).unwrap();
        assert_eq!(ServerSettings::load(&path), settings);
        fs::write(&path, b"{\"console_port\": 8766}").unwrap();
        // 与接入端口冲突的设置不采用。
        assert_eq!(ServerSettings::load(&path), ServerSettings::default());
        let _ = fs::remove_dir_all(dir);
    }

    #[test]
    fn validation() {
        let base = ServerSettings::default();
        assert_eq!(ServerSettings { console_port: 80, ..base.clone() }.validate(), Err(Invalid::PortRange));
        assert_eq!(ServerSettings { worker_port: 8765, ..base.clone() }.validate(), Err(Invalid::PortClash));
        assert_eq!(
            ServerSettings { backup_dir: Some("relative".into()), ..base.clone() }.validate(),
            Err(Invalid::BackupDir)
        );
        assert!(base.validate().is_ok());
    }
}
