# Security Policy

[中文说明见下方](#安全策略)

## Supported versions

Only the latest release receives security fixes. Please check that the problem still exists in the latest version
before reporting.

## Reporting a vulnerability

**Do not open a public issue, discussion or pull request for security problems.**

Report privately through GitHub instead: open the repository's **Security** tab and click
[**Report a vulnerability**](https://github.com/VideoTurbo/VideoTurbo/security/advisories/new).

Please include:

- the VideoTurbo version, and the version on the GPU PC if it differs;
- which part is affected (main app, dashboard, GPU PC service, cloud host, plugin system);
- steps to reproduce, or a proof of concept;
- the impact you expect (for example: access without the access key, reading stored API keys, running commands on a
  GPU PC or cloud host).

Do not attach diagnostics bundles or logs that contain your own keys or video file names unless they are needed.

You will get a reply in the private report. Once a fix is released, the advisory is published and you are credited
unless you ask not to be.

## Scope

Third-party plugins are maintained by their authors. A vulnerability that exists only in a third-party plugin should be
reported to that plugin's author. Problems in the plugin system itself (`*/base.py`, plugin loading and permissions)
are in scope here.

---

# 安全策略

## 支持的版本

只有最新发布的版本会修复安全问题。报告前请先确认问题在最新版本中仍然存在。

## 报告漏洞

**不要通过公开的 Issue、讨论或 PR 报告安全问题。**

请通过 GitHub 私密报告：打开仓库的 **Security** 页，点击
[**Report a vulnerability**](https://github.com/VideoTurbo/VideoTurbo/security/advisories/new)。

请写明：VideoTurbo 版本（显卡电脑版本不同时一并写出）、受影响的部分、复现步骤或概念验证，以及可能造成的影响。

除非确有必要，不要附上含有你自己密钥或影片文件名的诊断包或日志。

我们会在私密报告中回复。修复版本发布后会公开安全公告，并致谢报告者（如不希望署名请说明）。

## 范围

第三方插件由其作者维护，只存在于第三方插件中的漏洞请报告给插件作者。插件系统本身（`*/base.py`、插件加载与权限）的问题属于本项目范围。
