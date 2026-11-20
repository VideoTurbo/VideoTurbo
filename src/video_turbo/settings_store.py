"""runtime_settings 表中的设置与运行状态：语音识别、界面、传输限速、自动处理开关、接入密钥、翻译模型状态等。

表中每一行是一个键和一段文本（多数是 JSON）。已用的键：

- 本模块：asr、ui、transfer、pipeline_paused、worker_token、library_scans、translation_upstream_states；
- restore_store：restore；cloud_store：cloud、cloud_credentials、cloud_profiles、cloud_ssh_host_keys、jasna_license；
- plugin_store：plugins、plugin_data、plugin_repositories、media_servers、notifiers；
- config_store：app_settings、llm_settings；orchestrator/auth：admin_auth。

修改已保存的格式时按 migrations.py 的说明追加迁移。
"""

from __future__ import annotations

import json
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from .models import AsrSettings, TransferSettings, UiSettings, utc_now_iso
from .security import new_worker_token
from .store_base import StoreBase

WORKER_TOKEN_KEY = "worker_token"
# 各媒体库最近一次扫描完成的时间：{媒体库 id: ISO 时间}。
LIBRARY_SCANS_KEY = "library_scans"
ASR_KEY = "asr"
UI_KEY = "ui"
TRANSFER_KEY = "transfer"
PIPELINE_PAUSED_KEY = "pipeline_paused"
TRANSLATION_UPSTREAM_STATES_KEY = "translation_upstream_states"

_UPSERT = (
    "INSERT INTO runtime_settings (key, value, updated_at) VALUES (?, ?, ?) "
    "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at"
)

M = TypeVar("M", bound=BaseModel)


class SettingsStoreMixin(StoreBase):
    # ---- 通用读写：所有 runtime_settings 的访问都经过这里 ----

    def _setting_text(self, key: str) -> str | None:
        with self.connect() as conn:
            row = conn.execute("SELECT value FROM runtime_settings WHERE key = ?", (key,)).fetchone()
        return None if row is None else str(row["value"])

    def _set_setting_text(self, key: str, value: str) -> None:
        with self.connect() as conn:
            conn.execute(_UPSERT, (key, value, utc_now_iso()))

    def delete_setting(self, key: str) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM runtime_settings WHERE key = ?", (key,))

    def runtime_json(self, key: str) -> Any | None:
        """读取 JSON 值；不存在或无法解析时返回 None。"""
        text = self._setting_text(key)
        if text is None:
            return None
        try:
            return json.loads(text)
        except ValueError:
            return None

    def set_runtime_json(self, key: str, value: Any) -> None:
        self._set_setting_text(key, json.dumps(value, ensure_ascii=False))

    def model_setting(self, key: str, model: type[M], default: M | None = None) -> M:
        """按 pydantic 模型读取一组设置。

        数据库里缺少的字段用 ``default``（未给出时为模型默认值）补齐，已保存的选择（包括 false）保持不变；
        保存的内容无法解析或不再符合模型时返回 ``default``，不让一条坏数据挡住主程序启动。
        """
        fallback = default if default is not None else model()
        stored = self.runtime_json(key)
        if not isinstance(stored, dict):
            return fallback
        try:
            return model.model_validate({**fallback.model_dump(), **stored})
        except ValidationError:
            return fallback

    def set_model_setting(self, key: str, value: BaseModel) -> None:
        self._set_setting_text(key, value.model_dump_json())

    # ---- 各项设置 ----

    def asr_settings(self, default: AsrSettings | None = None) -> AsrSettings:
        return self.model_setting(ASR_KEY, AsrSettings, default)

    def set_asr_settings(self, settings: AsrSettings) -> None:
        self.set_model_setting(ASR_KEY, settings)

    def ui_settings(self) -> UiSettings:
        return self.model_setting(UI_KEY, UiSettings)

    def set_ui_settings(self, settings: UiSettings) -> None:
        self.set_model_setting(UI_KEY, settings)

    def transfer_settings(self) -> TransferSettings:
        return self.model_setting(TRANSFER_KEY, TransferSettings)

    def set_transfer_settings(self, settings: TransferSettings) -> None:
        self.set_model_setting(TRANSFER_KEY, settings)

    def is_pipeline_paused(self) -> bool:
        return self._setting_text(PIPELINE_PAUSED_KEY) == "1"

    def set_pipeline_paused(self, paused: bool) -> None:
        self._set_setting_text(PIPELINE_PAUSED_KEY, "1" if paused else "0")

    def _upstream_states(self, setting_key: str) -> dict[str, dict[str, Any]]:
        value = self.runtime_json(setting_key)
        if not isinstance(value, dict):
            return {}
        return {
            str(name): state
            for name, state in value.items()
            if isinstance(state, dict)
        }

    def _set_upstream_states(
        self, setting_key: str, states: dict[str, dict[str, Any]]
    ) -> None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT value FROM runtime_settings WHERE key = ?",
                (setting_key,),
            ).fetchone()
            merged: dict[str, dict[str, Any]] = {}
            if row is not None:
                try:
                    current = json.loads(str(row["value"]))
                    if isinstance(current, dict):
                        merged = {
                            str(name): state
                            for name, state in current.items()
                            if isinstance(state, dict)
                        }
                except (TypeError, ValueError, json.JSONDecodeError):
                    pass
            merged.update(states)
            conn.execute(_UPSERT, (setting_key, json.dumps(merged, ensure_ascii=False), utc_now_iso()))

    def worker_token(self) -> str:
        """Worker 共享密钥：首次读取时自动生成并保存，之后可在控制台复制或重置。"""
        token = self.runtime_json(WORKER_TOKEN_KEY)
        return token if isinstance(token, str) and token else self.reset_worker_token()

    def reset_worker_token(self) -> str:
        token = new_worker_token()
        self.set_runtime_json(WORKER_TOKEN_KEY, token)
        return token

    def translation_upstream_states(self) -> dict[str, dict[str, Any]]:
        return self._upstream_states(TRANSLATION_UPSTREAM_STATES_KEY)

    def set_translation_upstream_states(self, states: dict[str, dict[str, Any]]) -> None:
        self._set_upstream_states(TRANSLATION_UPSTREAM_STATES_KEY, states)
