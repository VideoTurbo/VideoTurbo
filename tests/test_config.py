import os
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from video_turbo.config import (
    AppConfig,
    SubtitleConfig,
    TranslationConfig,
    apply_local_preview,
    deployment_settings,
)
from video_turbo.i18n import render
from video_turbo.worker.config import load_worker_config


def load_config(path: Path) -> AppConfig:
    """按 YAML 描述构造 AppConfig，并按 api_key_env 从环境变量填入 Key（仅供测试书写方便）。"""
    config = AppConfig.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")) or {})
    translation = config.translation
    translation.api_key = os.getenv("API_KEY") or translation.api_key
    translation.fallback_api_key = os.getenv("FALLBACK_API_KEY") or translation.fallback_api_key
    for upstream in [*translation.upstreams, *translation.fallback_upstreams]:
        if upstream.api_key_env and os.getenv(upstream.api_key_env):
            upstream.api_key = os.getenv(upstream.api_key_env)
    apply_local_preview(config)
    return config


def test_empty_main_config_uses_safe_defaults(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("{}\n", encoding="utf-8")
    config = load_config(path)
    assert config.asr.mode == "whisperjav"
    assert config.asr.whisperjav_ensemble is True
    assert config.translation.ready is False
    assert config.translation.model == "deepseek-v4-flash-0731"
    assert config.translation.job_concurrency == 1
    assert config.translation.checkpoint_every_batches == 3
    assert config.translation.checkpoint_interval_seconds == 10
    assert config.translation.request_timeout_seconds == 90
    assert config.subtitles.overwrite_existing is False
    assert config.audio.codec == "opus"
    assert config.audio.opus_bitrate == "64k"
    assert config.scan.settle_minutes == 0


def test_unlabeled_policy_rejects_unknown_values() -> None:
    with pytest.raises(ValidationError):
        SubtitleConfig(unlabeled_embedded_subtitle_policy="wait")


def test_local_preview_uses_same_config_without_enabling_pipeline(
    tmp_path: Path, monkeypatch
) -> None:
    path = tmp_path / "config.yaml"
    original = """
server:
  host: 0.0.0.0
media_libraries:
  - {id: lib-nas, name: NAS, path: /media/nas}
scan:
  enabled: true
  manual_scan_enabled: true
wake_on_lan:
  enabled: true
  mac_address: 00:11:22:33:44:55
translation:
  model: model-1
  upstreams:
    - {name: primary, base_url: http://primary/v1, api_key_env: NODE_KEY}
  fallback_model: model-2
  fallback_upstreams:
    - {name: backup, base_url: http://backup/v1, api_key_env: BACKUP_KEY}
""".lstrip()
    path.write_text(original, encoding="utf-8")
    monkeypatch.setenv("NODE_KEY", "primary-token")
    monkeypatch.setenv("BACKUP_KEY", "backup-token")
    monkeypatch.setenv("VIDEO_TURBO_LOCAL_PREVIEW", "1")

    preview = load_config(path)
    assert preview.server.host == "127.0.0.1"
    # 媒体库照常显示，但不会被扫描。
    assert len(preview.media_libraries) == 1
    assert preview.scan.enabled is False
    assert preview.scan.manual_scan_enabled is False
    assert preview.translation.upstreams[0].api_key == "primary-token"
    assert preview.translation.fallback_upstreams[0].api_key == "backup-token"
    assert path.read_text(encoding="utf-8") == original

    monkeypatch.delenv("VIDEO_TURBO_LOCAL_PREVIEW")
    production = load_config(path)
    assert production.server.host == "0.0.0.0"
    assert len(production.media_libraries) == 1
    assert production.scan.enabled is True


def test_translation_uses_two_jobs_and_eight_requests() -> None:
    config = TranslationConfig(
        concurrency=8,
        job_concurrency=2,
    )

    assert config.concurrency == 8
    assert config.job_concurrency == 2


def test_worker_relative_paths_resolve_from_config(tmp_path: Path) -> None:
    path = tmp_path / "worker.yaml"
    path.write_text(
        "server_url: http://nas:8765\nworker_token: test-token\ncache_dir: ../cache\nasr:\n  download_root: ../models\n",
        encoding="utf-8",
    )
    config = load_worker_config(path)
    assert config.cache_dir == tmp_path.parent / "cache"
    assert config.asr_dir == tmp_path.parent / "cache" / "asr"
    assert config.restore_dir == tmp_path.parent / "cache" / "restore"
    assert config.gpu_lock_file == tmp_path.parent / "cache" / ".gpu.lock"
    assert config.asr.download_root == tmp_path.parent / "models"


def test_worker_instance_environment_overrides_name_and_shares_default_cache(
    tmp_path: Path, monkeypatch
) -> None:
    path = tmp_path / "worker.yaml"
    # 配置里的旧 name 被忽略：名称 = 硬件机器标识 + worker.sh 传入的实例后缀。
    path.write_text("server_url: http://nas:8765\nworker_token: test-token\nname: original\n", encoding="utf-8")
    monkeypatch.setenv("VIDEO_TURBO_MACHINE_ID", "gpu-3f2a9c1d")
    monkeypatch.setenv("VIDEO_TURBO_WORKER_SUFFIX", "02")

    config = load_worker_config(path)

    assert config.name == "gpu-3f2a9c1d-02"
    assert config.cache_dir == tmp_path.parent / "cache"


def test_translation_fallback_can_inherit_gateway_and_override_api_key(
    tmp_path: Path, monkeypatch
) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        """
translation:
  base_url: http://gateway/v1
  model: grok-4.5
  fallback_model: deepseek-v4-flash-0731
  fallback_thinking_mode: disabled
""".lstrip(),
        encoding="utf-8",
    )
    monkeypatch.setenv("API_KEY", "primary-token")
    monkeypatch.setenv("FALLBACK_API_KEY", "fallback-token")

    app_config = load_config(path)
    config = app_config.translation
    fallback = config.fallback_request_config()

    assert config.ready is True
    assert config.fallback_ready is True
    assert fallback is not None
    assert fallback.base_url == "http://gateway/v1"
    assert fallback.model == "deepseek-v4-flash-0731"
    assert fallback.api_key == "fallback-token"


