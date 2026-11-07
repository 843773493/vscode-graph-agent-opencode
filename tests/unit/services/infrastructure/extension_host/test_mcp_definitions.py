"""经原宿主 owner 核 Provider 代际、库存、取消和迟到结果；进程边界沿原 fixture。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest

from app.schemas.gateway.extension_installation import (
    InstalledExtension,
    ProfileExtensionInventory,
)
from app.schemas.gateway.mcp_definitions import (
    ExtensionMcpDefinitionChannelRequest,
    McpProviderCancelDTO,
    McpProviderDefinitionDTO,
    McpProviderInvalidateDTO,
    McpProviderResolvedDTO,
    McpProviderSnapshotDTO,
    McpProviderStdioLaunchDTO,
)
from app.schemas.internal_v2.mcp import McpProviderDefinitionRefDTO
from app.services.infrastructure.extension_host.management import (
    ExtensionManagementClient,
)
from app.services.infrastructure.extension_host.owner import NodeHostConnection
from app.services.infrastructure.mcp.provider import McpProviderUnavailable
from tests.unit.services.infrastructure.extension_host.test_owner import Case
from tests.unit.services.infrastructure.extension_host.test_owner import (
    case as case,  # noqa: PLC0414
)


@dataclass
class ProviderCase:
    host: Case
    snapshot: McpProviderSnapshotDTO
    inventory: ProfileExtensionInventory

    def request(self, connection: NodeHostConnection) -> ExtensionMcpDefinitionChannelRequest:
        identity = connection.identity
        return ExtensionMcpDefinitionChannelRequest(protocol_version=1, gateway_id=identity.gateway_id,
            backend_id=identity.backend_id, view_id=identity.view_id, generation=identity.generation,
            workspace_ids=list(identity.workspace_ids), host_kind="node")

    def reference(self, connection: NodeHostConnection) -> McpProviderDefinitionRefDTO:
        identity, snapshot = connection.identity, self.snapshot
        return McpProviderDefinitionRefDTO(gateway_id=identity.gateway_id, backend_id=identity.backend_id,
            client_id=identity.client_id, view_id=identity.view_id, host_generation=identity.generation,
            host_kind="node", extension_id=snapshot.extension_id, extension_key=snapshot.extension_key,
            collection_id=snapshot.collection_id, provider_ref=snapshot.provider_ref,
            definition_ref=snapshot.definitions[0].definition_ref)


@pytest.fixture
def provider_case(case: Case, monkeypatch: pytest.MonkeyPatch) -> ProviderCase:
    result = ProviderCase(case, McpProviderSnapshotDTO(extension_id="probe.dynamic", extension_key="probe.dynamic-1.0.0",
        collection_id="probe.dynamic/definitions", provider_ref="provider-original",
        definitions=[McpProviderDefinitionDTO(definition_ref="object-original", label="same", version="same", transport="stdio")]),
        ProfileExtensionInventory(profile_id="test-profile", target_platform="linux-x64", extensions=[
            InstalledExtension(source="profile", extension_key="probe.dynamic-1.0.0", is_valid=True, validations=[],
                description={"identifier": {"value": "probe.dynamic"},
                    "contributes": {"mcpServerDefinitionProviders": [{"id": "definitions", "label": "Probe"}]}})]))

    async def inventory(_management: ExtensionManagementClient) -> ProfileExtensionInventory:
        return result.inventory

    monkeypatch.setattr(ExtensionManagementClient, "inventory", inventory)
    return result


@pytest.fixture
async def provider_host(provider_case: ProviderCase):
    case = provider_case.host
    async with case.owner.connect(gateway_id="gateway_" + "a" * 32, expected_backend_id=case.owner.backend_id,
            client_id="original-client", view_id="original-view", workspace_ids=case.workspace_ids) as connection:
        yield connection


@pytest.mark.parametrize("field,value", [("gateway_id", "gateway_" + "b" * 32),
    ("backend_id", "00000000-0000-4000-8000-000000000009"), ("view_id", "foreign"),
    ("generation", "old-generation"), ("host_kind", "web"), ("workspace_ids", [])])
async def test_control_channel_requires_original_identity_and_ordered_roots(provider_case, provider_host, field, value) -> None:
    request = provider_case.request(provider_host).model_copy(update={field: value})
    with pytest.raises(McpProviderUnavailable):
        async with provider_case.host.owner.mcp_definition_channel(request, client_id="original-client"):
            pytest.fail("错误身份不能创建控制通道")
    assert provider_case.host.owner.active == (provider_host.identity,)
    assert len(provider_case.host.hosts) == 1


async def test_original_channel_cannot_be_overwritten_or_borrowed_by_another_client(provider_case, provider_host) -> None:
    owner, request = provider_case.host.owner, provider_case.request(provider_host)
    async with owner.mcp_definition_channel(request, client_id="original-client") as (channel, _):
        with pytest.raises(McpProviderUnavailable):
            async with owner.mcp_definition_channel(request, client_id="another-client"):
                pytest.fail("不能借另一窗口身份")
        with pytest.raises(ValueError, match="已有"):
            async with owner.mcp_definition_channel(request, client_id="original-client"):
                pytest.fail("不能覆盖原通道")
        await owner.validate_mcp_snapshot(request, provider_case.snapshot, client_id="original-client")
        channel.receive(provider_case.snapshot)
        reference = provider_case.reference(provider_host)
        owner.check_mcp_definition(reference, workspace_id=provider_host.identity.workspace_ids[0], transport="stdio")
        for field, value in [("client_id", "another-client"), ("extension_key", "probe.dynamic-2.0.0"),
                ("provider_ref", "another-provider"), ("definition_ref", "same-label-is-not-an-object"),
                ("collection_id", "probe.dynamic/foreign")]:
            with pytest.raises(McpProviderUnavailable):
                owner.check_mcp_definition(reference.model_copy(update={field: value}),
                    workspace_id=provider_host.identity.workspace_ids[0], transport="stdio")
        with pytest.raises(McpProviderUnavailable):
            owner.check_mcp_definition(reference, workspace_id="foreign-workspace", transport="stdio")
        with pytest.raises(McpProviderUnavailable):
            owner.check_mcp_definition(reference, workspace_id=provider_host.identity.workspace_ids[0], transport="streamable_http")


@pytest.mark.parametrize("boundary", ["caller_cancel", "change", "dispose", "host_close"])
async def test_pending_resolve_is_cancelled_and_late_launch_never_revives(provider_case, provider_host, boundary) -> None:
    owner, request = provider_case.host.owner, provider_case.request(provider_host)
    async with owner.mcp_definition_channel(request, client_id="original-client") as (channel, _):
        channel.receive(provider_case.snapshot)
        reference = provider_case.reference(provider_host)
        pending = asyncio.create_task(owner.resolve_mcp_definition(reference,
            workspace_id=provider_host.identity.workspace_ids[0], transport="stdio"))
        resolution = await channel.outbound.get()
        assert resolution.kind == "resolve"
        if boundary == "caller_cancel":
            pending.cancel()
        elif boundary in {"change", "dispose"}:
            channel.receive(McpProviderInvalidateDTO(provider_ref=reference.provider_ref))
        else:
            owner.close_admission()
        with pytest.raises((asyncio.CancelledError, McpProviderUnavailable)):
            await pending
        cancellation = await channel.outbound.get()
        assert isinstance(cancellation, McpProviderCancelDTO) and cancellation.request_id == resolution.request_id
        launch = McpProviderStdioLaunchDTO(transport="stdio", command="must-not-launch", args=[],
            env={"TOKEN": "late-private-value"}, unset_env=[])
        if boundary != "host_close":
            channel.receive(McpProviderResolvedDTO(request_id=resolution.request_id, launch=launch))
        assert all("late-private-value" not in repr(snapshot) for snapshot in channel.snapshots.values())
        if boundary == "caller_cancel":
            # 后续同原对象仍需新的 resolve；不能借已取消请求的迟到秘密。
            next_pending = asyncio.create_task(owner.resolve_mcp_definition(reference,
                workspace_id=provider_host.identity.workspace_ids[0], transport="stdio"))
            next_request = await channel.outbound.get()
            assert next_request.request_id != resolution.request_id and not next_pending.done()
            channel.receive(McpProviderResolvedDTO(request_id=next_request.request_id, error_code="resolve_undefined"))
            with pytest.raises(McpProviderUnavailable, match="resolve_undefined"):
                await next_pending
        else:
            with pytest.raises(McpProviderUnavailable):
                owner.check_mcp_definition(reference, workspace_id=provider_host.identity.workspace_ids[0], transport="stdio")


async def test_inventory_rechecked_after_resolve_and_snapshot_fences_prepared_admission(provider_case, provider_host) -> None:
    owner, request = provider_case.host.owner, provider_case.request(provider_host)
    async with owner.mcp_definition_channel(request, client_id="original-client") as (channel, _):
        await owner.validate_mcp_snapshot(request, provider_case.snapshot, client_id="original-client")
        channel.receive(provider_case.snapshot)
        reference = provider_case.reference(provider_host)
        launch = McpProviderStdioLaunchDTO(transport="stdio", command="original-runtime", args=[], env={"TOKEN": "memory-only"}, unset_env=[])

        async def resolve():
            task = asyncio.create_task(owner.resolve_mcp_definition(reference,
                workspace_id=provider_host.identity.workspace_ids[0], transport="stdio"))
            message = await channel.outbound.get()
            channel.receive(McpProviderResolvedDTO(request_id=message.request_id, launch=launch))
            return await task

        admission = await resolve()
        assert admission.launch is launch and "memory-only" not in repr(admission)
        assert not admission.invalidated.is_set() and not admission.committed.is_set()
        channel.receive(provider_case.snapshot)
        assert admission.invalidated.is_set()
        with pytest.raises(McpProviderUnavailable):
            admission.check()
        # resolve 返回前卸载 VSIX，即使旧 launch 已收到也不能准入。
        pending = asyncio.create_task(owner.resolve_mcp_definition(reference,
            workspace_id=provider_host.identity.workspace_ids[0], transport="stdio"))
        message = await channel.outbound.get()
        provider_case.inventory = provider_case.inventory.model_copy(update={"extensions": []})
        channel.receive(McpProviderResolvedDTO(request_id=message.request_id, launch=launch))
        with pytest.raises(McpProviderUnavailable, match="安装实例"):
            await pending
