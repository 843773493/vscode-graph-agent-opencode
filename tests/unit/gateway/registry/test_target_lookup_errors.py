"""未知目标的原始输入只留诊断，失败不得修改权威注册表。"""

from collections.abc import Callable, Iterator
from contextlib import closing
from dataclasses import asdict
from pathlib import Path

import pytest

from app.core import failures
from app.gateway.control.gateway_state import GatewayStateStore
from app.gateway.federation import FEDERATION_PROTOCOL_VERSION, RemoteGatewayConnection
from app.gateway.registry import (
    GatewayWorkspaceRegistry,
    WorkspaceRouteLease,
    WorkspaceTarget,
)
from app.gateway.runtime.workspace import WorkspaceRuntime

LOCAL_GATEWAY_ID = "gateway_" + "1" * 32
REMOTE_GATEWAY_ID = "gateway_" + "2" * 32
FIRST_WORKSPACE_ID = "00000000-0000-4000-8000-000000000001"
UNKNOWN_WORKSPACE_ID = "00000000-0000-4000-8000-000000000003"
WORKSPACE_LOOKUP_ERRORS = [
    ("resolve", LookupError), ("remove", KeyError), ("validate_removal", KeyError),
    ("activate", KeyError), ("update", KeyError), ("rename", KeyError),
    ("set_parent", KeyError), ("missing_parent", KeyError), ("reorder", ValueError),
]


@pytest.fixture
def registry_case(tmp_path: Path) -> Iterator[tuple[GatewayWorkspaceRegistry, GatewayStateStore]]:
    with (
        GatewayStateStore(path=tmp_path / "gateway.sqlite") as state,
        closing(GatewayWorkspaceRegistry(storage_path=tmp_path / "registry.json", state_store=state, gateway_id=LOCAL_GATEWAY_ID)) as registry,
    ):
        for index, name in enumerate(("first-target", "second-target"), start=1):
            registry.upsert(WorkspaceTarget(
                gateway_id=registry.gateway_id,
                workspace_id=f"00000000-0000-4000-8000-{index:012d}",
                name=name, root_path=str(tmp_path / name),
                backend_url=f"http://127.0.0.1:{41000 + index}", connection_kind="local",
            ), activate=index == 1)
        yield registry, state


@pytest.fixture
def workspace_actions(
    registry_case: tuple[GatewayWorkspaceRegistry, GatewayStateStore],
) -> dict[str, Callable[[str], object]]:
    registry, _ = registry_case
    first_key = (registry.gateway_id, FIRST_WORKSPACE_ID)
    return {
        "resolve": registry.resolve,
        "remove": registry.remove,
        "validate_removal": registry.validate_target_removal,
        "activate": registry.activate,
        "update": lambda workspace_id: registry.update(workspace_id, name="不能提交"),
        "rename": lambda workspace_id: registry.rename(workspace_id, "不能提交"),
        "set_parent": lambda workspace_id: registry.set_parent(workspace_id, first_key),
        "missing_parent": lambda workspace_id: registry.update(FIRST_WORKSPACE_ID, name="不能提交", parent_workspace=(registry.gateway_id, workspace_id)),
        "reorder": lambda workspace_id: registry.reorder([first_key, (registry.gateway_id, workspace_id)]),
    }


@pytest.fixture
def first_route_lease(
    registry_case: tuple[GatewayWorkspaceRegistry, GatewayStateStore],
) -> Iterator[WorkspaceRouteLease]:
    registry, _ = registry_case
    lease = registry.acquire_route_reference(FIRST_WORKSPACE_ID, streaming=False)
    try:
        yield lease
    finally:
        registry.release_route_reference(FIRST_WORKSPACE_ID, streaming=False)