def test_translation_upstreams_load_independent_api_keys_from_environment(
    tmp_path: Path, monkeypatch
) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        """
translation:
  model: grok-4.5
  upstreams:
    - name: grok-1
      base_url: http://grok-1/v1
      api_key_env: API_KEY_1
      concurrency: 2
    - name: grok-2
      base_url: http://grok-2/v1
      api_key_env: API_KEY_2
      concurrency: 2
    - name: grok-3
      base_url: http://grok-3/v1
      api_key_env: API_KEY_3
      concurrency: 2
  max_upstream_attempts: 2
  upstream_cooldown_seconds: 45
""".lstrip(),
        encoding="utf-8",
    )
    monkeypatch.setenv("API_KEY_1", "token-1")
    monkeypatch.setenv("API_KEY_2", "token-2")
    monkeypatch.setenv("API_KEY", "legacy-token")

    app_config = load_config(path)
    config = app_config.translation

    assert config.ready is True
    assert [upstream.api_key for upstream in config.upstreams] == ["token-1", "token-2", None]
    assert [upstream.concurrency for upstream in config.upstreams] == [2, 2, 2]
    assert config.ready_upstream_count == 2
    assert config.max_upstream_attempts == 2
    assert config.upstream_cooldown_seconds == 45
    assert any("缺少 API Key" in render(warning) and "grok-3" in warning for warning in (render(item["message"]) for item in app_config.warnings()))


def test_translation_upstream_can_select_chat_completions(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        """
translation:
  upstreams:
    - {name: responses, base_url: http://one/v1}
    - {name: chat, base_url: http://two/v1, api_mode: chat_completions}
""".lstrip(),
        encoding="utf-8",
    )

    config = load_config(path)

    assert [upstream.api_mode for upstream in config.translation.upstreams] == [
        "responses",
        "chat_completions",
    ]


