"""Web 资格沿原宿主 owner 冻结库存；管理扫描为显式边界替身。"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass, field
from uuid import uuid4

import pytest

from app.schemas.gateway.extension_installation import (
    InstalledExtension,
    NativeExtensionFileRequest,
    ProfileExtensionInventory,
)
from app.schemas.gateway.extension_web import ExtensionWebHostOpenRequest
from app.services.infrastructure.extension_host.management import (
    ExtensionManagementClient,
)
from app.services.infrastructure.extension_host.owner import WebHostConnection
from tests.unit.services.infrastructure.extension_host.test_owner import (
    Case,
)
from tests.unit.services.infrastructure.extension_host.test_owner import (
    case as case,  # noqa: PLC0414
)

GATEWAY = "gateway_" + "a" * 32
KEY = "publisher.probe-1.0.0-linux-x64"
NEXT_KEY = "publisher.probe-2.0.0-linux-x64"
OTHER_KEY = "publisher.other-1.0.0"
INVALID_KEY = "publisher.broken-1.0.0"


def _entry(key: str, *, valid: bool = True) -> InstalledExtension:
    return InstalledExtension(source="profile", extension_key=key,
        description={"browser": "dist/main.js"}, is_valid=valid, validations=[])


@dataclass
class WebCase:
    node: Case
    request: ExtensionWebHostOpenRequest
    inventory: ProfileExtensionInventory
    scans: list[int] = field(default_factory=list)
    scanning: asyncio.Event = field(default_factory=asyncio.Event)
    gate: asyncio.Event = field(default_factory=asyncio.Event)


@pytest.fixture
def web_case(case: Case, monkeypatch: pytest.MonkeyPatch) -> WebCase:
    result = WebCase(case, ExtensionWebHostOpenRequest(protocol_version=1, gateway_id=GATEWAY,
        backend_id=case.owner.backend_id, view_id="window-one", workspace_ids=[],
        profile_id="test-profile", extension_keys=[KEY]),
        ProfileExtensionInventory(profile_id="test-profile", extensions=[
            _entry(KEY), _entry(OTHER_KEY), _entry(INVALID_KEY, valid=False)]))
    result.gate.set()

    async def inventory(_management: ExtensionManagementClient) -> ProfileExtensionInventory:
        result.scans.append(len(case.owner.active))
        result.scanning.set()
        await result.gate.wait()
        return result.inventory

    monkeypatch.setattr(ExtensionManagementClient, "inventory", inventory)
    return result


def _file(connection: WebHostConnection, *, key: str = KEY) -> NativeExtensionFileRequest:
    identity = connection.identity
    return NativeExtensionFileRequest(protocol_version=1, gateway_id=identity.gateway_id,
        backend_id=identity.backend_id, profile_id=connection.inventory.profile_id,
        extension_key=key, extension_id="publisher.probe", relative_segments=["dist", "main.js"],
        current_profile_member=False, generation=identity.generation, view_id=identity.view_id)


async def test_web_freezes_only_requested_valid_exact_keys_without_workspace_or_node_host(
    web_case: WebCase, monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = web_case.node.owner
    monkeypatch.setattr(sys, "platform", "darwin")
    async with owner.connect_web(web_case.request, client_id="client") as connection:
        assert connection.identity.host_kind == "web" and connection.identity.workspace_ids == ()
        assert connection.inventory.extensions == [web_case.inventory.extensions[0]]
        assert web_case.scans == [1]
        owner.check_web_extension_file(_file(connection), client_id="client")
        for key in (OTHER_KEY, INVALID_KEY):
            with pytest.raises(ValueError, match="冻结版本"):
                owner.check_web_extension_file(_file(connection, key=key), client_id="client")
        assert web_case.node.hosts == [] and not web_case.node.views.exists()
        assert not (owner._home / "state/backend/extension-hosts").exists()
    assert owner.active == ()


@pytest.mark.parametrize("change", ["backend", "workspace", "profile", "missing", "invalid", "uninstalled"])
async def test_web_rejects_noncurrent_or_invalid_members_and_releases_reservation(web_case: WebCase, change: str) -> None:
    payload = web_case.request.model_dump()
    if change == "backend":
        payload["backend_id"] = str(uuid4())
    elif change == "workspace":
        payload["workspace_ids"] = [str(uuid4())]
    elif change == "profile":
        payload["profile_id"] = "foreign-profile"
    elif change == "missing":
        payload["extension_keys"] = [NEXT_KEY]
    elif change == "invalid":
        payload["extension_keys"] = [INVALID_KEY]
    else:
        web_case.inventory = ProfileExtensionInventory(profile_id="test-profile", extensions=[])
    with pytest.raises((ValueError, KeyError)):
        async with web_case.node.owner.connect_web(ExtensionWebHostOpenRequest.model_validate(payload), client_id="client"):
            pytest.fail("不合法安装成员不能取得运行资格")
    assert web_case.node.owner.active == () and web_case.node.hosts == []
    if change in {"backend", "workspace"}:
        assert web_case.scans == []


async def test_node_and_web_can_share_view_without_overwriting_the_same_kind(web_case: WebCase) -> None:
    owner, request = web_case.node.owner, web_case.request
    async with owner.connect(gateway_id=GATEWAY, expected_backend_id=owner.backend_id, client_id="client",
            view_id=request.view_id, workspace_ids=web_case.node.workspace_ids) as node:
        async with owner.connect_web(request, client_id="client") as web:
            assert owner.active == (node.identity, web.identity)
            assert node.identity.host_kind == "node"
            with pytest.raises(ValueError, match="尚未关闭"):
                async with owner.connect_web(request, client_id="client"):
                    pytest.fail("同类视图不能覆盖原代际")
        assert owner.active == (node.identity,)
    assert owner.active == ()


async def test_web_instances_keep_independent_versions_after_update_and_uninstall(web_case: WebCase) -> None:
    owner, request = web_case.node.owner, web_case.request
    async with owner.connect_web(request, client_id="first") as first:
        web_case.inventory = ProfileExtensionInventory(profile_id="test-profile", extensions=[_entry(NEXT_KEY)])
        next_request = request.model_copy(update={"extension_keys": [NEXT_KEY]})
        async with owner.connect_web(next_request, client_id="second") as second:
            assert first.identity.generation != second.identity.generation
            web_case.inventory = ProfileExtensionInventory(profile_id="test-profile", extensions=[])
            owner.check_web_extension_file(_file(first), client_id="first")
            owner.check_web_extension_file(_file(second, key=NEXT_KEY), client_id="second")
            for candidate in (request, next_request):
                with pytest.raises(ValueError, match="当前有效"):
                    async with owner.connect_web(candidate, client_id="new"):
                        pytest.fail("卸载后不能从保留目录创建新实例")
        with pytest.raises(ValueError, match="冻结版本"):
            owner.check_web_extension_file(_file(second, key=NEXT_KEY), client_id="second")
        owner.check_web_extension_file(_file(first), client_id="first")
    with pytest.raises(ValueError, match="冻结版本"):
        owner.check_web_extension_file(_file(first), client_id="first")


async def test_reopened_web_view_gets_new_generation_and_only_current_version(web_case: WebCase) -> None:
    owner, request = web_case.node.owner, web_case.request
    async with owner.connect_web(request, client_id="client") as original:
        original_file = _file(original)
    web_case.inventory = ProfileExtensionInventory(profile_id="test-profile", extensions=[_entry(NEXT_KEY)])
    with pytest.raises(ValueError, match="当前有效"):
        async with owner.connect_web(request, client_id="client"):
            pytest.fail("重连不能恢复原旧版")
    async with owner.connect_web(request.model_copy(update={"extension_keys": [NEXT_KEY]}), client_id="client") as current:
        assert current.identity.generation != original.identity.generation
        owner.check_web_extension_file(_file(current, key=NEXT_KEY), client_id="client")
        with pytest.raises(ValueError, match="冻结版本"):
            owner.check_web_extension_file(original_file, client_id="client")


@pytest.mark.parametrize("change", ["client", "gateway", "backend", "view", "generation", "profile", "current"])
async def test_web_resource_requires_complete_original_binding(web_case: WebCase, change: str) -> None:
    owner = web_case.node.owner
    async with owner.connect_web(web_case.request, client_id="client") as connection:
        request = _file(connection)
        changes = {
            "gateway": {"gateway_id": "gateway_" + "b" * 32}, "backend": {"backend_id": str(uuid4())},
            "view": {"view_id": "other-window"}, "generation": {"generation": str(uuid4())},
            "profile": {"profile_id": "other-profile"}, "current": {"current_profile_member": True},
        }.get(change, {})
        with pytest.raises(ValueError, match="冻结版本"):
            owner.check_web_extension_file(request.model_copy(update=changes), client_id="other" if change == "client" else "client")


async def test_web_shutdown_during_scan_cannot_publish_or_leave_reserved_session(web_case: WebCase) -> None:
    web_case.gate.clear()

    async def connect() -> None:
        async with web_case.node.owner.connect_web(web_case.request, client_id="client"):
            pytest.fail("关闭期间不能发布 Web 连接")

    opening = asyncio.create_task(connect())
    try:
        await asyncio.wait_for(web_case.scanning.wait(), 2)
        assert len(web_case.node.owner.active) == 1
        await web_case.node.owner.close()
        web_case.gate.set()
        with pytest.raises(RuntimeError, match="撤销准入"):
            await opening
        assert web_case.node.owner.active == ()
    finally:
        web_case.gate.set()
        await asyncio.gather(opening, return_exceptions=True)


async def test_cancelled_web_scan_releases_reserved_generation(web_case: WebCase) -> None:
    web_case.gate.clear()

    async def connect() -> None:
        async with web_case.node.owner.connect_web(web_case.request, client_id="client"):
            pytest.fail("已取消扫描不能发布 Web 连接")

    opening = asyncio.create_task(connect())
    try:
        await asyncio.wait_for(web_case.scanning.wait(), 2)
        opening.cancel("窗口已关闭")
        with pytest.raises(asyncio.CancelledError, match="窗口已关闭"):
            await opening
        assert web_case.node.owner.active == ()
    finally:
        web_case.gate.set()
        await asyncio.gather(opening, return_exceptions=True)


async def test_web_stop_revokes_reads_before_waiting_for_transport_settlement(web_case: WebCase) -> None:
    owner = web_case.node.owner
    async with owner.connect_web(web_case.request, client_id="client") as connection:
        connection.transport_settled.clear()
        closing = asyncio.create_task(owner.close())
        try:
            await asyncio.wait_for(connection.stopping.wait(), 2)
            assert not closing.done()
            with pytest.raises(ValueError, match="冻结版本"):
                owner.check_web_extension_file(_file(connection), client_id="client")
        finally:
            connection.transport_settled.set()
            await closing
        assert owner.active == ()