@pytest.mark.parametrize("operation,error_type", WORKSPACE_LOOKUP_ERRORS)
def test_unknown_target_preserves_error_category_and_committed_registry(
    registry_case: tuple[GatewayWorkspaceRegistry, GatewayStateStore],
    workspace_actions: dict[str, Callable[[str], object]], first_route_lease: WorkspaceRouteLease,
    operation: str, error_type: type[Exception], caplog: pytest.LogCaptureFixture,
) -> None:
    registry, state = registry_case
    original = state.load_workspace_registry()
    targets = [asdict(target) for target in registry.targets()]
    revision, active = registry.registry_revision, registry.active_workspace
    assert active == (registry.gateway_id, FIRST_WORKSPACE_ID)
    with pytest.raises(error_type) as caught:
        workspace_actions[operation](UNKNOWN_WORKSPACE_ID)
    assert type(caught.value) is error_type
    assert UNKNOWN_WORKSPACE_ID not in str(caught.value)
    assert "stage=gateway-workspace-" in str(caught.value)
    cause = caught.value.__cause__
    assert cause is not None
    assert type(cause) is error_type and UNKNOWN_WORKSPACE_ID in cause.args[0]
    assert any(record.exc_info is not None and record.exc_info[1] is cause for record in caplog.records)
    assert state.load_workspace_registry() == original
    assert [asdict(target) for target in registry.targets()] == targets
    assert (registry.registry_revision, registry.active_workspace) == (revision, active)
    assert registry.resolve() is registry.resolve(FIRST_WORKSPACE_ID)
    assert not first_route_lease.invalidated.is_set()
    assert registry.route_lease(FIRST_WORKSPACE_ID) == first_route_lease
    assert registry.route_reference_counts(FIRST_WORKSPACE_ID) == (1, 0)
    updated = registry.update(FIRST_WORKSPACE_ID, name="仍可修改")
    assert updated.name == "仍可修改" and registry.registry_revision == revision + 1


@pytest.mark.parametrize("workspace_id", [
    "/private/lookup-marker/workspace", r"C:\private\lookup-marker\workspace",
    r"\\server\private\lookup-marker\workspace", "first-target",
    "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA",
], ids=["posix", "drive", "unc", "old-alias", "noncanonical-uuid"])
@pytest.mark.parametrize("operation", [operation for operation, _ in WORKSPACE_LOOKUP_ERRORS])
def test_malformed_workspace_id_preserves_diagnostics_and_committed_registry(
    registry_case: tuple[GatewayWorkspaceRegistry, GatewayStateStore],
    workspace_actions: dict[str, Callable[[str], object]], first_route_lease: WorkspaceRouteLease,
    workspace_id: str, operation: str, caplog: pytest.LogCaptureFixture,
) -> None:
    registry, state = registry_case
    original = state.load_workspace_registry()
    targets = [asdict(target) for target in registry.targets()]
    revision, active = registry.registry_revision, registry.active_workspace
    with pytest.raises(RuntimeError, match="标准 UUID") as caught:
        workspace_actions[operation](workspace_id)
    assert type(caught.value) is RuntimeError
    assert workspace_id not in str(caught.value)
    cause = caught.value.__cause__
    assert cause is not None and type(cause) is ValueError
    assert any(repr(workspace_id) in note for note in cause.__notes__)
    assert any(record.exc_info is not None and record.exc_info[1] is cause for record in caplog.records)
    assert state.load_workspace_registry() == original
    assert [asdict(target) for target in registry.targets()] == targets
    assert (registry.registry_revision, registry.active_workspace) == (revision, active)
    assert registry.resolve() is registry.resolve(FIRST_WORKSPACE_ID)
    assert not first_route_lease.invalidated.is_set()
    assert registry.route_lease(FIRST_WORKSPACE_ID) == first_route_lease
    assert registry.route_reference_counts(FIRST_WORKSPACE_ID) == (1, 0)
    updated = registry.update(FIRST_WORKSPACE_ID, name="仍可修改")
    assert updated.name == "仍可修改" and registry.registry_revision == revision + 1


