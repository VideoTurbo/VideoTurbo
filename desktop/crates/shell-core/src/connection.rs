//! 接入串：`VT1:` 加 `{"u": 主程序地址, "t": 接入密钥}` 的 base64url 编码（去掉末尾的 `=`）。
//! 格式与主程序 src/video_turbo/connection_string.py 生成的一致。

use base64::engine::general_purpose::URL_SAFE_NO_PAD;
use base64::Engine;
use serde::Deserialize;

const PREFIX: &str = "VT1:";

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Connection {
    pub server_url: String,
    pub token: String,
}

#[derive(Deserialize)]
struct Payload {
    u: String,
    t: String,
}

fn valid_url(url: &str) -> bool {
    let rest = url.strip_prefix("http://").or_else(|| url.strip_prefix("https://"));
    matches!(rest, Some(host) if !host.is_empty() && !host.contains('/') && !host.contains('@') && !host.contains(char::is_whitespace))
}

/// 解析接入串；复制时混进来的空白会被去掉。不是有效的接入串时返回 None。
pub fn parse(text: &str) -> Option<Connection> {
    let compact: String = text.chars().filter(|c| !c.is_whitespace()).collect();
    if compact.len() < PREFIX.len() || !compact[..PREFIX.len()].eq_ignore_ascii_case(PREFIX) {
        return None;
    }
    let body = compact[PREFIX.len()..].trim_end_matches('=');
    let raw = URL_SAFE_NO_PAD.decode(body).ok()?;
    let payload: Payload = serde_json::from_slice(&raw).ok()?;
    let server_url = payload.u.trim().trim_end_matches('/').to_string();
    let token = payload.t.trim().to_string();
    if token.is_empty() || !valid_url(&server_url) {
        return None;
    }
    Some(Connection { server_url, token })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_codes_from_the_console() {
        // 由 connection_string.encode("http://192.168.1.10:8766", "abc123") 生成。
        let code = "VT1:eyJ1IjoiaHR0cDovLzE5Mi4xNjguMS4xMDo4NzY2IiwidCI6ImFiYzEyMyJ9";
        let expected = Connection { server_url: "http://192.168.1.10:8766".into(), token: "abc123".into() };
        assert_eq!(parse(code), Some(expected.clone()));
        assert_eq!(parse(&format!("  vt1:{}\n{} ", &code[4..20], &code[20..])), Some(expected));
    }

    #[test]
    fn rejects_anything_else() {
        for text in ["", "http://nas:8766", "VT1:", "VT1:!!", "VT1:e30"] {
            assert_eq!(parse(text), None, "{text}");
        }
        let bad_url = URL_SAFE_NO_PAD.encode(br#"{"u":"ftp://nas","t":"x"}"#);
        assert_eq!(parse(&format!("VT1:{bad_url}")), None);
    }
}
