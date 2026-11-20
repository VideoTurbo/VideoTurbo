"""翻译中间进度：定期写入文件，中断后从已翻译的部分继续。"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from ..config import TranslationConfig
from ..languages import LanguagePair
from ..models import Cue
from .prompts import DEFAULT_PAIR

logger = logging.getLogger(__name__)


def _checkpoint_payload(
    cues: Sequence[Cue],
    config: TranslationConfig,
    model_by_cue_id: dict[int, str] | None = None,
    upstream_by_cue_id: dict[int, str] | None = None,
    upstream_failure_counts: dict[str, int] | None = None,
    pair: LanguagePair = DEFAULT_PAIR,
) -> dict[str, Any]:
    return {
        "model": config.model,
        "base_url": config.base_url,
        "source_language": pair.source,
        "target_language": pair.target,
        "cues": [cue.model_dump() for cue in cues],
        "translation_models": {
            str(cue_id): model for cue_id, model in (model_by_cue_id or {}).items()
        },
        "translation_upstreams": {
            str(cue_id): upstream
            for cue_id, upstream in (upstream_by_cue_id or {}).items()
        },
        "upstream_failure_counts": dict(sorted((upstream_failure_counts or {}).items())),
    }


def write_checkpoint(
    path: Path,
    cues: Sequence[Cue],
    config: TranslationConfig,
    model_by_cue_id: dict[int, str] | None = None,
    upstream_by_cue_id: dict[int, str] | None = None,
    upstream_failure_counts: dict[str, int] | None = None,
    pair: LanguagePair = DEFAULT_PAIR,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(
            _checkpoint_payload(
                cues,
                config,
                model_by_cue_id,
                upstream_by_cue_id,
                upstream_failure_counts,
                pair,
            ),
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    temporary.replace(path)


def load_checkpoint(
    path: Path,
    cues: Sequence[Cue],
    config: TranslationConfig,
    pair: LanguagePair = DEFAULT_PAIR,
) -> tuple[dict[int, str], dict[int, str], dict[int, str], dict[str, int]]:
    if not path.is_file():
        return {}, {}, {}, {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if (
            payload.get("model") != config.model
            or payload.get("base_url") != config.base_url
            or payload.get("source_language") != pair.source
            or payload.get("target_language") != pair.target
        ):
            return {}, {}, {}, {}
        source_by_id = {cue.cue_id: cue.source for cue in cues}
        translated: dict[int, str] = {}
        model_by_cue_id: dict[int, str] = {}
        upstream_by_cue_id: dict[int, str] = {}
        stored_models = payload.get("translation_models") or {}
        stored_upstreams = payload.get("translation_upstreams") or {}
        stored_failures = payload.get("upstream_failure_counts") or {}
        for item in payload.get("cues", []):
            cue_id = int(item["cue_id"])
            text = str(item.get("target") or "").strip()
            if text and source_by_id.get(cue_id) == item.get("source"):
                translated[cue_id] = text
                model = str(stored_models.get(str(cue_id)) or "").strip()
                if model:
                    model_by_cue_id[cue_id] = model
                upstream = str(stored_upstreams.get(str(cue_id)) or "").strip()
                if upstream:
                    upstream_by_cue_id[cue_id] = upstream
        upstream_failure_counts = {
            str(name): int(count)
            for name, count in stored_failures.items()
            if int(count) > 0
        }
        return translated, model_by_cue_id, upstream_by_cue_id, upstream_failure_counts
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        logger.warning("翻译中间进度无效，将忽略并重新翻译：%s", path)
        return {}, {}, {}, {}
