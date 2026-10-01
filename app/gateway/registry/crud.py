from __future__ import annotations

from uuid import uuid4

from app.gateway.runtime.workspace import WorkspaceRuntime
from app.gateway.workspace_ids import (
    build_managed_local_workspace_id,
    build_workspace_id,
    is_legacy_workspace_id,
)
from app.schemas.gateway import GatewayConnectionKind

from .core import _UNSET, WorkspaceTarget


class RegistryCrudMixin:
    def upsert(
        self,
        target: WorkspaceTarget,
        *,
        runtime: WorkspaceRuntime | None = None,
        activate: bool = True,
        mutation_owner: str | None = None,
    ) -> WorkspaceTarget:
        if mutation_owner is not None and mutation_owner not in {
            "config",
            "config_batch",
            "manual",
            "manual_crud",
            "remote_projection",
            "system",
            "registry",
        }:
            raise ValueError(f"Gateway registry mutation owner 非法: {mutation_owner}")
        existing = self._targets.get(target.workspace_id)
        preserve_generation = False
        self._validate_target_identity(target)
        if existing is not None:
            if runtime is not None and self._has_route_references(
                target.workspace_id
            ):
                runtime.close()
                raise RuntimeError(
                    "Gateway 不能替换仍有代理引用的工作区运行时，请先完成排空: "
                    f"workspace_id={target.workspace_id}"
                )
            # TODO: 旧内存夹具和早期注册记录可能只设置 system_default，未同步写入 system owner；仅允许默认目标完成这一次规范化。
            legacy_system_owner_migration = (
                existing.owner == "manual"
                and target.owner == "system"
                and existing.system_default
                and target.system_default
            )
            if target.owner != existing.owner and not legacy_system_owner_migration:
                raise PermissionError(
                    "Gateway registry 不允许通过 upsert 改变 target owner: "
                    f"workspace_id={target.workspace_id}, "
                    f"current={existing.owner}, requested={target.owner}"
                )
            if target.target_generation == "":
                target.target_generation = existing.target_generation
                preserve_generation = True
            if target.runtime_lease_id is None:
                target.runtime_lease_id = existing.runtime_lease_id
        if target.target_generation == "":
            target.target_generation = f"target_generation_{uuid4().hex}"
        route_signature = self._route_signature(target)
        route_changed = (
            target.workspace_id not in self._targets
            or runtime is not None
            or self._route_signatures.get(target.workspace_id) != route_signature
        )
        if route_changed and existing is not None and preserve_generation:
            target.target_generation = f"target_generation_{uuid4().hex}"
        if runtime is not None:
            target.runtime_lease_id = f"runtime_lease_{uuid4().hex}"
        self._targets[target.workspace_id] = target
        if runtime is not None:
            if target.connection_kind == "local" and target.managed:
                target.desired_running = True
            previous = self._runtimes.pop(target.workspace_id, None)
            if previous is not None:
                previous_browser_url = previous.service_urls.get("browser_manager")
                replacement_browser_url = runtime.service_urls.get("browser_manager")
                if (
                    previous_browser_url is not None
                    and previous_browser_url == replacement_browser_url
                ):
                    previous.detach_process("browser_manager")
                previous_terminal_url = previous.service_urls.get("terminal_manager")
                replacement_terminal_url = runtime.service_urls.get("terminal_manager")
                if (
                    previous_terminal_url is not None
                    and previous_terminal_url == replacement_terminal_url
                ):
                    previous.detach_process("terminal_manager")
                previous.close()
            self._runtimes[target.workspace_id] = runtime
        self._route_signatures[target.workspace_id] = route_signature
        if route_changed:
            self.invalidate_route(target.workspace_id)
        if activate or self._active_workspace_id is None:
            self._active_workspace_id = target.workspace_id
        self._save(owner=mutation_owner or target.owner)
        return target
    @staticmethod
    def _validate_target_identity(target: WorkspaceTarget) -> None:
        if target.owner not in {"config", "manual", "system", "remote_projection"}:
            raise ValueError(f"Gateway registry target owner 非法: {target.owner}")
        if not target.target_namespace.strip():
            raise ValueError("Gateway registry target namespace 不能为空")
        if target.connection_id is not None and not target.connection_id.strip():
            raise ValueError("Gateway registry target connection_id 不能为空")
        if target.owner == "remote_projection" and (
            target.connection_id is None
            or target.remote_workspace_id is None
        ):
            raise ValueError(
                "remote_projection target 必须包含 connection_id 和 remote_workspace_id"
            )
        if target.active_request_count < 0 or target.active_stream_count < 0:
            raise ValueError("Gateway registry 活动引用计数不能为负数")
    def remove(self, workspace_id: str, *, owner: str | None = None) -> None:
        target = self._targets.get(workspace_id)
        if target is None:
            raise KeyError(f"未知 Gateway 工作区: {workspace_id}")
        if not target.removable or target.system_default:
            raise PermissionError(f"默认工作区不能删除: {target.name}")
        mutation_owner = owner or target.owner
        scope_owner = self._owner_scope(mutation_owner)
        if scope_owner is not None and scope_owner != target.owner:
            raise PermissionError(
                "Gateway registry 删除不能越过 target owner: "
                f"workspace_id={workspace_id}, current={target.owner}, "
                f"requested={scope_owner}"
            )
        if target.connection_kind == "local":
            self._assert_route_references_drained(workspace_id)
        self.invalidate_route(workspace_id)
        runtime = self._runtimes.pop(workspace_id, None)
        if runtime is not None:
            runtime.close()
        for child in self._targets.values():
            if child.parent_workspace_id == workspace_id:
                child.parent_workspace_id = None
        del self._targets[workspace_id]
        self._route_signatures.pop(workspace_id, None)
        self._close_unused_remote_gateway(target.remote_gateway_connection_id)
        if self._active_workspace_id == workspace_id:
            self._active_workspace_id = self._default_workspace_id()
        self._save(owner=mutation_owner)
    def remove_system_default_aliases(self, *, keep_workspace_id: str) -> None:
        for workspace_id, target in self._targets.items():
            if (
                workspace_id != keep_workspace_id
                and target.system_default
                and target.connection_kind == "local"
            ):
                self._assert_route_references_drained(workspace_id)
        changed = False
        for workspace_id, target in list(self._targets.items()):
            if workspace_id == keep_workspace_id or not target.system_default:
                continue
            runtime = self._runtimes.pop(workspace_id, None)
            if runtime is not None:
                runtime.close()
            self.invalidate_route(workspace_id)
            del self._targets[workspace_id]
            self._route_signatures.pop(workspace_id, None)
            changed = True
        if self._active_workspace_id not in self._targets:
            self._active_workspace_id = keep_workspace_id
            changed = True
        if changed:
            self._save(owner="system")
    def ensure_default_workspace_first(self) -> None:
        if self._order_customized:
            return
        default_workspace_id = self._default_workspace_id()
        if default_workspace_id is None:
            return
        first_workspace_id = next(iter(self._targets), None)
        if first_workspace_id == default_workspace_id:
            return
        self._targets = {
            default_workspace_id: self._targets[default_workspace_id],
            **{
                workspace_id: target
                for workspace_id, target in self._targets.items()
                if workspace_id != default_workspace_id
            },
        }
        self._save(owner="system")
    def reorder(self, workspace_ids: list[str]) -> None:
        if len(workspace_ids) != len(set(workspace_ids)):
            raise ValueError("Gateway 工作区排序列表包含重复 ID")
        known_workspace_ids = set(self._targets)
        requested_workspace_ids = set(workspace_ids)
        unknown_workspace_ids = sorted(requested_workspace_ids - known_workspace_ids)
        missing_workspace_ids = sorted(known_workspace_ids - requested_workspace_ids)
        if unknown_workspace_ids:
            raise ValueError(f"Gateway 工作区排序包含未知 ID: {', '.join(unknown_workspace_ids)}")
        if missing_workspace_ids:
            raise ValueError(f"Gateway 工作区排序缺少 ID: {', '.join(missing_workspace_ids)}")
        self._targets = {
            workspace_id: self._targets[workspace_id]
            for workspace_id in workspace_ids
        }
        self._order_customized = True
        self._save(owner="manual_crud")
    def activate(self, workspace_id: str) -> None:
        if workspace_id not in self._targets:
            raise KeyError(f"未知 Gateway 工作区: {workspace_id}")
        self._active_workspace_id = workspace_id
        self._save(owner="manual_crud")
    def rename(self, workspace_id: str, name: str) -> WorkspaceTarget:
        return self.update(workspace_id, name=name)
    def set_parent(
        self,
        workspace_id: str,
        parent_workspace_id: str | None,
    ) -> WorkspaceTarget:
        return self.update(
            workspace_id,
            parent_workspace_id=parent_workspace_id,
        )
    def update(
        self,
        workspace_id: str,
        *,
        name: str | object = _UNSET,
        parent_workspace_id: str | None | object = _UNSET,
    ) -> WorkspaceTarget:
        target = self._targets.get(workspace_id)
        if target is None:
            raise KeyError(f"未知 Gateway 工作区: {workspace_id}")
        if target.owner not in {"manual", "system"} or (
            target.owner == "system" and not target.system_default
        ):
            raise PermissionError(
                "Gateway manual CRUD 只能修改 manual target 或 system default target: "
                f"workspace_id={workspace_id}, owner={target.owner}"
            )

        normalized_name: str | None = None
        if name is not _UNSET:
            if not isinstance(name, str):
                raise TypeError("Gateway 工作区名称必须是字符串")
            normalized_name = name.strip()
            if not normalized_name:
                raise ValueError("Gateway 工作区名称不能为空")

        normalized_parent_workspace_id: str | None = None
        if parent_workspace_id is not _UNSET:
            if parent_workspace_id is not None and not isinstance(
                parent_workspace_id,
                str,
            ):
                raise TypeError("Gateway 父工作区 ID 必须是字符串或 null")
            normalized_parent_workspace_id = parent_workspace_id
            if normalized_parent_workspace_id == workspace_id:
                raise ValueError("工作区不能成为自己的父工作区")
            if (
                normalized_parent_workspace_id is not None
                and normalized_parent_workspace_id not in self._targets
            ):
                raise KeyError(
                    f"未知 Gateway 父工作区: {normalized_parent_workspace_id}"
                )
            ancestor_id = normalized_parent_workspace_id
            while ancestor_id is not None:
                if ancestor_id == workspace_id:
                    raise ValueError("工作区父子关系不能形成循环")
                ancestor_id = self._targets[ancestor_id].parent_workspace_id

        if normalized_name is not None:
            target.name = normalized_name
            target.name_customized = True
        if parent_workspace_id is not _UNSET:
            target.parent_workspace_id = normalized_parent_workspace_id
        self._save(owner="manual_crud")
        return target
    def resolve(self, workspace_id: str | None = None) -> WorkspaceTarget:
        target_id = workspace_id or self._active_workspace_id
        if target_id is None:
            raise LookupError("Gateway 尚未注册任何工作区")
        target = self._targets.get(target_id)
        if target is None:
            raise LookupError(f"Gateway 工作区不存在: {target_id}")
        return target
    @staticmethod
    def _owner_scope(owner: str) -> str | None:
        return {
            "config": "config",
            "config_batch": "config",
            "manual": "manual",
            "manual_crud": "manual",
            "system": "system",
            "remote_projection": "remote_projection",
        }.get(owner)
    def _default_workspace_id(self) -> str | None:
        for target in self._targets.values():
            if target.system_default:
                return target.workspace_id
        return next(iter(self._targets), None)
    @staticmethod
    def _migrate_legacy_workspace_id(
        *,
        workspace_id: str,
        root_path: str,
        backend_url: str,
        connection_kind: GatewayConnectionKind,
        managed: bool,
        system_default: bool,
        schema_version: int,
    ) -> str:
        if (
            schema_version < 6
            and connection_kind == "local"
            and (managed or system_default)
        ):
            return build_managed_local_workspace_id(root_path)
        if not is_legacy_workspace_id(workspace_id):
            return workspace_id
        return build_workspace_id(
            connection_kind,
            root_path,
            backend_url,
        )
    def _validate_parent_graph(self) -> None:
        for workspace_id in self._targets:
            visited: set[str] = set()
            current_id: str | None = workspace_id
            while current_id is not None:
                if current_id in visited:
                    raise ValueError(
                        "Gateway registry 工作区父子关系形成循环: "
                        f"workspace_id={workspace_id}"
                    )
                visited.add(current_id)
                current_id = self._targets[current_id].parent_workspace_id