@pytest.mark.parametrize("workspace_id,error_type,cause_type", [
    (UNKNOWN_WORKSPACE_ID, LookupError, LookupError),
    ("/private/lookup-marker/workspace", RuntimeError, ValueError),
    (r"C:\private\lookup-marker\workspace", RuntimeError, ValueError),
    (r"\\server\private\lookup-marker\workspace", RuntimeError, ValueError),
], ids=["unknown-uuid", "posix", "drive", "unc"])
def test_lookup_failure_survives_diagnostic_sink_failure(
    registry_case: tuple[GatewayWorkspaceRegistry, GatewayStateStore], first_route_lease: WorkspaceRouteLease,
    monkeypatch: pytest.MonkeyPatch, workspace_id: str,
    error_type: type[Exception], cause_type: type[Exception],
) -> None:
    registry, state = registry_case
    original = state.load_workspace_registry()
    targets = [asdict(target) for target in registry.targets()]
    revision, active = registry.registry_revision, registry.active_workspace
    sink_error = OSError("/private/sink-marker/log")

    def reject_log(*_: object, **__: object) -> None:
        raise sink_error

    monkeypatch.setattr(failures.logger, "error", reject_log)
    with pytest.raises(error_type) as caught:
        registry.resolve(workspace_id)
    assert type(caught.value) is error_type
    assert workspace_id not in str(caught.value) and "sink-marker" not in str(caught.value)
    assert "diagnostic_sink_error=OSError" in str(caught.value)
    cause = caught.value.__cause__
    assert cause is not None and type(cause) is cause_type
    assert repr(workspace_id) in "\n".join([str(cause), *cause.__notes__])
    assert any("sink-marker" in note for note in cause.__notes__)
    assert state.load_workspace_registry() == original
    assert [asdict(target) for target in registry.targets()] == targets
    assert (registry.registry_revision, registry.active_workspace) == (revision, active)
    assert not first_route_lease.invalidated.is_set()
    assert registry.route_lease(FIRST_WORKSPACE_ID) == first_route_lease
    assert registry.route_reference_counts(FIRST_WORKSPACE_ID) == (1, 0)


@pytest.mark.parametrize("native_path", [
    "/private/remote-lookup-marker/connection", r"C:\private\remote-lookup-marker\connection",
    r"\\server\private\remote-lookup-marker\connection",
], ids=["posix", "drive", "unc"])
@pytest.mark.parametrize("operation", ["connection", "url"])
def test_remote_lookup_preserves_committed_state_and_can_resolve_known_connection(
    registry_case: tuple[GatewayWorkspaceRegistry, GatewayStateStore], native_path: str,
    first_route_lease: WorkspaceRouteLease, operation: str, caplog: pytest.LogCaptureFixture,
) -> None:
    registry, state = registry_case
    connection = RemoteGatewayConnection(
        connection_id="rgw_known", name="已登记远端", host="remote.test", port=22,
        username="developer", private_key_path=None, ssh_config_host=None,
        remote_gateway_port=8014, remote_gateway_id=REMOTE_GATEWAY_ID, protocol_version=FEDERATION_PROTOCOL_VERSION,
        source_owner="manual",
    )
    registry.upsert_remote_gateway(connection)
    original = state.load_workspace_registry()
    targets = [asdict(target) for target in registry.targets()]
    revision, active = registry.registry_revision, registry.active_workspace
    lookup = registry.remote_gateway_connection if operation == "connection" else registry.remote_gateway_url
    with pytest.raises(LookupError) as caught:
        lookup(native_path)
    assert type(caught.value) is LookupError
    assert "remote-lookup-marker" not in str(caught.value)
    assert "stage=gateway-remote-" in str(caught.value)
    cause = caught.value.__cause__
    assert cause is not None and type(cause) is LookupError
    assert native_path in cause.args[0]
    assert any(record.exc_info and record.exc_info[1] is cause for record in caplog.records)
    assert state.load_workspace_registry() == original
    assert [asdict(target) for target in registry.targets()] == targets
    assert (registry.registry_revision, registry.active_workspace) == (revision, active)
    assert not first_route_lease.invalidated.is_set()
    assert registry.route_lease(FIRST_WORKSPACE_ID) == first_route_lease
    assert registry.route_reference_counts(FIRST_WORKSPACE_ID) == (1, 0)
    assert registry.remote_gateway_connection(connection.connection_id) is connection
    with pytest.raises(LookupError, match="远程 Gateway 隧道尚未连接"):
        registry.remote_gateway_url(connection.connection_id)
    assert state.load_workspace_registry() == original
    runtime = WorkspaceRuntime(service_urls={"workspace_api": "http://remote.test:8014"})
    registry.upsert_remote_gateway(connection, runtime=runtime)
    assert registry.remote_gateway_connection(connection.connection_id) is connection
    assert registry.remote_gateway_url(connection.connection_id) == runtime.service_urls["workspace_api"]
