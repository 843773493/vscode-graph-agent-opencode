from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from app.core.backend_identity import validate_backend_id
from app.core.path_utils import get_gateway_root
from app.core.workspace_identity import validate_workspace_id
from app.gateway.auth import LOCAL_TOKEN
from app.gateway.credentials import FederationCredentialStore
from app.gateway.proxy_upstream import TARGET_GATEWAY_ID_HEADER
from app.gateway.runtime.managed_backend import ManagedBackendOwner
from app.gateway.service_types import GatewayServiceName

from .core import WorkspaceRouteLease, WorkspaceSelector, WorkspaceTarget

if TYPE_CHECKING:
    from . import GatewayWorkspaceRegistry


@dataclass(frozen=True, slots=True)
class BackendManagementRoute:
    """同一已登记进程组的短请求路由；稳定安装域仍由目标 Backend 回执确认。"""

    base_url: str
    preferred: bool
    leases: tuple[WorkspaceRouteLease, ...]
    backend_id: str | None
    owner: ManagedBackendOwner | None = None
    target: WorkspaceTarget | None = None


class RegistryRoutesMixin:
    def remote_gateway_route_identity(self, connection_id: str) -> tuple[object, object]:
        registry = cast("GatewayWorkspaceRegistry", self)
        connection = registry.remote_gateway_connection(connection_id)
        runtime = registry._remote_gateway_runtimes.get(connection_id)
        if runtime is None:
            raise LookupError("远端 Gateway 管理隧道尚未连接")
        return connection, runtime

    @contextmanager
    def backend_management_routes(self, *, backend_id: str | None = None,
                                  workspace_ids: tuple[str, ...] = ()) -> Iterator[tuple[BackendManagementRoute, ...]]:
        """从原 owner 合同取得管理入口，不借用活动工作区或按 URL 合并身份。"""
        registry = cast("GatewayWorkspaceRegistry", self)
        owners = registry.backend_owners()
        owned_members = {member_id for owner in owners for member_id in owner.contract.workspace_ids}
        candidates: list[tuple[ManagedBackendOwner | None, tuple[WorkspaceTarget, ...], str | None, str]] = []
        for owner in owners:
            if backend_id is not None and owner.backend_id != backend_id:
                continue
            if not set(workspace_ids).issubset(owner.contract.workspace_ids):
                continue
            candidates.append((owner, tuple(registry.resolve(member_id) for member_id in owner.contract.workspace_ids),
                owner.backend_id, owner.contract.backend_url))
        for target in registry.targets():
            if (target.gateway_id != registry.gateway_id or target.connection_kind != "local"
                or target.workspace_id in owned_members or backend_id is not None and target.backend_id != backend_id):
                continue
            if workspace_ids and not set(workspace_ids).issubset((target.workspace_id,)):
                continue
            candidates.append((None, (target,), target.backend_id, target.backend_url))
        if backend_id is not None and len(candidates) > 1:
            preferred = [candidate for candidate in candidates if any(member.system_default for member in candidate[1])]
            if len(preferred) != 1:
                raise ValueError("同一 Backend 安装域未明确唯一管理路由或来源上下文")
            candidates = preferred
        if backend_id is not None and not candidates:
            raise LookupError("原 Gateway 未登记请求的稳定 Backend 安装域或来源上下文；请先显式发现目标")
        with ExitStack() as references:
            routes = []
            for owner, members, confirmed_id, base_url in candidates:
                if owner is not None:
                    if owner.lock.locked():
                        raise LookupError("Backend 完整组正在切换管理路由")
                    owner.assert_healthy()
                elif members[0].managed:
                    raise RuntimeError("受管 Backend 缺少唯一原 registry owner")
                if any(member.connection_error is not None for member in members) or not base_url:
                    raise LookupError("所选 Backend 管理路由不可用")
                leases = []
                for member in members:
                    leases.append(registry.acquire_route_reference(member.key, streaming=False))
                    references.callback(registry.release_route_reference, member.key, streaming=False)
                routes.append(BackendManagementRoute(base_url, any(member.system_default for member in members),
                    tuple(leases), confirmed_id, owner, None if owner is not None else members[0]))
            yield tuple(routes)

    def confirm_backend_management_identity(self, route: BackendManagementRoute, backend_id: object) -> None:
        """仅在已认证发现后填原 owner 的运行投影；身份冲突不能改绑。"""
        registry = cast("GatewayWorkspaceRegistry", self)
        confirmed_id = validate_backend_id(backend_id)
        if any(lease.invalidated.is_set() for lease in route.leases):
            raise LookupError("Backend 管理路由在身份确认前已失效")
        if route.owner is not None:
            if any(registry.backend_owner((lease.gateway_id, lease.workspace_id)) is not route.owner for lease in route.leases):
                raise LookupError("Backend 原 owner 在身份确认前已替换")
            previous = route.owner.backend_id
            if previous is not None and previous != confirmed_id:
                raise ValueError("Backend 安装域身份与原 owner 不匹配")
            route.owner.backend_id = confirmed_id
        elif route.target is not None:
            if registry.resolve(route.target.key) is not route.target:
                raise LookupError("外部 Backend 登记在身份确认前已替换")
            previous = route.target.backend_id
            if previous is not None and previous != confirmed_id:
                raise ValueError("Backend 安装域身份与原登记不匹配")
            route.target.backend_id = confirmed_id

    def acquire_remote_gateway_reference(self, connection_id: str) -> str:
        """冻结管理请求的当前隧道，换代后仍持有原运行时直到请求结束。"""
        registry = cast("GatewayWorkspaceRegistry", self)
        connection = registry.remote_gateway_connection(connection_id)
        if connection.connection_error is not None:
            raise LookupError(f"远程 Gateway 连接不可用: {connection_id}")
        gateway_url = registry.remote_gateway_url(connection_id)
        registry._remote_gateway_runtimes[connection_id].assert_healthy()
        registry._remote_gateway_reference_counts[connection_id] = (
            registry._remote_gateway_reference_counts.get(connection_id, 0) + 1
        )
        return gateway_url

    def release_remote_gateway_reference(self, connection_id: str) -> None:
        """只收尾本请求引用及 retired 隧道，保留无工作区的合法连接。"""
        registry = cast("GatewayWorkspaceRegistry", self)
        count = registry._remote_gateway_reference_counts.get(connection_id, 0)
        if count == 0:
            raise RuntimeError(f"Gateway 远程管理引用重复释放: {connection_id}")
        if count > 1:
            registry._remote_gateway_reference_counts[connection_id] = count - 1
        else:
            registry._remote_gateway_reference_counts.pop(connection_id)
            registry._close_retired_remote_gateway_runtimes(connection_id)

    @staticmethod
    def _route_signature(target: WorkspaceTarget) -> tuple[object, ...]:
        return (
            target.connection_kind,
            target.backend_url.rstrip("/"),
            target.key,
            target.remote_gateway_connection_ids,
        )
    def route_lease(self, workspace_id: WorkspaceSelector) -> WorkspaceRouteLease:
        workspace_id = self.workspace_key(workspace_id)
        self.resolve(workspace_id)
        revision = self._route_revisions.get(workspace_id, 0)
        invalidated = self._route_change_events.get(workspace_id)
        if invalidated is None:
            invalidated = asyncio.Event()
            self._route_change_events[workspace_id] = invalidated
        return WorkspaceRouteLease(
            gateway_id=workspace_id[0],
            workspace_id=workspace_id[1],
            revision=revision,
            invalidated=invalidated,
        )
    def acquire_route_reference(
        self,
        workspace_id: WorkspaceSelector,
        *,
        streaming: bool,
    ) -> WorkspaceRouteLease:
        """为代理请求保留路由引用，直到响应体或长连接真正结束。"""

        workspace_id = self.workspace_key(workspace_id)
        target = self.resolve(workspace_id)
        target_connection_id = self.remote_connection_for_target(target).connection_id if target.connection_kind == "remote_gateway" else None
        lease = self.route_lease(workspace_id)
        requests, streams = self._route_reference_counts.get(workspace_id, (0, 0))
        if streaming:
            streams += 1
        else:
            requests += 1
        self._route_reference_counts[workspace_id] = (requests, streams)
        if target_connection_id is not None:
            self._route_reference_connections.setdefault(workspace_id, set()).add(
                target_connection_id
            )
        return lease
    def release_route_reference(
        self,
        workspace_id: WorkspaceSelector,
        *,
        streaming: bool,
    ) -> None:
        """释放代理请求引用；计数不写入 registry 持久化快照。"""

        workspace_id = self.workspace_key(workspace_id)
        requests, streams = self._route_reference_counts.get(workspace_id, (0, 0))
        if streaming:
            if streams == 0:
                raise RuntimeError(f"Gateway 流引用重复释放: {workspace_id}")
            streams -= 1
        else:
            if requests == 0:
                raise RuntimeError(f"Gateway 请求引用重复释放: {workspace_id}")
            requests -= 1
        if requests or streams:
            self._route_reference_counts[workspace_id] = (requests, streams)
        else:
            self._route_reference_counts.pop(workspace_id, None)
            connection_ids = self._route_reference_connections.pop(workspace_id, set())
            for connection_id in connection_ids:
                self._cleanup_remote_gateway_if_unused(connection_id)
    def route_reference_counts(self, workspace_id: WorkspaceSelector) -> tuple[int, int]:
        """返回当前 Gateway 代理持有的普通请求数与长连接数。"""

        workspace_id = self.workspace_key(workspace_id)
        self.resolve(workspace_id)
        return self._route_reference_counts.get(workspace_id, (0, 0))
    def _has_route_references(self, workspace_id: WorkspaceSelector) -> bool:
        workspace_id = self.workspace_key(workspace_id)
        return any(self._route_reference_counts.get(workspace_id, (0, 0)))
    def _assert_route_references_drained(self, workspace_id: WorkspaceSelector) -> None:
        workspace_id = self.workspace_key(workspace_id)
        request_count, stream_count = self._route_reference_counts.get(
            workspace_id,
            (0, 0),
        )
        if request_count or stream_count:
            raise RuntimeError(
                "Gateway 不能关闭仍有代理引用的工作区运行时，请先完成排空: "
                f"workspace_id={workspace_id}, requests={request_count}, "
                f"streams={stream_count}"
            )
    def invalidate_route(self, workspace_id: WorkspaceSelector) -> None:
        """使现有代理租约失效，强制长连接重新解析当前工作区路由。"""
        workspace_id = self.workspace_key(workspace_id)
        self._route_revisions[workspace_id] = (
            self._route_revisions.get(workspace_id, 0) + 1
        )
        previous = self._route_change_events.get(workspace_id)
        if previous is not None:
            previous.set()
        self._route_change_events[workspace_id] = asyncio.Event()
    def resolve_service_url(
        self,
        workspace_id: WorkspaceSelector,
        service: GatewayServiceName,
    ) -> str:
        target = self.resolve(workspace_id)
        if target.connection_kind == "remote_gateway":
            connection_id = self.remote_connection_for_target(target).connection_id
            gateway_url = self.remote_gateway_url(connection_id)
            if service not in target.remote_service_names:
                raise LookupError(
                    f"远程工作区未提供服务: workspace_id={workspace_id}, service={service}"
                )
            if service == "workspace_api":
                return gateway_url
            service_path = (
                "terminal-manager"
                if service == "terminal_manager"
                else "browser-manager"
            )
            return (
                f"{gateway_url}/api/gateway/workspaces/"
                f"{target.workspace_id}/{service_path}"
            )
        if (
            not target.managed
            and service == "workspace_api"
            and target.backend_url
        ):
            # 外部编排的本地后端没有 Gateway runtime，但仍然可以直接通过
            # 持久化的 backend_url 代理工作区 API。
            return target.backend_url
        runtime = self._runtimes.get(target.workspace_id)
        if runtime is None:
            raise LookupError(f"工作区运行时尚未连接: {workspace_id}")
        if target.managed:
            owner = runtime.backend_owner
            if owner is None or self.backend_owner(target.key) is not owner:
                raise RuntimeError("managed target 缺少唯一完整 Backend owner")
            if owner.lock.locked():
                raise LookupError(f"Backend 完整组正在切换路由: workspace_id={workspace_id}")
            owner.assert_healthy()
            if service == "workspace_api":
                return owner.contract.backend_url
        service_url = runtime.service_urls.get(service)
        if service_url is None:
            raise LookupError(
                f"工作区未提供服务: workspace_id={workspace_id}, service={service}"
            )
        return service_url
    def resolve_request_target(self, workspace_id: str | None, gateway_id: str | None = None) -> WorkspaceTarget:
        """请求中的裸 UUID 使用本机语义，目标头只补充明确的原生归属。"""
        target = self.resolve((gateway_id, workspace_id) if gateway_id is not None and workspace_id is not None else workspace_id)
        if gateway_id is not None and target.gateway_id != gateway_id:
            raise ValueError("目标 Gateway 与选定工作区归属冲突")
        return target

    def targets(self) -> tuple[WorkspaceTarget, ...]:
        return tuple(self._targets.values())
    def has_target(self, workspace_id: WorkspaceSelector) -> bool:
        workspace_id = self.workspace_key(workspace_id)
        return workspace_id in self._targets
    def mark_connection_error(self, workspace_id: WorkspaceSelector, error: str) -> None:
        target = self.resolve(workspace_id)
        backend_owner = self.backend_owner(workspace_id)
        if backend_owner is not None:
            self.mark_backend_error(backend_owner, error)
        else:
            target.connection_error = error
            self._save(
                owner=target.owner,
                remote_gateway_connection_ids=target.remote_gateway_connection_ids,
            )
    def target_request_headers(self, target: WorkspaceTarget) -> dict[str, str]:
        """统一查询的定位和凭据；发送方身份与 request_id 由原调用方提供。"""
        validate_workspace_id(target.workspace_id)
        if target.connection_kind == "local":
            if target.gateway_id != self.gateway_id:
                raise ValueError("本机工作区归属不属于当前 Gateway")
            return {
                "X-Local-Token": LOCAL_TOKEN,
                "X-BoxTeam-Workspace-Id": target.workspace_id,
            }
        connection = self.remote_connection_for_target(target)
        credential = FederationCredentialStore(
            storage_path=get_gateway_root() / "credentials" / "federation.json"
        ).get(connection.connection_id)
        return {
            "X-BoxTeam-Federation-Token": credential.token,
            "X-BoxTeam-Workspace-Id": target.workspace_id,
            TARGET_GATEWAY_ID_HEADER: target.gateway_id,
        }
