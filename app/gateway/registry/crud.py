from __future__ import annotations

from pathlib import Path
from typing import cast
from uuid import uuid4

from app.core.failures import failure_detail
from app.core.workspace_identity import validate_workspace_id
from app.gateway.runtime.workspace import WorkspaceRuntime

from .core import (
    _UNSET,
    WorkspaceIdentityConflictError,
    WorkspaceKey,
    WorkspaceSelector,
    WorkspaceTarget,
)


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
        existing = self._targets.get(target.key)
        backend_owner = self.backend_owner(target.key) if existing is not None else None
        if existing is not None and backend_owner is not None:
            if backend_owner.lock.locked() or self.backend_topology_lock.locked():
                raise RuntimeError("Backend 完整组生命周期操作进行中，不能单独 upsert 成员")
            member = next(member for member in backend_owner.contract.members if member.workspace_id == target.workspace_id)
            if target.root_path != existing.root_path or target.workspace_id != member.workspace_id:
                raise ValueError("Backend 成员身份变更必须经完整组切换")
            if target.connection_kind != "local" or not target.managed or target.backend_url != backend_owner.contract.backend_url or target.desired_running != existing.desired_running or target.local_service_urls != existing.local_service_urls:
                raise ValueError("Backend 成员路由和启动意图只能由完整组操作提交")
            if runtime is not None:
                raise ValueError("managed Backend runtime 替换必须按完整组提交")
        preserve_generation = False
        self._validate_target_identity(target)
        if existing is not None:
            if runtime is not None and self._has_route_references(target.key):
                runtime.close()
                raise RuntimeError(
                    "Gateway 不能替换仍有代理引用的工作区运行时，请先完成排空: "
                    f"workspace_id={target.workspace_id}"
                )
            if target.owner != existing.owner:
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
            target.key not in self._targets
            or runtime is not None
            or self._route_signatures.get(target.key) != route_signature
        )
        if route_changed and existing is not None and preserve_generation:
            target.target_generation = f"target_generation_{uuid4().hex}"
        if runtime is not None:
            target.runtime_lease_id = f"runtime_lease_{uuid4().hex}"
        if runtime is not None and target.connection_kind == "local" and target.managed and runtime.backend_owner is None:
            raise ValueError("managed runtime 必须引用唯一 Backend owner")
        self._targets[target.key] = target
        if runtime is not None:
            if target.connection_kind == "local" and target.managed:
                if runtime.backend_owner is None:
                    raise ValueError("managed runtime 必须引用唯一 Backend owner")
                self.publish_backend_runtime(runtime.backend_owner, runtime.backend_owner.contract, {target.workspace_id: runtime})
                if activate or self._active_workspace is None:
                    self._active_workspace = target.key
                    self._save(owner=mutation_owner or target.owner)
                return self.resolve(target.key)
            previous = self._runtimes.get(target.workspace_id)
            if previous is not None and previous is not runtime:
                previous.close()
            self._runtimes[target.workspace_id] = runtime
        self._route_signatures[target.key] = route_signature
        if route_changed:
            self.invalidate_route(target.key)
        if activate or self._active_workspace is None:
            self._active_workspace = target.key
        self._save(owner=mutation_owner or target.owner)
        return target

    def _validate_local_root_binding(self, key: WorkspaceKey, root: Path) -> None:
        registered = self._targets.get(key)
        if registered is not None and Path(cast(str, registered.root_path)).resolve() != root.resolve():
            raise WorkspaceIdentityConflictError(
                f"工作区 UUID 已绑定另一物理根: workspace_id={key[1]}, "
                f"registered={registered.root_path}, requested={root}"
            )

    def _validate_target_identity(self, target: WorkspaceTarget) -> None:
        if target.connection_kind == "local":
            if not isinstance(target.root_path, str) or not target.root_path or not Path(target.root_path).is_absolute():
                raise ValueError(f"本地 target 缺少绝对 root binding: {target.workspace_id}")
        elif target.root_path is not None:
            raise ValueError(f"远端 projection 不得包含 root binding: {target.workspace_id}")
        if target.owner not in {"config", "manual", "system", "remote_projection"}:
            raise ValueError(f"Gateway registry target owner 非法: {target.owner}")
        if not isinstance(target.target_namespace, str) or not target.target_namespace.strip():
            raise ValueError("Gateway registry target_namespace 必须是非空字符串")
        if target.connection_id is not None and (
            not isinstance(target.connection_id, str) or not target.connection_id.strip()
        ):
            raise ValueError("Gateway registry target connection_id 必须是非空字符串或 null")
        validate_workspace_id(target.workspace_id)
        self.workspace_key(target.key)
        if target.connection_kind == "local":
            if target.gateway_id != self.gateway_id or target.remote_gateway_connection_ids:
                raise ValueError("本机工作区必须属于当前原生 Gateway，不能带远端连接")
            self._validate_local_root_binding(target.key, Path(cast(str, target.root_path)))
        elif target.connection_kind == "remote_gateway":
            if target.gateway_id == self.gateway_id or target.owner != "remote_projection" or not target.remote_gateway_connection_ids:
                raise ValueError("远端工作区必须具有独立 Gateway 归属和连接关联")
            if len(set(target.remote_gateway_connection_ids)) != len(target.remote_gateway_connection_ids):
                raise ValueError("远端工作区包含重复连接关联")
        else:
            raise ValueError(f"未知工作区连接类型: {target.connection_kind}")
        if target.active_request_count < 0 or target.active_stream_count < 0:
            raise ValueError("Gateway registry 活动引用计数不能为负数")

    def validate_target_removal(self, workspace_id: WorkspaceSelector, *, owner: str | None = None) -> WorkspaceTarget:
        workspace_id = self.workspace_key(workspace_id)
        target = self._targets.get(workspace_id)
        if target is None:
            error = KeyError(f"未知 Gateway 工作区: {workspace_id}")
            raise KeyError(failure_detail(error, stage="gateway-workspace-remove", label="未知 Gateway 工作区")) from error
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
        return target

    def remove(self, workspace_id: WorkspaceSelector, *, owner: str | None = None) -> None:
        workspace_id = self.workspace_key(workspace_id)
        target = self.validate_target_removal(workspace_id, owner=owner)
        mutation_owner = owner or target.owner
        scope_owner = self._owner_scope(mutation_owner)
        if target.connection_kind == "local":
            self._assert_route_references_drained(workspace_id)
        if self.backend_owner(workspace_id) is not None:
            raise ValueError("managed Backend 成员删除必须先完成 owner 完整组受控退出")
        runtime = self._runtimes.get(workspace_id[1]) if target.connection_kind == "local" else None
        if runtime is not None:
            if target.connection_kind == "local" and target.managed:
                raise RuntimeError("managed 成员仍有运行时，不能绕过完整组清理")
            runtime.close()
            self._runtimes.pop(workspace_id[1])
        self.invalidate_route(workspace_id)
        for child in self._targets.values():
            if child.parent_workspace == workspace_id:
                child.parent_workspace = None
        del self._targets[workspace_id]
        self._route_signatures.pop(workspace_id, None)
        for connection_id in target.remote_gateway_connection_ids:
            self._close_unused_remote_gateway(connection_id)
        if self._active_workspace == workspace_id:
            self._active_workspace = self._default_workspace_key()
        self._save(
            owner=mutation_owner,
            remote_gateway_connection_ids=target.remote_gateway_connection_ids
            if scope_owner == "remote_projection"
            else (),
        )

    def remove_system_default_aliases(self, *, keep_workspace_id: WorkspaceSelector) -> None:
        keep_workspace_id = self.workspace_key(keep_workspace_id)
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
            backend_owner = self.backend_owner(workspace_id)
            if backend_owner is not None:
                self.discard_stopped_backend_owner(backend_owner)
            runtime = self._runtimes.get(workspace_id[1]) if target.connection_kind == "local" else None
            if runtime is not None:
                runtime.close()
                self._runtimes.pop(workspace_id[1])
            self.invalidate_route(workspace_id)
            del self._targets[workspace_id]
            self._route_signatures.pop(workspace_id, None)
            changed = True
        if self._active_workspace not in self._targets:
            self._active_workspace = keep_workspace_id
            changed = True
        if changed:
            self._save(owner="system")

    def ensure_default_workspace_first(self) -> None:
        if self._order_customized:
            return
        default_workspace_id = self._default_workspace_key()
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

    def reorder(self, workspace_ids: list[WorkspaceKey]) -> None:
        workspace_ids = [self.workspace_key(value) for value in workspace_ids]
        if len(workspace_ids) != len(set(workspace_ids)):
            raise ValueError("Gateway 工作区排序列表包含重复 ID")
        known_workspace_ids = set(self._targets)
        requested_workspace_ids = set(workspace_ids)
        unknown_workspace_ids = sorted(requested_workspace_ids - known_workspace_ids)
        missing_workspace_ids = sorted(known_workspace_ids - requested_workspace_ids)
        if unknown_workspace_ids:
            error = ValueError(f"Gateway 工作区排序包含未知 ID: {', '.join(map(str, unknown_workspace_ids))}")
            raise ValueError(failure_detail(error, stage="gateway-workspace-order", label="Gateway 工作区排序包含未知 ID")) from error
        if missing_workspace_ids:
            raise ValueError(
                f"Gateway 工作区排序缺少 ID: {', '.join(map(str, missing_workspace_ids))}"
            )
        self._targets = {
            workspace_id: self._targets[workspace_id] for workspace_id in workspace_ids
        }
        self._order_customized = True
        self._save(owner="manual_crud")

    def activate(self, workspace_id: WorkspaceSelector) -> None:
        workspace_id = self.workspace_key(workspace_id)
        if workspace_id not in self._targets:
            error = KeyError(f"未知 Gateway 工作区: {workspace_id}")
            raise KeyError(failure_detail(error, stage="gateway-workspace-activate", label="未知 Gateway 工作区")) from error
        self._active_workspace = workspace_id
        self._save(owner="manual_crud")

    def rename(self, workspace_id: WorkspaceSelector, name: str) -> WorkspaceTarget:
        return self.update(workspace_id, name=name)

    def set_parent(
        self,
        workspace_id: WorkspaceSelector,
        parent_workspace: WorkspaceKey | None,
    ) -> WorkspaceTarget:
        return self.update(
            workspace_id,
            parent_workspace=parent_workspace,
        )

    def update(
        self,
        workspace_id: WorkspaceSelector,
        *,
        name: str | object = _UNSET,
        parent_workspace: WorkspaceKey | None | object = _UNSET,
    ) -> WorkspaceTarget:
        workspace_id = self.workspace_key(workspace_id)
        target = self._targets.get(workspace_id)
        if target is None:
            error = KeyError(f"未知 Gateway 工作区: {workspace_id}")
            raise KeyError(failure_detail(error, stage="gateway-workspace-update", label="未知 Gateway 工作区")) from error
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

        normalized_parent_workspace: WorkspaceKey | None = None
        if parent_workspace is not _UNSET:
            if parent_workspace is not None and not isinstance(
                parent_workspace,
                tuple,
            ):
                raise TypeError("Gateway 父工作区必须是完整 owner 或 null")
            normalized_parent_workspace = self.workspace_key(parent_workspace) if parent_workspace is not None else None
            if normalized_parent_workspace == workspace_id:
                raise ValueError("工作区不能成为自己的父工作区")
            if (
                normalized_parent_workspace is not None
                and normalized_parent_workspace not in self._targets
            ):
                error = KeyError(f"未知 Gateway 父工作区: {normalized_parent_workspace}")
                raise KeyError(failure_detail(error, stage="gateway-workspace-parent", label="未知 Gateway 父工作区")) from error
            ancestor_id = normalized_parent_workspace
            while ancestor_id is not None:
                if ancestor_id == workspace_id:
                    raise ValueError("工作区父子关系不能形成循环")
                ancestor_id = self._targets[ancestor_id].parent_workspace

        if normalized_name is not None:
            target.name = normalized_name
            target.name_customized = True
        if parent_workspace is not _UNSET:
            target.parent_workspace = normalized_parent_workspace
        self._save(owner="manual_crud")
        return target

    def resolve(self, workspace_id: WorkspaceSelector | None = None) -> WorkspaceTarget:
        target_key = self._active_workspace if workspace_id is None else self.workspace_key(workspace_id)
        if target_key is None:
            raise LookupError("Gateway 尚未注册任何工作区")
        target = self._targets.get(target_key)
        if target is None:
            error = LookupError(f"Gateway 工作区不存在: {target_key}")
            raise LookupError(failure_detail(error, stage="gateway-workspace-resolve", label="Gateway 工作区不存在")) from error
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

    def _default_workspace_key(self) -> WorkspaceKey | None:
        for target in self._targets.values():
            if target.system_default:
                return target.key
        return next(iter(self._targets), None)

    def _validate_parent_graph(self) -> None:
        for workspace_id in self._targets:
            visited: set[WorkspaceKey] = set()
            current_id: WorkspaceKey | None = workspace_id
            while current_id is not None:
                if current_id in visited:
                    raise ValueError(
                        "Gateway registry 工作区父子关系形成循环: "
                        f"workspace_id={workspace_id}"
                    )
                visited.add(current_id)
                current_id = self._targets[current_id].parent_workspace
