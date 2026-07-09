from __future__ import annotations

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
    remote: dict[str, object] = field(default_factory=dict)


class GatewayWorkspaceRegistry:
    def __init__(self, *, storage_path: Path) -> None:
        self._storage_path = storage_path
        self._targets: dict[str, WorkspaceTarget] = {}
        self._active_workspace_id: str | None = None
        self._managed_processes: dict[str, ManagedProcess] = {}
        self._load()

    @property
    def active_workspace_id(self) -> str | None:
        return self._active_workspace_id

    @staticmethod
    def build_workspace_id(kind: ConnectionKind, root_path: str, backend_url: str) -> str:
        digest = hashlib.sha1(f"{kind}\n{root_path}\n{backend_url}".encode("utf-8")).hexdigest()
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
        if workspace_id not in self._targets:
            raise KeyError(f"未知 Gateway 工作区: {workspace_id}")
        process = self._managed_processes.pop(workspace_id, None)
        if process is not None:
            process.close()
        del self._targets[workspace_id]
        if self._active_workspace_id == workspace_id:
            self._active_workspace_id = next(iter(self._targets), None)
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
        result: list[GatewayWorkspaceDTO] = []
        async with httpx.AsyncClient(timeout=2) as client:
            for target in self._targets.values():
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
                result.append(
                    GatewayWorkspaceDTO(
                        workspace_id=target.workspace_id,
                        name=target.name,
                        root_path=target.root_path,
                        backend_url=target.backend_url,
                        connection_kind=target.connection_kind,
                        status=status,
                        active=target.workspace_id == self._active_workspace_id,
                        managed=target.managed,
                        remote=target.remote,
                    )
                )
        return result

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
                remote=dict(item.get("remote", {})),
            )
            self._targets[target.workspace_id] = target
        active_id = payload.get("active_workspace_id")
        if isinstance(active_id, str) and active_id in self._targets:
            self._active_workspace_id = active_id
        elif self._targets:
            self._active_workspace_id = next(iter(self._targets))

    def _save(self) -> None:
        self._storage_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "active_workspace_id": self._active_workspace_id,
            "targets": [asdict(target) for target in self._targets.values()],
        }
        with self._storage_path.open("w", encoding="utf-8") as file:
            json.dump(payload, file, ensure_ascii=False, indent=2)
