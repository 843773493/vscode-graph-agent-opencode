from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Literal

import httpx

from app.gateway.processes import ManagedProcess
from app.gateway.schemas import GatewayWorkspaceDTO


ConnectionKind = Literal["local", "ssh"]


@dataclass(slots=True)
class WorkspaceTarget:
    workspace_id: str
    name: str
    root_path: str
    backend_url: str
    connection_kind: ConnectionKind
    managed: bool = False
    removable: bool = True
    system_default: bool = False
    remote: dict[str, object] = field(default_factory=dict)


class GatewayWorkspaceRegistry:
    def __init__(self, *, storage_path: Path) -> None:
        self._storage_path = storage_path
        self._targets: dict[str, WorkspaceTarget] = {}
        self._active_workspace_id: str | None = None
        self._order_customized = False
        self._managed_processes: dict[str, ManagedProcess] = {}
        self._load()

    @property
    def active_workspace_id(self) -> str | None:
        return self._active_workspace_id

    @staticmethod
    def build_workspace_id(kind: ConnectionKind, root_path: str, backend_url: str) -> str:
        digest = hashlib.sha1(f"{kind}\n{root_path}\n{backend_url}".encode("utf-8")).hexdigest()
        return f"gw_{digest[:12]}"

    @staticmethod
    def build_ssh_workspace_id(
        *,
        root_path: str,
        host: str,
        port: int,
        username: str,
        remote_backend_host: str,
        remote_backend_port: int,
    ) -> str:
        signature = "\n".join(
            [
                "ssh",
                root_path,
                f"{username}@{host}:{port}",
                f"{remote_backend_host}:{remote_backend_port}",
            ]
        )
        digest = hashlib.sha1(signature.encode("utf-8")).hexdigest()
        return f"gw_{digest[:12]}"

    def close(self) -> None:
        errors: list[str] = []
        for workspace_id, process in list(self._managed_processes.items()):
            try:
                process.close()
            except Exception as error:
                errors.append(f"{workspace_id}: {error}")
        self._managed_processes.clear()
        if errors:
            raise RuntimeError("关闭 Gateway 托管进程失败: " + "; ".join(errors))

    def upsert(
        self,
        target: WorkspaceTarget,
        *,
        process: ManagedProcess | None = None,
        activate: bool = True,
    ) -> WorkspaceTarget:
        self._targets[target.workspace_id] = target
        if process is not None:
            previous = self._managed_processes.pop(target.workspace_id, None)
            if previous is not None:
                previous.close()
            self._managed_processes[target.workspace_id] = process
        if activate or self._active_workspace_id is None:
            self._active_workspace_id = target.workspace_id
        self._save()
        return target

    def remove(self, workspace_id: str) -> None:
        target = self._targets.get(workspace_id)
        if target is None:
            raise KeyError(f"未知 Gateway 工作区: {workspace_id}")
        if not target.removable or target.system_default:
            raise PermissionError(f"默认工作区不能删除: {target.name}")
        process = self._managed_processes.pop(workspace_id, None)
        if process is not None:
            process.close()
        del self._targets[workspace_id]
        if self._active_workspace_id == workspace_id:
            self._active_workspace_id = self._default_workspace_id()
        self._save()

    def remove_backend_aliases(self, *, backend_url: str, keep_workspace_id: str) -> None:
        normalized_backend_url = backend_url.rstrip("/")
        changed = False
        for workspace_id, target in list(self._targets.items()):
            if workspace_id == keep_workspace_id:
                continue
            if target.backend_url.rstrip("/") != normalized_backend_url:
                continue
            process = self._managed_processes.pop(workspace_id, None)
            if process is not None:
                process.close()
            del self._targets[workspace_id]
            changed = True
        if self._active_workspace_id not in self._targets:
            self._active_workspace_id = self._default_workspace_id()
            changed = True
        if changed:
            self._save()

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
        self._save()

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
        self._save()

    def activate(self, workspace_id: str) -> None:
        if workspace_id not in self._targets:
            raise KeyError(f"未知 Gateway 工作区: {workspace_id}")
        self._active_workspace_id = workspace_id
        self._save()

    def resolve(self, workspace_id: str | None = None) -> WorkspaceTarget:
        target_id = workspace_id or self._active_workspace_id
        if target_id is None:
            raise LookupError("Gateway 尚未注册任何工作区")
        target = self._targets.get(target_id)
        if target is None:
            raise LookupError(f"Gateway 工作区不存在: {target_id}")
        return target

    async def list_dtos(self) -> list[GatewayWorkspaceDTO]:
        targets = list(self._targets.values())
        async with httpx.AsyncClient(timeout=2) as client:
            async def build_dto(target: WorkspaceTarget) -> GatewayWorkspaceDTO:
                status = "offline"
                try:
                    response = await client.get(
                        f"{target.backend_url.rstrip('/')}/api/v1/health",
                        headers={"X-Local-Token": "local-dev-token"},
                    )
                    if response.status_code == 200:
                        status = "ready"
                except Exception:
                    status = "offline"
                return GatewayWorkspaceDTO(
                    workspace_id=target.workspace_id,
                    name=target.name,
                    root_path=target.root_path,
                    backend_url=target.backend_url,
                    connection_kind=target.connection_kind,
                    status=status,
                    active=target.workspace_id == self._active_workspace_id,
                    managed=target.managed,
                    removable=target.removable,
                    system_default=target.system_default,
                    remote=target.remote,
                )

            return list(await asyncio.gather(*(build_dto(target) for target in targets)))

    def _default_workspace_id(self) -> str | None:
        for target in self._targets.values():
            if target.system_default:
                return target.workspace_id
        return next(iter(self._targets), None)

    def _load(self) -> None:
        if not self._storage_path.exists():
            return
        with self._storage_path.open("r", encoding="utf-8") as file:
            payload = json.load(file)
        targets = payload.get("targets", [])
        if not isinstance(targets, list):
            raise ValueError(f"Gateway registry targets 必须是数组: {self._storage_path}")
        for item in targets:
            if not isinstance(item, dict):
                raise ValueError(f"Gateway registry target 必须是对象: {self._storage_path}")
            target = WorkspaceTarget(
                workspace_id=str(item["workspace_id"]),
                name=str(item["name"]),
                root_path=str(item["root_path"]),
                backend_url=str(item["backend_url"]),
                connection_kind=item["connection_kind"],
                managed=bool(item.get("managed", False)),
                removable=bool(item.get("removable", True)),
                system_default=bool(item.get("system_default", False)),
                remote=dict(item.get("remote", {})),
            )
            self._targets[target.workspace_id] = target
        active_id = payload.get("active_workspace_id")
        if isinstance(active_id, str) and active_id in self._targets:
            self._active_workspace_id = active_id
        elif self._targets:
            self._active_workspace_id = next(iter(self._targets))
        self._order_customized = bool(payload.get("order_customized", False))

    def _save(self) -> None:
        self._storage_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "active_workspace_id": self._active_workspace_id,
            "order_customized": self._order_customized,
            "targets": [asdict(target) for target in self._targets.values()],
        }
        with self._storage_path.open("w", encoding="utf-8") as file:
            json.dump(payload, file, ensure_ascii=False, indent=2)
