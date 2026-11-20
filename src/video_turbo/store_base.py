"""数据库各部分（*_store.py 中的 mixin）共用的声明。

``Database``（database.py）负责连接管理，并组合各个 mixin：

- settings_store：runtime_settings 表中的设置与运行状态；
- media_store：媒体库中的影片、海报缓存与改名跟踪；
- job_store：字幕任务的创建、状态流转、领取与租约；
- worker_store：已接入的机器；
- restore_store、cloud_store、plugin_store：影片修复、云主机与通知、插件。

mixin 只通过 ``self.connect()`` 访问数据库，彼此的方法都挂在同一个 ``Database`` 对象上。
"""

from __future__ import annotations

import sqlite3
from contextlib import AbstractContextManager
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeVar

from pydantic import BaseModel

if TYPE_CHECKING:
    from .media_store import MediaFiles

M = TypeVar("M", bound=BaseModel)


class StoreBase:
    """由 Database 提供的属性与方法；这里只声明以便类型检查。"""

    path: Path
    files: MediaFiles

    def connect(self) -> AbstractContextManager[sqlite3.Connection]:  # pragma: no cover - 由 Database 实现
        raise NotImplementedError

    # 由 settings_store 实现。
    def runtime_json(self, key: str) -> Any | None:  # pragma: no cover
        raise NotImplementedError

    def set_runtime_json(self, key: str, value: Any) -> None:  # pragma: no cover
        raise NotImplementedError

    def delete_setting(self, key: str) -> None:  # pragma: no cover
        raise NotImplementedError

    def model_setting(self, key: str, model: type[M], default: M | None = None) -> M:  # pragma: no cover
        raise NotImplementedError

    def set_model_setting(self, key: str, value: BaseModel) -> None:  # pragma: no cover
        raise NotImplementedError


