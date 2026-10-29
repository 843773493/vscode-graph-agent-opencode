"""未知目标的原始输入只留诊断，失败不得修改权威注册表。"""

from collections.abc import Callable, Iterator
from dataclasses import asdict
from pathlib import Path

import pytest

from app.core import failures
from app.gateway.control.gateway_state import GatewayStateStore
from app.gateway.registry import GatewayWorkspaceRegistry, WorkspaceTarget


@pytest.fixture
def registry_case(tmp_path: Path) -> Iterator[tuple[GatewayWorkspaceRegistry, GatewayStateStore]]:
    with GatewayStateStore(path=tmp_path / "gateway.sqlite") as state:
        registry = GatewayWorkspaceRegistry(storage_path=tmp_path / "registry.json", state_store=state)
        for index, identity in enumerate(("first-target", "second-target"), start=1):
            registry.upsert(WorkspaceTarget(
                workspace_id=identity, name=identity, root_path=str(tmp_path / identity),
                backend_url=f"http://127.0.0.1:{41000 + index}", connection_kind="local",
                backend_workspace_id=f"00000000-0000-4000-8000-{index:012d}",
            ), activate=index == 1)
        yield registry, state


@pytest.mark.parametrize("native_path", [
    "/private/lookup-marker/workspace", r"C:\private\lookup-marker\workspace",
    r"\\server\private\lookup-marker\workspace",
], ids=["posix", "drive", "unc"])
@pytest.mark.parametrize("operation,error_type", [
    ("resolve", LookupError), ("remove", KeyError), ("validate_removal", KeyError),
    ("activate", KeyError), ("update", KeyError), ("rename", KeyError),
    ("set_parent", KeyError), ("missing_parent", KeyError), ("reorder", ValueError),
])
def test_unknown_target_preserves_error_category_and_committed_registry(
    registry_case: tuple[GatewayWorkspaceRegistry, GatewayStateStore],
    native_path: str, operation: str, error_type: type[Exception], caplog: pytest.LogCaptureFixture,
) -> None:
    registry, state = registry_case
    actions: dict[str, Callable[[], object]] = {
        "resolve": lambda: registry.resolve(native_path),
        "remove": lambda: registry.remove(native_path),
        "validate_removal": lambda: registry.validate_target_removal(native_path),
        "activate": lambda: registry.activate(native_path),
        "update": lambda: registry.update(native_path, name="不能提交"),
        "rename": lambda: registry.rename(native_path, "不能提交"),
        "set_parent": lambda: registry.set_parent(native_path, "first-target"),
        "missing_parent": lambda: registry.update("first-target", name="不能提交", parent_workspace_id=native_path),
        "reorder": lambda: registry.reorder(["first-target", native_path]),
    }
    original = state.load_workspace_registry()
    targets = [asdict(target) for target in registry.targets()]
    revision, active = registry.registry_revision, registry.active_workspace_id
    with pytest.raises(error_type) as caught:
        actions[operation]()
    assert type(caught.value) is error_type
    assert "lookup-marker" not in str(caught.value)
    assert "stage=gateway-workspace-" in str(caught.value)
    cause = caught.value.__cause__
    assert cause is not None
    assert type(cause) is error_type and native_path in cause.args[0]
    assert any(record.exc_info is not None and record.exc_info[1] is cause for record in caplog.records)
    assert state.load_workspace_registry() == original
    assert [asdict(target) for target in registry.targets()] == targets
    assert (registry.registry_revision, registry.active_workspace_id) == (revision, active)
    assert registry.resolve() is registry.resolve("first-target")
    updated = registry.update("first-target", name="仍可修改")
    assert updated.name == "仍可修改" and registry.registry_revision == revision + 1


def test_lookup_failure_survives_diagnostic_sink_failure(
    registry_case: tuple[GatewayWorkspaceRegistry, GatewayStateStore], monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry, state = registry_case
    original = state.load_workspace_registry()
    sink_error = OSError("/private/sink-marker/log")

    def reject_log(*_: object, **__: object) -> None:
        raise sink_error

    monkeypatch.setattr(failures.logger, "error", reject_log)
    with pytest.raises(LookupError) as caught:
        registry.resolve("/private/lookup-marker/workspace")
    assert type(caught.value) is LookupError
    assert "lookup-marker" not in str(caught.value) and "sink-marker" not in str(caught.value)
    assert "diagnostic_sink_error=OSError" in str(caught.value)
    assert caught.value.__cause__ is not None
    assert "/private/lookup-marker/workspace" in caught.value.__cause__.args[0]
    assert any("sink-marker" in note for note in caught.value.__cause__.__notes__)
    assert state.load_workspace_registry() == original
