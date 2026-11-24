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
from collections.abc import Mapping
from copy import deepcopy
from hashlib import sha256
from importlib.metadata import distribution
from pathlib import Path
from threading import Event, Lock, Thread
from types import ModuleType
from typing import TYPE_CHECKING, Any

import httpx

from app.runtime.network_proxy import ProxyRoutes

if TYPE_CHECKING:
    import litellm
    from langchain_litellm import ChatLiteLLM
    from litellm.llms.custom_httpx.http_handler import AsyncHTTPHandler, HTTPHandler

__all__ = ["AsyncHTTPHandler", "ChatLiteLLM", "HTTPHandler", "litellm"]

logger = logging.getLogger(__name__)
_LOCAL_ONLY = "LITELLM_LOCAL_MODEL_COST_MAP"
_import_lock = Lock()
_import_local_only: bool | None = None
_runtime_lock = Lock()
_runtime: ModelMetadataRuntime | None = None
_owner_runtimes: dict[str, tuple[str, ModelMetadataRuntime]] = {}
_retired_runtimes: dict[str, list[ModelMetadataRuntime]] = {}


def _import_litellm() -> ModuleType:
    # TODO: 上游提供轻量 URL／参数元数据入口后迁移冷 inspection；当前完整导入约3.7秒，详见配置继承7.4验收。
    global _import_local_only
    with _import_lock:
        if "litellm" in sys.modules:
            return importlib.import_module("litellm")
        previous = os.environ.get(_LOCAL_ONLY)
        # TODO: 上游提供无网络导入参数后移除此临时开关；不修改 SDK 的下载函数。
        _import_local_only = (previous or "").lower() == "true"
        os.environ[_LOCAL_ONLY] = "true"
        try:
            return importlib.import_module("litellm")
        finally:
            if previous is None:
                os.environ.pop(_LOCAL_ONLY, None)
            else:
                os.environ[_LOCAL_ONLY] = previous
            _import_local_only = None


def _local_only_mode() -> bool:
    # 不等待后台 SDK 导入，也不能把导入期临时值当成用户配置。
    importing = _import_local_only
    return importing if importing is not None else os.environ.get(_LOCAL_ONLY, "").lower() == "true"


def model_request_url(connection: Mapping[str, Any]) -> str:
    """预检使用实际 SDK 的地址规则，不创建客户端、解析凭据或发起网络调用。"""
    _import_litellm()
    from litellm.secret_managers.main import get_secret_bool
    from litellm.types.utils import LlmProviders
    from litellm.utils import ProviderConfigManager

    protocol = connection["api_mode"]["protocol"]
    provider = connection["custom_llm_provider"]
    endpoint = connection["endpoint"]
    if protocol == "anthropic_messages":
        # TODO: LiteLLM 将 main._complete_anthropic 的地址准备公开后直接调用它。
        if not get_secret_bool("LITELLM_ANTHROPIC_DISABLE_URL_SUFFIX") and not endpoint.endswith("/v1/messages"):
            endpoint += "/v1/messages"
        return endpoint
    if protocol == "responses":
        if provider == "chatgpt":
            # TODO: SDK 提供无认证副作用的地址准备入口后直接复用；当前构造器会创建令牌目录。
            # 与 ChatGPTResponsesAPIConfig.get_complete_url 的显式 api_base 分支一致。
            return endpoint.rstrip("/") + "/responses"
        config = ProviderConfigManager.get_provider_responses_api_config(provider, connection["model"])
        if config is None:
            raise ValueError(f"Provider {provider} 没有 Responses 地址合同")
        return config.get_complete_url(api_base=endpoint, litellm_params={})
    config = ProviderConfigManager.get_provider_chat_config(connection["model"], LlmProviders(provider))
    if config is None:
        raise ValueError(f"Provider {provider} 没有 Chat Completions 地址合同")
    return config.get_complete_url(api_base=endpoint, api_key=None, model=connection["model"],
                                   optional_params={}, litellm_params={})


