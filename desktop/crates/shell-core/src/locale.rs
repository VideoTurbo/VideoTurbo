//! 界面语言：`VIDEO_TURBO_LANGUAGE` 优先，否则跟随系统语言；中文以外一律英文，与主程序一致。

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Lang {
    Zh,
    En,
}

impl Lang {
    pub fn detect() -> Lang {
        let setting = std::env::var("VIDEO_TURBO_LANGUAGE")
            .ok()
            .filter(|value| !value.trim().is_empty())
            .or_else(sys_locale::get_locale)
            .unwrap_or_default();
        Lang::from_tag(&setting)
    }

    pub fn from_tag(tag: &str) -> Lang {
        if tag.trim().to_ascii_lowercase().starts_with("zh") {
            Lang::Zh
        } else {
            Lang::En
        }
    }

    /// 传给主程序的语言代码（推送通知等按它输出）。
    pub fn server_code(self) -> &'static str {
        match self {
            Lang::Zh => "zh-Hans",
            Lang::En => "en",
        }
    }

    pub fn pick(self, zh: &'static str, en: &'static str) -> &'static str {
        match self {
            Lang::Zh => zh,
            Lang::En => en,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::Lang;

    #[test]
    fn chinese_variants_are_chinese_and_everything_else_is_english() {
        assert_eq!(Lang::from_tag("zh-CN"), Lang::Zh);
        assert_eq!(Lang::from_tag("zh-Hant-TW"), Lang::Zh);
        assert_eq!(Lang::from_tag("en-US"), Lang::En);
        assert_eq!(Lang::from_tag("ja-JP"), Lang::En);
        assert_eq!(Lang::from_tag(""), Lang::En);
    }
}
