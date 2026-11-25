"""公共模型目录的等待、缓存及 SDK 注册保持合同。"""

from __future__ import annotations

import asyncio
import copy
import json
import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import ModuleType

import httpx
import pytest

from app.runtime import litellm_runtime
from app.runtime.litellm_runtime import ModelMetadataRuntime
from app.runtime.network_proxy import ProxyRoutes


@pytest.fixture
def sdk(monkeypatch: pytest.MonkeyPatch) -> Iterator[ModuleType]:
    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "true")
    module = litellm_runtime.load_litellm()
    from litellm import utils

    original = module.model_cost
    # 模型定义只读共享，测试另建顶层映射和 provider 名称集合隔离 SDK 写入。
    monkeypatch.setattr(module, "model_cost", dict(original))
    monkeypatch.setattr(module, "open_ai_chat_completion_models", set(module.open_ai_chat_completion_models))
    monkeypatch.setattr(utils, "_runtime_registered_model_cost", {})
    monkeypatch.setattr(module, "model_cost_map_url", "https://metadata.example/catalog.json")
    monkeypatch.setenv("LITELLM_MODEL_COST_MAP_URL", "https://metadata.example/catalog.json")
    monkeypatch.delenv("LITELLM_LOCAL_MODEL_COST_MAP")
    try:
        yield module
    finally:
        module.model_cost = original
        utils._invalidate_model_cost_lowercase_map()


def test_model_request_url_selects_responses_contract_from_explicit_endpoint(
    sdk: ModuleType, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 进程中的另一份 Azure endpoint 不能改变显式连接所选的实际 SDK 合同。
    monkeypatch.setattr(sdk, "api_base", None)
    monkeypatch.setenv("AZURE_AI_API_BASE", "https://other.models.ai.azure.com")
    assert litellm_runtime.model_request_url({
        "custom_llm_provider": "azure_ai", "model": "gpt-4.1",
        "endpoint": "https://explicit.openai.azure.com/openai/v1",
        "api_mode": {"protocol": "responses"},
    }) == "https://explicit.openai.azure.com/openai/v1/responses"


@pytest.fixture
def pending_refresh(
    sdk: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[ModelMetadataRuntime, threading.Event, dict[str, object]]]:
    runtime = ModelMetadataRuntime(routes=ProxyRoutes(()), cache_root=tmp_path / "cache")
    release = threading.Event()
    catalog = dict(sdk.model_cost)
    catalog["metadata-refresh-test"] = {"litellm_provider": "openai", "mode": "chat", "supports_vision": True}

    async def download() -> dict[str, object]:
        assert await asyncio.to_thread(release.wait, 5), "测试未释放下载屏障"
        return catalog

    monkeypatch.setattr(runtime, "_download", download)
    try:
        yield runtime, release, catalog
    finally:
        release.set()
        if runtime._worker is not None:
            runtime._worker.join(timeout=6)
            assert not runtime._worker.is_alive()


def test_initial_load_returns_before_pending_refresh(
    sdk: ModuleType, pending_refresh: tuple[ModelMetadataRuntime, threading.Event, dict[str, object]],
) -> None:
    runtime, release, _catalog = pending_refresh
    with ThreadPoolExecutor(max_workers=1) as executor:
        try:
            assert executor.submit(runtime.refresh).result(timeout=0.5) is None
            assert not runtime._finished.is_set()
        finally:
            release.set()


def test_late_refresh_is_adopted_only_on_next_use_and_keeps_registrations(
    sdk: ModuleType, pending_refresh: tuple[ModelMetadataRuntime, threading.Event, dict[str, object]],
) -> None:
    runtime, release, catalog = pending_refresh
    original_cost = sdk.get_model_info("gpt-4o")["input_cost_per_token"]
    gpt_4o_entry = dict(catalog["gpt-4o"])
    catalog["gpt-4o"] = gpt_4o_entry
    gpt_4o_entry["input_cost_per_token"] = original_cost + 0.0001
    sdk.register_model({"metadata-custom-test": {"litellm_provider": "openai", "mode": "chat", "supports_vision": True}})
    old_catalog = runtime.catalog()
    old_snapshot = copy.deepcopy(old_catalog)
    sdk_catalog = sdk.model_cost
    assert runtime.refresh() is None
    assert "metadata-refresh-test" not in runtime._catalog
    release.set()
    assert runtime._finished.wait(5)
    assert runtime._catalog is old_catalog
    assert runtime.refresh() is None
    assert "metadata-refresh-test" in runtime.catalog()
    assert sdk.model_cost["metadata-custom-test"]["supports_vision"] is True
    assert old_catalog == old_snapshot
    assert sdk.model_cost is sdk_catalog
    assert "metadata-refresh-test" not in sdk.model_cost
    assert sdk.get_model_info("gpt-4o")["input_cost_per_token"] == original_cost
    assert runtime.catalog()["gpt-4o"]["input_cost_per_token"] == original_cost + 0.0001


def test_concurrent_loads_share_one_refresh(
    pending_refresh: tuple[ModelMetadataRuntime, threading.Event, dict[str, object]],
) -> None:
    runtime, _release, _catalog = pending_refresh
    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(lambda _: runtime.refresh(), range(16)))
    assert all(result is results[0] for result in results)
    assert sum(worker.name == "model-metadata-refresh" for worker in threading.enumerate()) == 1


