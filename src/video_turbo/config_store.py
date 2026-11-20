"""主控运行配置：全部由控制台写入数据库并热更新，没有配置文件。

- 由控制台管理：翻译节点、翻译开关与提示词、媒体库、扫描、字幕判断规则
  和日志级别。数据库中没有记录的项使用内置默认值。
- 其余参数（翻译批次与重试、字幕排版、Worker 租约、影片修复校验等）固定使用内置默认值，不可修改。
- 与容器部署绑定的项（监听地址、端口、原片备份目录）来自环境变量，见 config.deployment_settings。
"""

from __future__ import annotations

import copy
import logging
from typing import Any

from pydantic import ValidationError

from .config import AppConfig, apply_local_preview, deployment_settings
from .database import Database
from .i18n import msg
from .llm_config import (
    TRANSLATION_TUNING_FIELDS,
    LlmSettings,
    build_translation_config,
)

logger = logging.getLogger(__name__)

SETTINGS_KEY = "app_settings"
LLM_KEY = "llm_settings"

# 控制台管理的配置段 → 该段中受管理的字段（None 表示整段）。
MANAGED_SECTIONS: dict[str, tuple[str, ...] | None] = {
    "server": ("log_level",),
    "media_libraries": None,
    "scan": ("enabled", "schedule", "settle_minutes"),
    "subtitles": (
        "skip_existing_target",
        "accept_other_chinese_variant",
        "unlabeled_embedded_subtitle_policy",
    ),
    "translation": TRANSLATION_TUNING_FIELDS,
}

class ConfigError(ValueError):
    """控制台提交的配置未通过校验。"""


def describe_validation_error(exc: ValidationError) -> str:
    parts = []
    for error in exc.errors():
        location = ".".join(str(item) for item in error.get("loc", ()) if item != "__root__")
        message = str(error.get("msg", "")).removeprefix("Value error, ")
        parts.append(msg("config.store.fieldError", location=location, message=message) if location else message)
    return msg("common.listJoin").join(parts) or str(exc)


def _section_from_config(config: AppConfig, section: str) -> Any:
    fields = MANAGED_SECTIONS[section]
    value = getattr(config, section)
    if section == "media_libraries":
        return [item.model_dump(mode="json") for item in value]
    data = value.model_dump(mode="json")
    if fields is None:
        return data
    return {key: data[key] for key in fields}


class ConfigStore:
    def __init__(self, database: Database):
        self.database = database


    def settings(self) -> dict[str, Any]:
        value = self.database.runtime_json(SETTINGS_KEY)
        return value if isinstance(value, dict) else {}

    def llm(self) -> LlmSettings:
        value = self.database.runtime_json(LLM_KEY)
        try:
            return LlmSettings.model_validate(value or {})
        except ValidationError:
            logger.exception("数据库中的翻译模型配置无效，暂按空配置处理。")
            return LlmSettings()

    def build(
        self,
        *,
        settings: dict[str, Any] | None = None,
        llm: LlmSettings | None = None,
        preview: bool = True,
    ) -> AppConfig:
        """部署项（环境变量）叠加数据库中的控制台设置，生成运行配置。"""
        data: dict[str, Any] = deployment_settings()
        settings = self.settings() if settings is None else settings
        llm = self.llm() if llm is None else llm
        for section, fields in MANAGED_SECTIONS.items():
            if section not in settings:
                continue
            stored = copy.deepcopy(settings[section])
            if fields is None:
                data[section] = stored
            else:
                # 只取受管理的字段：数据库里其余的键一律忽略，使用内置默认值。
                data[section] = {
                    **data.get(section, {}),
                    **{k: v for k, v in stored.items() if k in fields},
                }
        data["translation"] = build_translation_config(llm, data.get("translation") or {})
        try:
            config = AppConfig.model_validate(data)
        except ValidationError as exc:
            raise ConfigError(describe_validation_error(exc)) from exc
        if preview:
            # 本地预览只在内存里关闭扫描等副作用，不影响保存到数据库的值。
            apply_local_preview(config)
        return config


    def update_section(self, section: str, value: Any) -> AppConfig:
        if section not in MANAGED_SECTIONS:
            raise ConfigError(msg("api.config.badSection", section=section))
        settings = copy.deepcopy(self.settings())
        fields = MANAGED_SECTIONS[section]
        if section == "media_libraries":
            if not isinstance(value, list):
                raise ConfigError(msg("config.store.librariesList"))
            settings[section] = value
        else:
            if not isinstance(value, dict):
                raise ConfigError(msg("config.store.sectionObject"))
            unknown = set(value) - set(fields or value)
            if unknown:
                raise ConfigError(msg("config.store.unknownFields", section=section, fields=sorted(unknown)))
            saved = settings.get(section) or {}
            settings[section] = {**saved, **value}
        canonical = self.build(settings=settings, preview=False)
        # 按校验后的规范值保存（例如扩展名去点、排序）。
        settings[section] = _section_from_config(canonical, section)
        self.database.set_runtime_json(SETTINGS_KEY, settings)
        return self.build(settings=settings)

    def save_llm(self, llm: LlmSettings) -> AppConfig:
        config = self.build(llm=llm)
        self.database.set_runtime_json(LLM_KEY, llm.model_dump(mode="json"))
        return config

    def public_settings(self) -> dict[str, Any]:
        """控制台表单使用的已保存值（不含本地预览的临时覆盖）。"""
        config = self.build(preview=False)
        return {section: _section_from_config(config, section) for section in MANAGED_SECTIONS}
