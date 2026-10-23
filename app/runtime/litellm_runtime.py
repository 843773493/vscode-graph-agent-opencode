"""统一 SDK 懒加载；公共模型目录本地优先，远程刷新不占用业务期限。"""

from __future__ import annotations

import asyncio
import importlib
import json
import logging
import os
import sys
import tempfile
import time
from hashlib import sha256
from pathlib import Path
from threading import Event, Lock, Thread
from types import ModuleType
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    import litellm
    import litellm.llms.custom_httpx.llm_http_handler as litellm_http_handler
    from langchain_litellm import ChatLiteLLM
    from litellm.llms.custom_httpx.http_handler import AsyncHTTPHandler, HTTPHandler

__all__ = ["AsyncHTTPHandler", "ChatLiteLLM", "HTTPHandler", "litellm", "litellm_http_handler"]

logger = logging.getLogger(__name__)
_LOCAL_ONLY = "LITELLM_LOCAL_MODEL_COST_MAP"
_import_lock = Lock()
_runtime_lock = Lock()
_runtime: ModelMetadataRuntime | None = None


def _import_litellm() -> ModuleType:
    with _import_lock:
        if "litellm" in sys.modules:
            return importlib.import_module("litellm")
        previous = os.environ.get(_LOCAL_ONLY)
        # TODO: 上游提供无网络导入参数后移除此临时开关；不修改 SDK 的下载函数。
        os.environ[_LOCAL_ONLY] = "true"
        try:
            return importlib.import_module("litellm")
        finally:
            if previous is None:
                os.environ.pop(_LOCAL_ONLY, None)
            else:
                os.environ[_LOCAL_ONLY] = previous


def _local_only_mode() -> bool:
    # 不能把另一个线程在首次导入期间设置的临时值当成用户配置。
    with _import_lock:
        return os.environ.get(_LOCAL_ONLY, "").lower() == "true"


def _adopt_catalog(sdk: ModuleType, catalog: dict[str, object]) -> None:
    from litellm.utils import (
        _invalidate_model_cost_lowercase_map,
        reapply_runtime_model_cost_registrations,
    )

    # 与上游热加载合同一致；显式 register_model 必须覆盖公共目录。
    sdk.model_cost = catalog
    _invalidate_model_cost_lowercase_map()
    sdk.add_known_models(model_cost_map=catalog)
    reapply_runtime_model_cost_registrations()