def test_successful_cache_is_available_without_starting_refresh(
    sdk: ModuleType, pending_refresh: tuple[ModelMetadataRuntime, threading.Event, dict[str, object]],
    tmp_path: Path,
) -> None:
    runtime, release, _catalog = pending_refresh
    runtime.refresh()
    release.set()
    assert runtime._finished.wait(5)
    restored = ModelMetadataRuntime(routes=ProxyRoutes(()), cache_root=tmp_path / "cache")
    restored.refresh()
    assert restored.catalog()["metadata-refresh-test"]["supports_vision"] is True
    assert restored._worker is None


def test_corrupt_cache_is_reported_and_retains_catalog(
    sdk: ModuleType, pending_refresh: tuple[ModelMetadataRuntime, threading.Event, dict[str, object]],
    caplog: pytest.LogCaptureFixture,
) -> None:
    runtime, release, _catalog = pending_refresh
    runtime.refresh()
    release.set()
    assert runtime._finished.wait(5)
    assert runtime._cache_path is not None
    runtime._cache_path.write_text("{invalid", encoding="utf-8")
    original = sdk.model_cost
    restored = ModelMetadataRuntime(routes=ProxyRoutes(()), cache_root=runtime._cache_root)
    restored._catalog = runtime.catalog()
    restored._source_url = runtime._source_url
    restored._cache_path = runtime._cache_path
    restored._load_cache()
    assert sdk.model_cost is original
    assert "模型目录缓存不可用" in caplog.text


def test_cache_is_scoped_to_remote_source(
    sdk: ModuleType, pending_refresh: tuple[ModelMetadataRuntime, threading.Event, dict[str, object]],
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    runtime, release, _catalog = pending_refresh
    runtime.refresh()
    release.set()
    assert runtime._finished.wait(5)
    assert runtime._cache_path is not None
    cached = json.loads(runtime._cache_path.read_text())
    cached["source_url"] = "https://another.example/catalog.json"
    runtime._cache_path.write_text(json.dumps(cached))
    old = sdk.model_cost
    runtime._load_cache()
    assert sdk.model_cost is old
    assert "缓存来源不匹配" in caplog.text


def test_background_total_deadline_cancels_transport_and_keeps_old_table(
    sdk: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    cancelled = threading.Event()

    async def hanging(_request: httpx.Request) -> httpx.Response:
        try:
            await asyncio.sleep(60)
        finally:
            cancelled.set()
        raise AssertionError("超时必须取消请求")

    client_type = httpx.AsyncClient
    monkeypatch.setattr(litellm_runtime.httpx, "AsyncClient", lambda **_kwargs: client_type(transport=httpx.MockTransport(hanging)))
    runtime = ModelMetadataRuntime(routes=ProxyRoutes(()), cache_root=tmp_path / "cache", refresh_timeout_seconds=0.05)
    original = sdk.model_cost
    runtime.refresh()
    assert runtime._finished.wait(5)
    assert cancelled.is_set()
    assert sdk.model_cost is original
    assert not list((tmp_path / "cache").glob("*.json"))
    assert "TimeoutError" in caplog.text
    worker = runtime._worker
    runtime.refresh()
    assert runtime._worker is worker, "失败后不能每个模型请求都重新下载"


def test_explicit_local_mode_never_starts_refresh(
    sdk: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "true")
    runtime = ModelMetadataRuntime(routes=ProxyRoutes(()), cache_root=tmp_path / "cache")
    assert runtime.refresh() is None
    assert runtime._worker is None
    assert not (tmp_path / "cache").exists()


@pytest.mark.parametrize("previous", [None, "false", "true"])
def test_import_restores_original_environment(previous: str | None, monkeypatch: pytest.MonkeyPatch) -> None:
    if previous is None:
        monkeypatch.delenv("LITELLM_LOCAL_MODEL_COST_MAP", raising=False)
    else:
        monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", previous)
    litellm_runtime._import_litellm()
    assert litellm_runtime.os.environ.get("LITELLM_LOCAL_MODEL_COST_MAP") == previous


def test_failed_import_restores_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "false")

    def reject(_name: str) -> ModuleType:
        raise ImportError("SDK 安装不完整")

    monkeypatch.setattr(litellm_runtime.importlib, "import_module", reject)
    with pytest.raises(ImportError, match="SDK 安装不完整"):
        litellm_runtime._import_litellm()
    assert litellm_runtime.os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] == "false"


