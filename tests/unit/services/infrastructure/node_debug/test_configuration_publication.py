"""方案写入成功才发布缓存，返回正文必须与冷读一致。"""

from __future__ import annotations

import errno
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import pytest

from app.schemas.internal_v2.node_debug import (
    NodeDebugConfigurationCreateRequest,
    NodeDebugConfigurationDTO,
    NodeDebugConfigurationUpdateRequest,
)
from app.services.infrastructure.node_debug.session import session_store
from tests.unit.services.infrastructure.node_debug.test_node_debug_launch_errors import (
    DebugLaunchContext,
    debug_launch_context,
)

__all__ = ["debug_launch_context"]

Mutation = Literal["create", "update", "import", "copy"]
_PUBLISHED_AT = datetime(2026, 12, 1, tzinfo=UTC)


@pytest.fixture(autouse=True)
def store_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    class StoreClock(datetime):
        @classmethod
        def now(cls, tz=None):
            assert tz == UTC
            return _PUBLISHED_AT

    monkeypatch.setattr(session_store, "datetime", StoreClock)


@pytest.fixture
def original(debug_launch_context: DebugLaunchContext) -> NodeDebugConfigurationDTO:
    fixture = debug_launch_context
    return fixture.service._configuration_registry.create(NodeDebugConfigurationCreateRequest(
        session_id=fixture.session_id, thread_id=fixture.thread_id,
        name="原方案", script_path="entry.mjs", activate=False,
    ))


def _mutate(
    fixture: DebugLaunchContext, original: NodeDebugConfigurationDTO, operation: Mutation,
) -> NodeDebugConfigurationDTO:
    registry = fixture.service._configuration_registry
    owner = {"session_id": fixture.session_id, "thread_id": fixture.thread_id}
    if operation == "create":
        return registry.create(NodeDebugConfigurationCreateRequest(
            **owner, name="新方案", script_path="entry.mjs", activate=False,
        ))
    if operation == "update":
        return registry.update(original.configuration_id, NodeDebugConfigurationUpdateRequest(
            **owner, name="修改方案", script_path="entry.mjs", args=["updated"],
        ))
    if operation == "import":
        return registry.import_configuration(
            fixture.session_id, original.model_copy(update={
                "configuration_id": "dbgcfg_" + "1" * 32, "name": "导入方案",
                "updated_at": datetime(2026, 1, 1, tzinfo=UTC),
            }), thread_id=fixture.thread_id,
        )
    return registry.copy_configuration(
        source_configuration=original, target_session_id=fixture.session_id,
        target_thread_id=fixture.thread_id, name="复制方案",
    )


@pytest.mark.parametrize("operation", ["create", "update", "import", "copy"])
def test_saved_return_and_cache_match_complete_cold_configuration(
    debug_launch_context: DebugLaunchContext, original: NodeDebugConfigurationDTO,
    operation: Mutation,
) -> None:
    fixture = debug_launch_context
    result = _mutate(fixture, original, operation)
    cold = fixture.store.read_configuration(fixture.session_id, result.configuration_id, fixture.thread_id)
    assert cold is not None and cold.updated_at == _PUBLISHED_AT
    assert result == cold
    assert fixture.service.get_configuration(fixture.session_id, result.configuration_id, fixture.thread_id) == cold
    registry = fixture.cold_service()._configuration_registry
    registry.ensure_loaded(fixture.session_id, fixture.thread_id)
    assert registry.get(fixture.session_id, fixture.thread_id, result.configuration_id) == cold


@pytest.mark.parametrize("entrypoint", ["start_selection", "breakpoint"])
def test_implicit_configuration_publishes_saved_body(
    debug_launch_context: DebugLaunchContext, entrypoint: str,
) -> None:
    fixture = debug_launch_context
    registry = fixture.service._configuration_registry
    registry.ensure_loaded(fixture.session_id, fixture.thread_id)
    if entrypoint == "start_selection":
        registry.select_for_start(
            session_id=fixture.session_id, thread_id=fixture.thread_id, configuration_id=None,
            path="entry.mjs", working_directory="", launch_profile_name="node-default", args=[],
        )
    else:
        registry.ensure_configuration_for_breakpoint(fixture.session_id, fixture.thread_id, path="entry.mjs")
    configuration_id = registry.active_id(fixture.session_id, fixture.thread_id)
    assert configuration_id is not None
    cold = fixture.store.read_configuration(fixture.session_id, configuration_id, fixture.thread_id)
    assert cold is not None and cold.updated_at == _PUBLISHED_AT
    assert registry.get(fixture.session_id, fixture.thread_id, configuration_id) == cold


def test_store_returns_written_copy_without_mutating_input(
    debug_launch_context: DebugLaunchContext, original: NodeDebugConfigurationDTO,
) -> None:
    fixture = debug_launch_context
    replacement = original.model_copy(update={"name": "更新正文", "updated_at": datetime(2026, 1, 1, tzinfo=UTC)})
    before = replacement.model_dump_json()
    result = fixture.store.write_configuration(fixture.session_id, replacement, fixture.thread_id)
    assert result == fixture.store.read_configuration(fixture.session_id, original.configuration_id, fixture.thread_id)
    assert result.updated_at == _PUBLISHED_AT and replacement.model_dump_json() == before


@pytest.mark.parametrize("operation", ["create", "update", "import", "copy"])
@pytest.mark.parametrize("phase", ["write", "replace"])
def test_native_write_failure_does_not_publish_unsaved_configuration(
    debug_launch_context: DebugLaunchContext, original: NodeDebugConfigurationDTO,
    operation: Mutation, phase: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = debug_launch_context
    registry = fixture.service._configuration_registry
    before = registry.list(fixture.session_id, fixture.thread_id)
    directory = fixture.workspace.resolver.resolve_thread_node(fixture.session_id, fixture.thread_id) / "debug/node/configurations"
    raw = {path: path.read_bytes() for path in directory.glob("*.json")}
    native = PermissionError(errno.EACCES, "不能发布方案", str(directory))

    def denied(*_args, **_kwargs):
        raise native

    with monkeypatch.context() as patch:
        patch.setattr(Path if phase == "write" else os, "write_text" if phase == "write" else "replace", denied)
        with pytest.raises(PermissionError) as caught:
            _mutate(fixture, original, operation)
    assert caught.value.__cause__ is native and caught.value.errno == errno.EACCES
    assert registry.list(fixture.session_id, fixture.thread_id) == before
    assert registry.active_id(fixture.session_id, fixture.thread_id) is None
    assert {path: path.read_bytes() for path in directory.glob("*.json")} == raw
    saved = _mutate(fixture, original, operation)
    assert saved == fixture.store.read_configuration(fixture.session_id, saved.configuration_id, fixture.thread_id)
