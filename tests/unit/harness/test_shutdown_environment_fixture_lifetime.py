from __future__ import annotations

import errno
import json
import os
from collections.abc import Generator, Iterator
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import Mock

import pytest

from tests.integration.workspace_services import (
    test_gateway_resource_shutdown as shutdown_case,
)

pytestmark = pytest.mark.skipif(not Path("/proc/self/fd").is_dir(), reason="验证Linux目录fd短路径")


@dataclass
class ShutdownEnvironmentLifetime:
    fixture: Generator[dict[str, str], None, None]
    config_path: Path
    original_config_bytes: bytes
    temporary_directory: Path
    owned_descriptors: list[int]


def descriptor_released(descriptor: int) -> bool:
    try:
        os.fstat(descriptor)
    except OSError as error:
        assert error.errno == errno.EBADF
        return True
    return False


@pytest.fixture(params=["responses", "chat_completions"])
def shutdown_lifetime(
    request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> Iterator[ShutdownEnvironmentLifetime]:
    config_path = tmp_path / "workspace.jsonc"
    original_config_bytes = b"// Preserve original formatting\n{}\n"
    config_path.write_bytes(original_config_bytes)
    config = {
        "llm": {"providers": [
            {"id": "primary", "model": "chat-fixture", "api_mode": "chat_completions"},
            {"id": "backup_3", "model": "responses-fixture", "api_mode": "responses"},
        ]},
        "agents": {"default": {"model": {"fallback_providers": ["backup_3"]}}},
    }
    # 只验证装具资源归属；配置解析与schema校验是明确的替身边界。
    monkeypatch.setattr(shutdown_case, "read_jsonc_object", Mock(return_value=config))
    monkeypatch.setattr(shutdown_case, "validate_config", Mock())
    original_open, original_close = os.open, os.close
    owned_descriptors: list[int] = []

    def track_open(*args: object) -> int:
        descriptor = original_open(*args)
        owned_descriptors.append(descriptor)
        return descriptor

    monkeypatch.setattr(shutdown_case.os, "open", track_open)
    fixture = shutdown_case.shutdown_environment.__wrapped__(
        str(config_path), str(tmp_path / "workspace"),
        shutdown_case.shutdown_cassette.__wrapped__(request.param), request.param,
    )
    state = ShutdownEnvironmentLifetime(
        fixture, config_path, original_config_bytes, tmp_path / "runtime/tmp", owned_descriptors,
    )
    try:
        yield state
    finally:
        fixture.close()
        # 失败探针也只回收本用例捕获的真实fd，不让旧实现的泄漏污染后续用例。
        for descriptor in owned_descriptors:
            if not descriptor_released(descriptor):
                original_close(descriptor)


def test_shutdown_environment_exposes_live_short_path_then_restores_exact_config(
    shutdown_lifetime: ShutdownEnvironmentLifetime,
) -> None:
    state = shutdown_lifetime
    environment = next(state.fixture)
    descriptor = state.owned_descriptors[0]
    expected_path = f"/proc/{os.getpid()}/fd/{descriptor}"
    assert environment == {name: expected_path for name in ("TMPDIR", "TMP", "TEMP")}
    assert Path(expected_path).resolve() == state.temporary_directory
    assert not descriptor_released(descriptor)
    config = json.loads(state.config_path.read_text())
    primary = config["llm"]["providers"][0]
    assert primary["endpoint"] == (
        "https://www.cctq.ai/v1" if primary["api_mode"] == "responses" else "https://opencode.ai/zen/v1"
    )
    assert primary["api_key"] == "fixture-key"
    assert primary["model"] == ("gpt-5.6-luna" if primary["api_mode"] == "responses" else "big-pickle")
    assert config["agents"]["default"]["model"]["fallback_providers"] == []

    state.fixture.close()

    assert descriptor_released(descriptor)
    assert state.config_path.read_bytes() == state.original_config_bytes


@pytest.mark.parametrize("stage", ["write", "mkdir", "open", "resolve"])
@pytest.mark.parametrize("interrupted", [False, True])
def test_shutdown_environment_preparation_failure_restores_config_and_closes_fd(
    shutdown_lifetime: ShutdownEnvironmentLifetime, monkeypatch: pytest.MonkeyPatch,
    stage: str, interrupted: bool,
) -> None:
    state = shutdown_lifetime
    error = KeyboardInterrupt("环境准备中断") if interrupted else OSError("环境准备失败")
    original_write, original_mkdir, original_resolve = Path.write_text, Path.mkdir, Path.resolve

    def fail_write(path: Path, *args: object, **kwargs: object) -> int:
        if path == state.config_path:
            original_write(path, "partial config")
            raise error
        return original_write(path, *args, **kwargs)

    def fail_mkdir(path: Path, *args: object, **kwargs: object) -> None:
        if path == state.temporary_directory:
            raise error
        original_mkdir(path, *args, **kwargs)

    def fail_resolve(path: Path, *args: object, **kwargs: object) -> Path:
        if str(path).startswith(f"/proc/{os.getpid()}/fd/"):
            raise error
        return original_resolve(path, *args, **kwargs)

    if stage == "write":
        monkeypatch.setattr(Path, "write_text", fail_write)
    elif stage == "mkdir":
        monkeypatch.setattr(Path, "mkdir", fail_mkdir)
    elif stage == "open":
        monkeypatch.setattr(shutdown_case.os, "open", Mock(side_effect=error))
    else:
        monkeypatch.setattr(Path, "resolve", fail_resolve)
    with pytest.raises(type(error)) as failure:
        next(state.fixture)

    assert failure.value is error
    released = all(descriptor_released(descriptor) for descriptor in state.owned_descriptors)
    restored = state.config_path.read_bytes() == state.original_config_bytes
    assert (released, restored) == (True, True)


@pytest.mark.parametrize("cleanup_failure", ["none", "error", "interrupt"])
def test_shutdown_environment_body_failure_restores_config_even_if_close_fails(
    shutdown_lifetime: ShutdownEnvironmentLifetime, monkeypatch: pytest.MonkeyPatch,
    cleanup_failure: str,
) -> None:
    state = shutdown_lifetime
    body_error = LookupError("测试主体失败")
    cleanup_error = KeyboardInterrupt("关闭fd中断") if cleanup_failure == "interrupt" else OSError("关闭fd失败")
    if cleanup_failure != "none":
        # 退出栈在获取资源时保存关闭回调，故障须在登记前注入。
        monkeypatch.setattr(shutdown_case.os, "close", Mock(side_effect=cleanup_error))
    expected_error = body_error if cleanup_failure == "none" else cleanup_error
    next(state.fixture)

    with pytest.raises(type(expected_error)) as failure:
        state.fixture.throw(body_error)

    assert failure.value is expected_error
    if cleanup_failure == "none":
        assert all(descriptor_released(descriptor) for descriptor in state.owned_descriptors)
    else:
        assert cleanup_error.__context__ is body_error
    assert state.config_path.read_bytes() == state.original_config_bytes
