from __future__ import annotations

import asyncio

from app.core.path_utils import get_gateway_root
from app.gateway.credentials import FederationCredentialStore
from app.gateway.service_types import GatewayServiceName

from .core import WorkspaceRouteLease, WorkspaceTarget


class RegistryRoutesMixin:
    @staticmethod
    def _route_signature(target: WorkspaceTarget) -> tuple[object, ...]:
        return (
            target.connection_kind,
            target.backend_url.rstrip("/"),
            target.remote_gateway_connection_id,
            target.remote_workspace_id,
        )
    def route_lease(self, workspace_id: str) -> WorkspaceRouteLease:
        self.resolve(workspace_id)
        revision = self._route_revisions.get(workspace_id, 0)
        invalidated = self._route_change_events.get(workspace_id)
        if invalidated is None:
            invalidated = asyncio.Event()
            self._route_change_events[workspace_id] = invalidated
        return WorkspaceRouteLease(
            workspace_id=workspace_id,
            revision=revision,
            invalidated=invalidated,
        )
    def acquire_route_reference(
        self,
        workspace_id: str,
        *,
        streaming: bool,
    ) -> WorkspaceRouteLease:
        """为代理请求保留路由引用，直到响应体或长连接真正结束。"""

        lease = self.route_lease(workspace_id)
        requests, streams = self._route_reference_counts.get(workspace_id, (0, 0))
        if streaming:
            streams += 1
        else:
            requests += 1
        self._route_reference_counts[workspace_id] = (requests, streams)
        target_connection_id = self.resolve(workspace_id).remote_gateway_connection_id
        if target_connection_id is not None:
            self._route_reference_connections.setdefault(workspace_id, set()).add(
                target_connection_id
            )
        return lease
    def release_route_reference(
        self,
        workspace_id: str,
        *,
        streaming: bool,
    ) -> None:
        """释放代理请求引用；计数不写入 registry 持久化快照。"""

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
    def route_reference_counts(self, workspace_id: str) -> tuple[int, int]:
        """返回当前 Gateway 代理持有的普通请求数与长连接数。"""

        self.resolve(workspace_id)
        return self._route_reference_counts.get(workspace_id, (0, 0))
    def _has_route_references(self, workspace_id: str) -> bool:
        return any(self._route_reference_counts.get(workspace_id, (0, 0)))
    def _assert_route_references_drained(self, workspace_id: str) -> None:
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
    def invalidate_route(self, workspace_id: str) -> None:
        """使现有代理租约失效，强制长连接重新解析当前工作区路由。"""
        self._route_revisions[workspace_id] = (
            self._route_revisions.get(workspace_id, 0) + 1
        )
        previous = self._route_change_events.get(workspace_id)
        if previous is not None:
            previous.set()
        self._route_change_events[workspace_id] = asyncio.Event()
    def resolve_service_url(
        self,
        workspace_id: str,
        service: GatewayServiceName,
    ) -> str:
        target = self.resolve(workspace_id)
        if target.connection_kind == "remote_gateway":
            connection_id = target.remote_gateway_connection_id
            remote_workspace_id = target.remote_workspace_id
            if connection_id is None or remote_workspace_id is None:
                raise RuntimeError(f"远程投影工作区缺少所属 Gateway 信息: {workspace_id}")
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
                f"{remote_workspace_id}/{service_path}"
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
        service_url = runtime.service_urls.get(service)
        if service_url is None:
            raise LookupError(
                f"工作区未提供服务: workspace_id={workspace_id}, service={service}"
            )
        return service_url
    def targets(self) -> tuple[WorkspaceTarget, ...]:
        return tuple(self._targets.values())
    def has_target(self, workspace_id: str) -> bool:
        return workspace_id in self._targets
    def mark_connection_error(self, workspace_id: str, error: str) -> None:
        target = self.resolve(workspace_id)
        target.connection_error = error
        self._save(owner=target.owner)
    @staticmethod
    def _target_headers(target: WorkspaceTarget) -> dict[str, str]:
        if target.connection_kind != "remote_gateway":
            return {"X-Local-Token": "local-dev-token"}
        connection_id = target.remote_gateway_connection_id
        remote_workspace_id = target.remote_workspace_id
        if connection_id is None or remote_workspace_id is None:
            raise RuntimeError(
                f"远程投影工作区缺少连接信息: {target.workspace_id}"
            )
        credential = FederationCredentialStore(
            storage_path=get_gateway_root() / "credentials" / "federation.json"
        ).get(connection_id)
        return {
            "X-BoxTeam-Federation-Token": credential.token,
            "X-BoxTeam-Workspace-Id": remote_workspace_id,
        }
