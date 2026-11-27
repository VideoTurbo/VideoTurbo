# VideoTurbo

[![Release](https://img.shields.io/github/v/release/VideoTurbo/VideoTurbo?include_prereleases&sort=semver)](https://github.com/VideoTurbo/VideoTurbo/releases/latest)
[![Docker](https://img.shields.io/badge/docker-ghcr.io%2Fvideoturbo%2Fvideoturbo-2496ED?logo=docker&logoColor=white)](https://github.com/VideoTurbo/VideoTurbo/pkgs/container/videoturbo)
[![CI](https://github.com/VideoTurbo/VideoTurbo/actions/workflows/ci.yml/badge.svg)](https://github.com/VideoTurbo/VideoTurbo/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.11%20%7C%203.12-3776AB?logo=python&logoColor=white)](pyproject.toml)
[![License](https://img.shields.io/badge/license-AGPL--3.0--or--later-blue)](LICENSE)

[简体中文](README.md) | English | **[Documentation](https://videoturbo.github.io/en/)**

A self-hosted tool that adds translated subtitles to your video library. VideoTurbo transcribes the dialogue, translates it
into the language you choose and saves an `.srt` next to each video, ready for Emby / Jellyfin after a refresh.

![Subtitles](docs/images/en/subtitles.png)

## Features

- **Automatic subtitles**: Japanese, English or Korean → Simplified Chinese, Traditional Chinese, English and more; videos that already have subtitles are skipped or only translated.
- **Main app + GPU PCs**: the main app runs in Docker on a NAS; speech recognition runs on Windows / Linux PCs with an NVIDIA GPU.
- **Several machines at once**: home PCs and on-demand cloud GPUs can all join; jobs are shared out automatically and cloud hosts shut down when they finish.
- **Optional**: AI video restoration, cloud storage libraries, Emby / Jellyfin refresh, webhook notifications and plugins.

## Quick start

Download `docker-compose.example.yml` from [Releases](https://github.com/VideoTurbo/VideoTurbo/releases/latest), rename it to
`docker-compose.yml`, replace `/path/to/your/media` with your video folder and start it:

```sh
docker compose up -d
```

Open `http://<main-app-ip>:8765` and set an admin password. For first-time setup, GPU PCs, the FAQ and updating, see the
**[documentation](https://videoturbo.github.io/en/guide/introduction/)**.

Linux NVIDIA subtitle workers use PyTorch 2.12.1 with CUDA 13.0 to match Jasna and require NVIDIA driver 580 or later. WhisperJAV uses Qwen3-ASR for a single pass and adds anime-whisper for Japanese two-pass recognition.

## Contributing

Issues and pull requests are welcome. See [CONTRIBUTING.md](CONTRIBUTING.md) for the development setup and conventions, and [SECURITY.md](SECURITY.md) to report vulnerabilities privately.

## Credits

- [WhisperJAV](https://github.com/meizhong986/WhisperJAV): the recognition pipeline of the default mode.
- [Qwen3-ASR](https://huggingface.co/Qwen/Qwen3-ASR-1.7B), [anime-whisper](https://huggingface.co/litagin/anime-whisper): speech recognition models.
- [Jasna](https://github.com/Kruk2/jasna): video restoration.
- FFmpeg, FastAPI, Vue, Naive UI and other open-source projects.

VideoTurbo doesn't ship code or model weights from these projects; they are downloaded from their official sources during installation and keep their own licenses.

## License

[AGPL-3.0-or-later](LICENSE). If you distribute a modified version, or offer one as a network service, you must publish its source under the same license.

**🚫 Do not use this project to make or spread illegal content or to infringe the rights of others.**