def anthropic_request_body(payload: Mapping[str, Any]) -> dict[str, Any]:
    """封存与发送校验复用 LiteLLM 自身的 Anthropic 正文转换，不发送请求。"""
    sdk = _import_litellm()
    from litellm.utils import get_optional_params

    params = deepcopy(dict(payload))
    model = params.pop("model")
    messages = params.pop("messages")
    config = sdk.AnthropicConfig()
    messages = config.translate_developer_role_to_system_role(
        messages=messages, model=model, custom_llm_provider="anthropic",
        api_base=params.get("api_base"), model_info=params.get("model_info"),
    )
    options = get_optional_params(model=model, messages=messages, custom_llm_provider="anthropic", **params)
    # 与原 handler 一致：流模式在完成正文转换后写入，内部控制值不进入 HTTP 正文。
    # TODO: LiteLLM 公开 handler 的最终正文转换后，直接复用并移除此处控制字段拆分。
    stream = options.pop("stream", None)
    options.pop("json_mode", None)
    options.pop("is_vertex_request", None)
    options.pop("vertex_count_tokens_location", None)
    body = config.transform_request(model=model, messages=messages, optional_params=options,
                                    litellm_params={}, headers={})
    if stream:
        body["stream"] = True
    return body


class ModelMetadataRuntime:
    """同一 Workspace／有效路由共享刷新；后台只发布缓存，后续读取才采用。"""

    def __init__(
        self, *, cache_root: Path | None, routes: ProxyRoutes | None = None,
        refresh_timeout_seconds: float = 30, freshness_seconds: float = 86400,
        retry_interval_seconds: float = 300,
    ) -> None:
        if min(
            refresh_timeout_seconds, freshness_seconds, retry_interval_seconds,
        ) <= 0:
            raise ValueError("模型目录刷新期限和缓存期限必须为正数")
        self._cache_root = cache_root
        self._routes = routes
        self._refresh_timeout = refresh_timeout_seconds
        self._freshness = freshness_seconds
        self._retry_interval = retry_interval_seconds
        self._lock = Lock()
        self._finished = Event()
        self._catalog: dict[str, object] | None = None
        self._worker: Thread | None = None
        self._pending: dict[str, object] | None = None
        self._next_refresh = 0.0
        self._source_url = ""
        self._cache_path: Path | None = None
        self._closed = False
        self._refresh_loop: asyncio.AbstractEventLoop | None = None
        self._refresh_task: asyncio.Task[object] | None = None

    def catalog(self) -> Mapping[str, object]:
        """配置读取只加载数据文件；不导入 SDK、不联网，也不另建缓存。"""
        with self._lock:
            if self._catalog is None:
                bundled = distribution("litellm").locate_file("litellm/model_prices_and_context_window_backup.json")
                self._catalog = json.loads(bundled.read_text(encoding="utf-8"))
                self._source_url = os.environ.get("LITELLM_MODEL_COST_MAP_URL",
                    "https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json")
                self._cache_path = (self._cache_root / (sha256(self._source_url.encode()).hexdigest() + ".json")
                                    if self._cache_root is not None else None)
                if self._cache_path is not None and not _local_only_mode():
                    self._load_cache()
            self._adopt_pending()
            return self._catalog

    def refresh(self) -> None:
        """准备本 owner 的本地目录，再调度有界下载；SDK 导入也留在后台。"""
        self.catalog()
        with self._lock:
            if self._closed:
                raise RuntimeError("模型目录 owner 已关闭")
            if not _local_only_mode() and self._routes is not None and time.monotonic() >= self._next_refresh and (
                self._worker is None or not self._worker.is_alive()
            ):
                self._finished.clear()
                self._next_refresh = time.monotonic() + self._retry_interval
                self._worker = Thread(target=self._refresh, name="model-metadata-refresh", daemon=True)
                self._worker.start()

    def _adopt_pending(self) -> None:
        if self._pending is not None and not self._closed:
            self._catalog = self._pending
            self._pending = None

    def _load_cache(self) -> None:
        assert self._cache_path is not None
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
            if not catalog or not all(isinstance(item, dict) for item in catalog.values()):
                raise ValueError("模型目录缓存条目无效")
        except (OSError, ValueError, TypeError, KeyError) as error:
            logger.warning("模型目录缓存不可用，使用已有目录: path=%s error=%s", self._cache_path, error)
            return
        self._catalog = catalog
        self._next_refresh = time.monotonic() + max(0, self._freshness - (time.time() - fetched_at))
        logger.info("已加载模型目录缓存: source=%s fetched_at=%s", self._source_url, fetched_at)

    async def _download(self) -> dict[str, object] | None:
        _import_litellm()
        from litellm.litellm_core_utils.get_model_cost_map import (
            ModelCostMapReloadUnavailable,
            refetch_model_cost_map,
        )

        assert self._routes is not None
        verify = httpx.create_ssl_context(trust_env=True)
        async with asyncio.timeout(self._refresh_timeout), httpx.AsyncClient(
            verify=verify, trust_env=False, mounts=self._routes.async_mounts(verify=verify),
        ) as client:
            result = await refetch_model_cost_map(url=self._source_url, client=client)
        if isinstance(result, ModelCostMapReloadUnavailable):
            logger.warning("模型目录后台刷新失败，保留旧目录: %s", result.reason)
            return None
        return result.model_cost_map

    async def _run_download(self) -> dict[str, object] | None:
        with self._lock:
            if self._closed:
                return None
            self._refresh_loop = asyncio.get_running_loop()
            self._refresh_task = asyncio.current_task()
        try:
            return await self._download()
        finally:
            with self._lock:
                self._refresh_loop = self._refresh_task = None

    def cancel(self) -> None:
        with self._lock:
            self._closed = True
            if self._refresh_loop is not None and self._refresh_task is not None:
                self._refresh_loop.call_soon_threadsafe(self._refresh_task.cancel)

    def close(self) -> None:
        self.cancel()
        if self._worker is not None:
            self._worker.join(timeout=self._refresh_timeout + 1)
            if self._worker.is_alive():
                raise RuntimeError("模型目录 owner 关闭后后台刷新仍未退出")

    def _refresh(self) -> None:
        try:
            catalog = asyncio.run(self._run_download())
            if catalog is None:
                return
            self._save_cache(catalog)
            with self._lock:
                self._pending = catalog
                self._next_refresh = time.monotonic() + self._freshness
            logger.info("模型目录后台刷新成功，后续请求可采用新目录: source=%s", self._source_url)
        except asyncio.CancelledError:
            logger.debug("模型目录 owner 关闭，取消后台刷新")
        except (OSError, ValueError, TimeoutError, httpx.HTTPError) as error:
            logger.warning("模型目录后台刷新失败，保留旧目录: %s: %s", type(error).__name__, error)
        finally:
            self._finished.set()

    def _save_cache(self, catalog: dict[str, object]) -> None:
        assert self._cache_path is not None
        assert self._cache_root is not None
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


