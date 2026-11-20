"""字幕翻译：把识别出的对白按批发给翻译模型，处理重试、备用模型和中断续翻。

- run：一部影片的翻译流程（translate_cues）；
- api：各类翻译接口的请求与响应格式；
- pool：多个模型组成的模型池；
- prompts / checkpoint / metrics / errors：提示词、中间进度、统计和错误类型。
"""

from .api import probe_translation_responses
from .errors import (
    TranslationBatchError,
    TranslationError,
    TranslationFatalError,
    TranslationNodeResponseError,
    TranslationNotConfigured,
    TranslationTransportBatchError,
)
from .metrics import TranslationMetrics
from .pool import TranslationUpstreamPool
from .prompts import build_style_prompt, build_system_prompt
from .run import translate_cues

__all__ = [
    "TranslationBatchError",
    "TranslationError",
    "TranslationFatalError",
    "TranslationMetrics",
    "TranslationNodeResponseError",
    "TranslationNotConfigured",
    "TranslationTransportBatchError",
    "TranslationUpstreamPool",
    "build_style_prompt",
    "build_system_prompt",
    "probe_translation_responses",
    "translate_cues",
]
