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


@pytest.fixture
def sdk(monkeypatch: pytest.MonkeyPatch) -> Iterator[ModuleType]:
    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "true")
    module = litellm_runtime.load_litellm()
    from litellm import utils

    original = module.model_cost
    monkeypatch.setattr(module, "model_cost", copy.deepcopy(original))
    monkeypatch.setattr(module, "open_ai_chat_completion_models", set(module.open_ai_chat_completion_models))
    monkeypatch.setattr(utils, "_runtime_registered_model_cost", {})
    monkeypatch.setattr(module, "model_cost_map_url", "https://metadata.example/catalog.json")
    monkeypatch.delenv("LITELLM_LOCAL_MODEL_COST_MAP")
    try:
        yield module
    finally:
        module.model_cost = original
        utils._invalidate_model_cost_lowercase_map()


@pytest.fixture
def pending_refresh(
    sdk: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[ModelMetadataRuntime, threading.Event, dict[str, object]]]:
    runtime = ModelMetadataRuntime(cache_root=tmp_path / "cache", startup_wait_seconds=0)
    release = threading.Event()
    catalog = copy.deepcopy(sdk.model_cost)
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


def test_late_refresh_is_adopted_only_on_next_use_and_keeps_registrations(
    sdk: ModuleType, pending_refresh: tuple[ModelMetadataRuntime, threading.Event, dict[str, object]],
) -> None:
    runtime, release, catalog = pending_refresh
    original_cost = sdk.get_model_info("gpt-4o")["input_cost_per_token"]
    catalog["gpt-4o"]["input_cost_per_token"] = original_cost + 0.0001
    sdk.register_model({"metadata-custom-test": {"litellm_provider": "openai", "mode": "chat", "supports_vision": True}})
    old_catalog = sdk.model_cost
    old_snapshot = copy.deepcopy(old_catalog)
    assert runtime.load() is sdk
    assert "metadata-refresh-test" not in sdk.model_cost
    release.set()
    assert runtime._finished.wait(5)
    assert sdk.model_cost is old_catalog
    assert runtime.load() is sdk
    assert "metadata-refresh-test" in sdk.model_cost
    assert sdk.model_cost["metadata-custom-test"]["supports_vision"] is True
    assert old_catalog == old_snapshot
    assert "metadata-refresh-test" in sdk.open_ai_chat_completion_models
    assert sdk.get_model_info("gpt-4o")["input_cost_per_token"] == original_cost + 0.0001


def test_concurrent_loads_share_one_refresh(
    pending_refresh: tuple[ModelMetadataRuntime, threading.Event, dict[str, object]],
) -> None:
    runtime, _release, _catalog = pending_refresh
    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(lambda _: runtime.load(), range(16)))
    assert all(result is results[0] for result in results)
    assert sum(worker.name == "model-metadata-refresh" for worker in threading.enumerate()) == 1


def test_successful_cache_is_available_without_starting_refresh(
    sdk: ModuleType, pending_refresh: tuple[ModelMetadataRuntime, threading.Event, dict[str, object]],
    tmp_path: Path,
) -> None:
    runtime, release, _catalog = pending_refresh
    runtime.load()
    release.set()
    assert runtime._finished.wait(5)
    restored = ModelMetadataRuntime(cache_root=tmp_path / "cache", startup_wait_seconds=0)
    restored.load()
    assert sdk.model_cost["metadata-refresh-test"]["supports_vision"] is True
    assert restored._worker is None


def test_corrupt_cache_is_reported_and_retains_catalog(
    sdk: ModuleType, pending_refresh: tuple[ModelMetadataRuntime, threading.Event, dict[str, object]],
    caplog: pytest.LogCaptureFixture,
) -> None:
    runtime, release, _catalog = pending_refresh
    runtime.load()
    release.set()
    assert runtime._finished.wait(5)
    assert runtime._cache_path is not None
    runtime._cache_path.write_text("{invalid", encoding="utf-8")
    original = sdk.model_cost
    restored = ModelMetadataRuntime(cache_root=runtime._cache_root, startup_wait_seconds=0)
    restored._sdk = sdk
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
    runtime.load()
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
    monkeypatch.setattr(litellm_runtime.httpx, "AsyncClient", lambda: client_type(transport=httpx.MockTransport(hanging)))
    runtime = ModelMetadataRuntime(cache_root=tmp_path / "cache", startup_wait_seconds=0, refresh_timeout_seconds=0.05)
    original = sdk.model_cost
    runtime.load()
    assert runtime._finished.wait(5)
    assert cancelled.is_set()
    assert sdk.model_cost is original
    assert not list((tmp_path / "cache").glob("*.json"))
    assert "TimeoutError" in caplog.text
    worker = runtime._worker
    runtime.load()
    assert runtime._worker is worker, "失败后不能每个模型请求都重新下载"


def test_explicit_local_mode_never_starts_refresh(
    sdk: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "true")
    runtime = ModelMetadataRuntime(cache_root=tmp_path / "cache")
    assert runtime.load() is sdk
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


def test_invalid_remote_catalog_cannot_replace_successful_cache(
    sdk: ModuleType, pending_refresh: tuple[ModelMetadataRuntime, threading.Event, dict[str, object]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, release, _catalog = pending_refresh
    runtime.load()
    release.set()
    assert runtime._finished.wait(5)
    runtime.load()
    assert runtime._cache_path is not None
    cached_bytes = runtime._cache_path.read_bytes()
    previous = sdk.model_cost
    client_type = httpx.AsyncClient
    monkeypatch.setattr(litellm_runtime.httpx, "AsyncClient", lambda: client_type(
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, json={"not-a-catalog": {}})),
    ))
    # 让本轮使用真正的官方下载和校验，替换的只有外部 HTTP 边界。
    monkeypatch.setattr(runtime, "_download", ModelMetadataRuntime._download.__get__(runtime))
    runtime._next_refresh = time.monotonic() - 1
    runtime.load()
    assert runtime._finished.wait(5)
    runtime.load()
    assert sdk.model_cost is previous
    assert runtime._cache_path.read_bytes() == cached_bytes


def test_failed_cache_publication_keeps_previous_bytes_and_removes_temporary(
    sdk: ModuleType, pending_refresh: tuple[ModelMetadataRuntime, threading.Event, dict[str, object]],
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    runtime, release, _catalog = pending_refresh
    runtime.load()
    release.set()
    assert runtime._finished.wait(5)
    runtime.load()
    assert runtime._cache_path is not None
    previous_bytes = runtime._cache_path.read_bytes()
    previous = sdk.model_cost

    def reject_replace(_source: Path, _target: Path) -> Path:
        raise PermissionError("目录缓存发布被拒绝")

    monkeypatch.setattr(litellm_runtime.Path, "replace", reject_replace)
    runtime._next_refresh = time.monotonic() - 1
    runtime.load()
    assert runtime._finished.wait(5)
    runtime.load()
    assert sdk.model_cost is previous
    assert runtime._cache_path.read_bytes() == previous_bytes
    assert list(runtime._cache_root.iterdir()) == [runtime._cache_path]
    assert "目录缓存发布被拒绝" in caplog.text