def _metadata_runtime() -> ModelMetadataRuntime:
    global _runtime
    with _runtime_lock:
        if _runtime is None:
            _runtime = ModelMetadataRuntime(cache_root=None)
        return _runtime


def local_model_catalog(*, owner_key: str | None = None) -> Mapping[str, object]:
    if owner_key is not None:
        with _runtime_lock:
            current = _owner_runtimes.get(owner_key)
        if current is not None:
            return current[1].catalog()
    return _metadata_runtime().catalog()


def refresh_model_metadata(*, owner_key: str, routes: ProxyRoutes) -> None:
    """Workspace owner 显式准备默认出口；同值重用，换代不复用旧下载客户端。"""
    from app.runtime.model_stream import is_model_stream_replay

    if _local_only_mode():
        return
    replay = is_model_stream_replay()
    from app.core.path_utils import get_boxteam_home

    revision = routes.revision
    retired = None
    with _runtime_lock:
        current = _owner_runtimes.get(owner_key)
        if current is None or current[0] != revision:
            previous = _retired_runtimes.get(owner_key, [])
            _retired_runtimes[owner_key] = [item for item in previous if item._worker is not None and item._worker.is_alive()]
            if current is not None:
                retired = current[1]
                _retired_runtimes[owner_key].append(retired)
            identity = sha256((owner_key + "\0" + revision).encode()).hexdigest()
            runtime = ModelMetadataRuntime(
                cache_root=get_boxteam_home() / "cache" / "model-metadata" / identity, routes=routes,
            )
            _owner_runtimes[owner_key] = (revision, runtime)
        else:
            runtime = current[1]
    if retired is not None:
        retired.cancel()
    if replay:
        runtime.catalog()
    else:
        runtime.refresh()


async def release_model_metadata(owner_key: str) -> None:
    """Workspace 生命周期结束时移除本 owner 的当前代与尚在退出的旧下载。"""
    with _runtime_lock:
        current = _owner_runtimes.pop(owner_key, None)
        runtimes = _retired_runtimes.pop(owner_key, [])
        if current is not None:
            runtimes.append(current[1])
    for runtime in runtimes:
        runtime.cancel()
    await asyncio.gather(*(asyncio.to_thread(runtime.close) for runtime in runtimes))


def load_litellm() -> ModuleType:
    # SDK 全局目录只包含内置数据及显式注册，不采纳任意 Workspace 下载结果。
    return _import_litellm()


def prepare_model_request() -> None:
    """只准备 SDK 与显式回放 transport，目录刷新由 Workspace owner 调度。"""
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
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
