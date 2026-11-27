# VideoTurbo

[![Release](https://img.shields.io/github/v/release/VideoTurbo/VideoTurbo?include_prereleases&sort=semver)](https://github.com/VideoTurbo/VideoTurbo/releases/latest)
[![Docker](https://img.shields.io/badge/docker-ghcr.io%2Fvideoturbo%2Fvideoturbo-2496ED?logo=docker&logoColor=white)](https://github.com/VideoTurbo/VideoTurbo/pkgs/container/videoturbo)
[![CI](https://github.com/VideoTurbo/VideoTurbo/actions/workflows/ci.yml/badge.svg)](https://github.com/VideoTurbo/VideoTurbo/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.11%20%7C%203.12-3776AB?logo=python&logoColor=white)](pyproject.toml)
[![License](https://img.shields.io/badge/license-AGPL--3.0--or--later-blue)](LICENSE)

简体中文 | [English](README.en.md) | **[官网文档](https://videoturbo.github.io/)**

给影片库自动生成翻译字幕的自托管工具：VideoTurbo 识别影片里的对白、翻译成你要的语言，在影片旁边写好 `.srt`，
Emby / Jellyfin 刷新后就能直接选用。

![字幕生成](docs/images/zh/subtitles.png)

## 功能

- **自动生成字幕**：日语、英语、韩语影片 → 简体中文、繁体中文、英语等，已有字幕的影片自动跳过或只翻译。
- **主程序 + 显卡电脑**：主程序用 Docker 装在 NAS 上，语音识别交给有 NVIDIA 显卡的 Windows / Linux 电脑。
- **多台机器一起干活**：家里的电脑和按需租用的云 GPU 都能接入，任务自动分配，云主机跑完自动关机。
- **可选功能**：AI 影片修复、网盘媒体库、Emby / Jellyfin 自动刷新、Webhook 推送，以及插件扩展。

## 快速开始

从 [Releases](https://github.com/VideoTurbo/VideoTurbo/releases/latest) 下载 `docker-compose.example.yml`，改名为
`docker-compose.yml`，把里面的 `/path/to/your/media` 改成影片所在的文件夹，然后启动：

```sh
docker compose up -d
```

浏览器打开 `http://主程序的IP:8765` 设置管理口令。之后的首次设置、显卡电脑安装、常见问题和更新方法见
**[官网文档](https://videoturbo.github.io/guide/introduction/)**。

Linux NVIDIA 字幕使用与 Jasna 对齐的 PyTorch 2.12.1 和 CUDA 13.0，需要 580 系列或更高版本的驱动。WhisperJAV 单遍使用 Qwen3-ASR，日语双遍增加 anime-whisper。

## 参与贡献

欢迎提交 Issue 和 PR，开发环境和提交规范见 [CONTRIBUTING.md](CONTRIBUTING.md)，安全漏洞请按 [SECURITY.md](SECURITY.md) 私密报告。

## 致谢

- [WhisperJAV](https://github.com/meizhong986/WhisperJAV)：字幕识别流程。
- [Qwen3-ASR](https://huggingface.co/Qwen/Qwen3-ASR-1.7B)、[anime-whisper](https://huggingface.co/litagin/anime-whisper)：语音识别模型。
- [Jasna](https://github.com/Kruk2/jasna)：影片修复。
- FFmpeg、FastAPI、Vue、Naive UI 等开源项目。

本项目不包含上述项目的代码或模型权重，安装时从各自官方来源下载，许可证以其官方为准。

## 许可证

[AGPL-3.0-or-later](LICENSE)。分发修改版，或把修改版作为网络服务提供给他人时，需要以相同许可证公开源代码。

**🚫禁止将本项目用于制作、传播违法内容或侵犯他人权益。**