class ModelMetadataRuntime:
    """每个进程持有一个刷新任务；后台只发布缓存，后续请求才采用结果。"""

    def __init__(
        self, *, cache_root: Path, startup_wait_seconds: float = 1,
        refresh_timeout_seconds: float = 30, freshness_seconds: float = 86400,
        retry_interval_seconds: float = 300,
    ) -> None:
        if startup_wait_seconds < 0 or min(
            refresh_timeout_seconds, freshness_seconds, retry_interval_seconds,
        ) <= 0:
            raise ValueError("模型目录等待预算必须非负，其余期限必须为正数")
        self._cache_root = cache_root
        self._startup_wait = startup_wait_seconds
        self._refresh_timeout = refresh_timeout_seconds
        self._freshness = freshness_seconds
        self._retry_interval = retry_interval_seconds
        self._lock = Lock()
        self._finished = Event()
        self._sdk: ModuleType | None = None
        self._worker: Thread | None = None
        self._pending: dict[str, object] | None = None
        self._next_refresh = 0.0
        self._source_url = ""
        self._cache_path: Path | None = None

    def load(self) -> ModuleType:
        local_only = _local_only_mode()
        first_load = False
        with self._lock:
            if self._sdk is None:
                self._sdk = _import_litellm()
                first_load = True
            sdk = self._sdk
            if local_only:
                return sdk
            if not self._source_url:
                self._source_url = sdk.model_cost_map_url
                self._cache_path = self._cache_root / (sha256(self._source_url.encode()).hexdigest() + ".json")
                self._load_cache()
            self._adopt_pending()
            if time.monotonic() >= self._next_refresh and (
                self._worker is None or not self._worker.is_alive()
            ):
                self._finished.clear()
                self._next_refresh = time.monotonic() + self._retry_interval
                self._worker = Thread(target=self._refresh, name="model-metadata-refresh", daemon=True)
                self._worker.start()
        if first_load and self._worker is not None and not self._finished.wait(self._startup_wait):
            logger.info("模型目录前台等待已结束，继续使用旧目录；后台刷新仍在进行")
        with self._lock:
            self._adopt_pending()
        return sdk

    def _adopt_pending(self) -> None:
        if self._pending is not None:
            assert self._sdk is not None
            _adopt_catalog(self._sdk, self._pending)
            self._pending = None

    def _load_cache(self) -> None:
        from litellm.litellm_core_utils.get_model_cost_map import GetModelCostMap

        assert self._cache_path is not None and self._sdk is not None
        if not self._cache_path.exists():
            return
        try:
            cached = json.loads(self._cache_path.read_text(encoding="utf-8"))
            if not isinstance(cached, dict) or cached.get("source_url") != self._source_url:
                raise ValueError("模型目录缓存来源不匹配")
            catalog = cached["catalog"]
            fetched_at = cached["fetched_at"]
            if not isinstance(fetched_at, (int, float)) or not isinstance(catalog, dict):
                raise TypeError("模型目录缓存结构无效")
            if not GetModelCostMap.validate_model_cost_map(
                fetched_map=catalog, backup_model_count=GetModelCostMap._get_backup_model_count(),
            ):
                raise ValueError("模型目录缓存未通过官方校验")
        except (OSError, ValueError, TypeError, KeyError) as error:
            logger.warning("模型目录缓存不可用，使用已有目录: path=%s error=%s", self._cache_path, error)
            return
        _adopt_catalog(self._sdk, catalog)
        self._next_refresh = time.monotonic() + max(0, self._freshness - (time.time() - fetched_at))
        logger.info("已加载模型目录缓存: source=%s fetched_at=%s", self._source_url, fetched_at)

    async def _download(self) -> dict[str, object] | None:
        from litellm.litellm_core_utils.get_model_cost_map import (
            ModelCostMapReloadUnavailable,
            refetch_model_cost_map,
        )

        async with asyncio.timeout(self._refresh_timeout), httpx.AsyncClient() as client:
            result = await refetch_model_cost_map(url=self._source_url, client=client)
        if isinstance(result, ModelCostMapReloadUnavailable):
            logger.warning("模型目录后台刷新失败，保留旧目录: %s", result.reason)
            return None
        return result.model_cost_map

    def _refresh(self) -> None:
        try:
            catalog = asyncio.run(self._download())
            if catalog is None:
                return
            self._save_cache(catalog)
            with self._lock:
                self._pending = catalog
                self._next_refresh = time.monotonic() + self._freshness
            logger.info("模型目录后台刷新成功，后续请求可采用新目录: source=%s", self._source_url)
        except (OSError, ValueError, TimeoutError, httpx.HTTPError) as error:
            logger.warning("模型目录后台刷新失败，保留旧目录: %s: %s", type(error).__name__, error)
        finally:
            self._finished.set()

    def _save_cache(self, catalog: dict[str, object]) -> None:
        assert self._cache_path is not None
        self._cache_root.mkdir(parents=True, exist_ok=True)
        payload = {"source_url": self._source_url, "fetched_at": time.time(), "catalog": catalog}
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self._cache_root, delete=False) as stream:
                temporary = Path(stream.name)
                json.dump(payload, stream, ensure_ascii=False)
            temporary.replace(self._cache_path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)


def load_litellm() -> ModuleType:
    global _runtime
    if _local_only_mode():
        return _import_litellm()
    with _runtime_lock:
        if _runtime is None:
            from app.core.path_utils import get_boxteam_home

            _runtime = ModelMetadataRuntime(cache_root=get_boxteam_home() / "cache" / "model-metadata")
        runtime = _runtime
    return runtime.load()


def prepare_model_request() -> None:
    """请求开始前采用已完成的目录刷新，再安装显式回放 transport。"""
    from app.runtime.model_stream import ensure_model_stream_installed

    load_litellm()
    ensure_model_stream_installed()


def __getattr__(name: str) -> object:
    if name == "litellm":
        return load_litellm()
    if name == "ChatLiteLLM":
        load_litellm()
        from langchain_litellm import ChatLiteLLM

        return ChatLiteLLM
    if name in {"AsyncHTTPHandler", "HTTPHandler"}:
        load_litellm()
        module = importlib.import_module("litellm.llms.custom_httpx.http_handler")
        return getattr(module, name)
    if name == "litellm_http_handler":
        load_litellm()
        return importlib.import_module("litellm.llms.custom_httpx.llm_http_handler")
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