def test_fallback_upstreams_load_independent_keys_and_inherit_fallback_model(
    tmp_path: Path, monkeypatch
) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        """
translation:
  fallback_model: deepseek-v4-flash
  fallback_upstreams:
    - name: deepseek-1
      base_url: http://deepseek-1/v1
      api_key_env: FALLBACK_API_KEY
    - name: deepseek-2
      base_url: http://deepseek-2/v1
      api_key_env: FALLBACK_API_KEY_2
  max_fallback_upstream_attempts: 2
""".lstrip(),
        encoding="utf-8",
    )
    monkeypatch.setenv("FALLBACK_API_KEY", "fallback-token-1")
    monkeypatch.setenv("FALLBACK_API_KEY_2", "fallback-token-2")

    config = load_config(path).translation
    pool_config = config.fallback_pool_config()

    assert config.fallback_ready is True
    assert config.ready_fallback_upstream_count == 2
    assert [item.api_key for item in config.fallback_upstreams] == [
        "fallback-token-1",
        "fallback-token-2",
    ]
    assert pool_config is not None
    assert pool_config.model == "deepseek-v4-flash"
    assert pool_config.upstreams == config.fallback_upstreams
    assert config.fallback_request_config() is None


def test_machine_id_comes_from_gpu_uuid_and_instances_share_machine(monkeypatch) -> None:
    from video_turbo import machine_id
    from video_turbo.restore_store import machine_key

    first = machine_id.from_gpu_uuid("GPU-1f2e3d4c-aaaa-bbbb-cccc-1234567890ab")
    assert first == machine_id.from_gpu_uuid(" gpu-1f2e3d4c-aaaa-bbbb-cccc-1234567890AB ")
    assert first.startswith("gpu-") and len(first) == 12
    assert first != machine_id.from_gpu_uuid("GPU-ffffffff-aaaa-bbbb-cccc-1234567890ab")
    asr, dc = machine_id.worker_name(first, "01"), machine_id.worker_name(first, "dc")
    assert machine_key(asr) == machine_key(dc) == machine_key(first) == first
    with pytest.raises(ValueError):
        machine_id.worker_name(first, "b-01")

    monkeypatch.delenv("VIDEO_TURBO_MACHINE_ID", raising=False)
    monkeypatch.setattr(machine_id.platform, "system", lambda: "Linux")
    monkeypatch.setattr(machine_id, "_run", lambda command: "GPU-1f2e3d4c-aaaa-bbbb-cccc-1234567890ab\n")
    assert machine_id.detect_machine_id("auto", 0) == first
    # CPU 模式不读 GPU，改用主机硬件 UUID。
    monkeypatch.setattr(machine_id, "_host_uuid", lambda: ("cpu", "host-uuid"))
    assert machine_id.detect_machine_id("cpu", 0).startswith("cpu-")


def test_job_cache_dir_is_named_after_title_and_survives_rename(tmp_path: Path) -> None:
    from video_turbo.naming import job_dir, job_dir_name

    job_id = "1a2b3c4d-5e6f-7a8b-9c0d-112233445566"
    assert job_dir_name("ABC-123", job_id) == "ABC-123-1a2b3c4d5e6f"
    assert job_dir_name("影片 01", job_id, ascii_only=True) == "___01-1a2b3c4d5e6f"
    assert job_dir_name("影片", job_id, ascii_only=True) == "1a2b3c4d5e6f"
    assert job_dir_name('a/b:c?', job_id) == "a_b_c_-1a2b3c4d5e6f"
    existing = tmp_path / "ABC-123-1a2b3c4d5e6f"
    existing.mkdir()
    # 影片改名后继续用已有目录，不丢失已缓存的文件。
    assert job_dir(tmp_path, "ABC-123-U", job_id) == existing
    assert job_dir(tmp_path, "XYZ", "ffffffff-0000-0000-0000-000000000000").name == "XYZ-ffffffff0000"


def test_local_preview_listens_on_localhost_only(monkeypatch: pytest.MonkeyPatch) -> None:
    # 服务实际监听的地址来自 deployment_settings，本地预览即使设置了 VIDEO_TURBO_HOST 也只听本机。
    monkeypatch.setenv("VIDEO_TURBO_HOST", "0.0.0.0")
    monkeypatch.setenv("VIDEO_TURBO_LOCAL_PREVIEW", "1")
    assert deployment_settings()["server"]["host"] == "127.0.0.1"
    monkeypatch.delenv("VIDEO_TURBO_LOCAL_PREVIEW")
    assert deployment_settings()["server"]["host"] == "0.0.0.0"