def test_local_catalog_and_refresh_do_not_wait_for_background_sdk_import(
    sdk: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    importing, release = threading.Event(), threading.Event()

    def slow_import(_name: str) -> ModuleType:
        importing.set()
        assert release.wait(5)
        return sdk

    async def no_download(_self):
        return None

    monkeypatch.delitem(litellm_runtime.sys.modules, "litellm")
    monkeypatch.setattr(litellm_runtime.importlib, "import_module", slow_import)
    monkeypatch.setattr(ModelMetadataRuntime, "_download", no_download)
    runtime = ModelMetadataRuntime(cache_root=tmp_path / "cache", routes=ProxyRoutes(()))
    with ThreadPoolExecutor(max_workers=2) as executor:
        pending = executor.submit(litellm_runtime._import_litellm)
        try:
            assert importing.wait(2)
            assert executor.submit(runtime.catalog).result(timeout=0.5)
            assert executor.submit(runtime.refresh).result(timeout=0.5) is None
            assert runtime._worker is not None, "导入期临时 local-only 值不能禁用真实刷新"
        finally:
            release.set()
            assert pending.result(timeout=5) is sdk
            runtime.close()


def test_invalid_remote_catalog_cannot_replace_successful_cache(
    sdk: ModuleType, pending_refresh: tuple[ModelMetadataRuntime, threading.Event, dict[str, object]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, release, _catalog = pending_refresh
    runtime.refresh()
    release.set()
    assert runtime._finished.wait(5)
    runtime.refresh()
    assert runtime._cache_path is not None
    cached_bytes = runtime._cache_path.read_bytes()
    previous = sdk.model_cost
    client_type = httpx.AsyncClient
    monkeypatch.setattr(litellm_runtime.httpx, "AsyncClient", lambda **_kwargs: client_type(
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, json={"not-a-catalog": {}})),
    ))
    # 让本轮使用真正的官方下载和校验，替换的只有外部 HTTP 边界。
    monkeypatch.setattr(runtime, "_download", ModelMetadataRuntime._download.__get__(runtime))
    runtime._next_refresh = time.monotonic() - 1
    runtime.refresh()
    assert runtime._finished.wait(5)
    runtime.refresh()
    assert sdk.model_cost is previous
    assert runtime._cache_path.read_bytes() == cached_bytes


def test_failed_cache_publication_keeps_previous_bytes_and_removes_temporary(
    sdk: ModuleType, pending_refresh: tuple[ModelMetadataRuntime, threading.Event, dict[str, object]],
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    runtime, release, _catalog = pending_refresh
    runtime.refresh()
    release.set()
    assert runtime._finished.wait(5)
    runtime.refresh()
    assert runtime._cache_path is not None
    previous_bytes = runtime._cache_path.read_bytes()
    previous = sdk.model_cost

    def reject_replace(_source: Path, _target: Path) -> Path:
        raise PermissionError("目录缓存发布被拒绝")

    monkeypatch.setattr(litellm_runtime.Path, "replace", reject_replace)
    runtime._next_refresh = time.monotonic() - 1
    runtime.refresh()
    assert runtime._finished.wait(5)
    runtime.refresh()
    assert sdk.model_cost is previous
    assert runtime._cache_path.read_bytes() == previous_bytes
    assert list(runtime._cache_root.iterdir()) == [runtime._cache_path]
    assert "目录缓存发布被拒绝" in caplog.text


@pytest.fixture
def metadata_owners(sdk: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("BOXTEAM_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(litellm_runtime, "_runtime", None)
    monkeypatch.setattr(litellm_runtime, "_owner_runtimes", {})
    monkeypatch.setattr(litellm_runtime, "_retired_runtimes", {})
    yield
    for owner in list(litellm_runtime._owner_runtimes):
        asyncio.run(litellm_runtime.release_model_metadata(owner))


def test_unbound_sdk_and_configuration_reads_do_not_start_network(metadata_owners, monkeypatch: pytest.MonkeyPatch):
    def forbidden(*_args, **_kwargs):
        pytest.fail("尚未绑定 Workspace 默认路由时不能启动元数据网络")
    monkeypatch.setattr(ModelMetadataRuntime, "_download", forbidden)
    assert litellm_runtime.load_litellm() is not None
    assert litellm_runtime.local_model_catalog()
    litellm_runtime.prepare_model_request()
    assert litellm_runtime._metadata_runtime()._worker is None


def test_workspace_route_generation_cancels_old_download_and_releases_all_clients(
    metadata_owners, monkeypatch: pytest.MonkeyPatch,
):
    started = threading.Event()
    cancelled = []
    clients = []
    original_client = httpx.AsyncClient
    async def hanging(request: httpx.Request):
        started.set()
        try:
            await asyncio.sleep(60)
        finally:
            cancelled.append(request.url)
    def client(**kwargs):
        assert kwargs["trust_env"] is False
        # 真实 HTTPX client 的关闭仍由被测 owner 持有；只替换传输边界。
        instance = original_client(transport=httpx.MockTransport(hanging), trust_env=False)
        clients.append(instance)
        return instance
    monkeypatch.setattr(litellm_runtime.httpx, "AsyncClient", client)
    litellm_runtime.refresh_model_metadata(owner_key="workspace-a", routes=ProxyRoutes(()))
    assert started.wait(3)
    old = litellm_runtime._owner_runtimes["workspace-a"][1]
    started.clear()
    changed = ProxyRoutes((("all://", httpx.Proxy("http://127.0.0.1:1")),))
    litellm_runtime.refresh_model_metadata(owner_key="workspace-a", routes=changed)
    assert started.wait(3)
    current = litellm_runtime._owner_runtimes["workspace-a"][1]
    assert current is not old
    asyncio.run(litellm_runtime.release_model_metadata("workspace-a"))
    assert old._finished.is_set() and current._finished.is_set()
    assert all(instance.is_closed for instance in clients)
    assert len(cancelled) == 2
    assert "workspace-a" not in litellm_runtime._owner_runtimes
    assert "workspace-a" not in litellm_runtime._retired_runtimes


def test_replay_restores_owner_cache_without_constructing_network(
    metadata_owners, sdk: ModuleType, monkeypatch: pytest.MonkeyPatch,
):
    async def download(_self):
        return {**sdk.model_cost, "replay-cached-model": {"litellm_provider": "openai", "mode": "chat"}}
    monkeypatch.setattr(ModelMetadataRuntime, "_download", download)
    litellm_runtime.refresh_model_metadata(owner_key="workspace-a", routes=ProxyRoutes(()))
    first = litellm_runtime._owner_runtimes["workspace-a"][1]
    assert first._finished.wait(5)
    assert first._cache_path is not None and first._cache_path.is_file()
    asyncio.run(litellm_runtime.release_model_metadata("workspace-a"))
    def forbidden(*_args, **_kwargs):
        pytest.fail("回放不能构造元数据传输或发起下载")
    monkeypatch.setattr("app.runtime.model_stream.is_model_stream_replay", lambda: True)
    monkeypatch.setattr(ModelMetadataRuntime, "_download", forbidden)
    monkeypatch.setattr(ProxyRoutes, "async_mounts", forbidden)
    litellm_runtime.refresh_model_metadata(owner_key="workspace-a", routes=ProxyRoutes(()))
    restored = litellm_runtime._owner_runtimes["workspace-a"][1]
    assert restored._cache_path == first._cache_path
    assert restored._worker is None
    assert restored.catalog()["replay-cached-model"] == {"litellm_provider": "openai", "mode": "chat"}
