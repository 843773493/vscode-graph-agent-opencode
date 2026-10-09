from __future__ import annotations

from app.abstractions.session_resources import (
    BackgroundTaskRegistryProtocol,
)
from app.core.background_task_contracts import BackgroundTaskHandle
from app.schemas.internal_v2.session_resource import (
    SessionResourceAction,
    SessionResourceControlResultDTO,
    SessionResourceDTO,
    SessionResourceKind,
)
from app.services.business.browser_resources.owner import BrowserResourceOwner
from app.services.business.session_resource_actions import (
    background_task_available_actions,
)
from app.services.business.terminal_resources.owner import TerminalResourceOwner
from app.services.business.workspace_resources.owner import ResourceConflict
from app.services.business.workspace_resources.records import ThreadRef
from app.services.mapping.session_resource_mapper import SessionResourceMapper

_ACTIVE_TERMINAL_STATUSES = frozenset({"created", "running"})
_CURRENT_BROWSER_STATUSES = frozenset({"created", "running", "frozen", "discarded"})


class BackgroundTaskResourceProvider:
    kind: SessionResourceKind = "background_task"

    def __init__(
        self,
        *,
        task_registry: BackgroundTaskRegistryProtocol,
        resource_mapper: SessionResourceMapper,
    ) -> None:
        self._task_registry = task_registry
        self._resource_mapper = resource_mapper

    async def list_resources(
        self,
        session_id: str,
        *,
        include_history: bool = True,
    ) -> list[SessionResourceDTO]:
        handles = [
            *self._task_registry.list_handles(session_id),
        ]
        if include_history:
            handles.extend(
                handle
                for handle in self._task_registry.list_closed_handles(session_id)
                if handle.status != "deleted"
            )
        return [self._to_resource(handle) for handle in handles]

    async def control(
        self,
        *,
        session_id: str,
        resource_id: str,
        action: SessionResourceAction,
        operation_id: str,
    ) -> SessionResourceControlResultDTO:
        if action in {"cancel", "reconcile"}:
            existing_handle = self._task_registry.get_handle(session_id, resource_id)
            if existing_handle is None:
                raise ValueError(
                    f"后台任务不存在: session_id={session_id}, task_id={resource_id}"
                )
            handle = await (
                self._task_registry.reconcile(session_id, resource_id) if action == "reconcile"
                else self._task_registry.cancel(session_id, resource_id)
            )
            return SessionResourceControlResultDTO(
                session_id=session_id,
                resource_id=resource_id,
                kind=self.kind,
                action=action,
                status=handle.status,
                resource=self._to_resource(handle),
            )
        if action == "delete":
            handle = await self._task_registry.delete(session_id, resource_id)
            return SessionResourceControlResultDTO(
                session_id=session_id,
                resource_id=resource_id,
                kind=self.kind,
                action=action,
                status="deleted",
                resource=self._to_resource(handle),
            )
        raise ValueError(f"background_task 资源不支持操作: {action}")

    async def cleanup_session(self, session_id: str) -> int:
        return await self._task_registry.delete_session(session_id)

    def _to_resource(self, handle: BackgroundTaskHandle) -> SessionResourceDTO:
        return self._resource_mapper.background_task_to_resource(
            handle,
            available_actions=background_task_available_actions(handle.status),
        )


class TerminalResourceProvider:
    kind: SessionResourceKind = "terminal"

    def __init__(self, *, terminal_owner: TerminalResourceOwner, resource_mapper: SessionResourceMapper) -> None:
        self._owner = terminal_owner
        self._resource_mapper = resource_mapper

    async def list_resources(self, session_id: str, *, include_history: bool = True) -> list[SessionResourceDTO]:
        terminals = await self._owner.list_resources(session_id=session_id)
        return [
            self._to_resource(item, session_id) for item in terminals
            if any(ref["session_id"] == session_id for ref in item["thread_associations"])
            and (include_history or item["status"] in _ACTIVE_TERMINAL_STATUSES)
        ]

    async def control(self, *, session_id: str, resource_id: str, action: SessionResourceAction, operation_id: str) -> SessionResourceControlResultDTO:
        terminal = await self._owner.get_resource(resource_id)
        if not any(item["session_id"] == session_id for item in terminal["thread_associations"]):
            raise ValueError(f"Terminal 不属于当前 Session 的 Thread 关联: {resource_id}")
        if action == "cancel":
            result = await self._owner.command(
                resource_id, action="stop", expected_revision=terminal["owner_revision"],
                holder_id=f"request:{operation_id}", operation_id=operation_id,
            )
        elif action == "delete":
            raise ResourceConflict("Terminal 仍有 Thread 关联；请在工作区 Terminal 面板解除关联后删除")
        else:
            raise ValueError(f"Terminal 不支持操作: {action}")
        resource = self._to_resource(result, session_id)
        return SessionResourceControlResultDTO(session_id=session_id, resource_id=resource_id, kind=self.kind, action=action, status=resource.status, resource=resource)

    async def cleanup_session(self, session_id: str) -> int:
        return await self._owner.cleanup_session(session_id)

    def _to_resource(self, terminal: dict[str, object], session_id: str) -> SessionResourceDTO:
        return self._resource_mapper.terminal_to_resource(
            {**terminal, "session_id": session_id},
            available_actions=["cancel"] if terminal["status"] == "running"
            and terminal["reconciliation_status"] == "verified" else [],
        )


class BrowserResourceProvider:
    kind: SessionResourceKind = "browser"

    def __init__(self, *, browser_owner: BrowserResourceOwner, resource_mapper: SessionResourceMapper) -> None:
        self._owner = browser_owner
        self._resource_mapper = resource_mapper

    async def list_resources(self, session_id: str, *, include_history: bool = True) -> list[SessionResourceDTO]:
        browsers = await self._owner.list_resources(session_id=session_id)
        return [
            self._to_resource(item, session_id) for item in browsers
            if any(ref["session_id"] == session_id for ref in item["thread_associations"])
            and (include_history or item["status"] in _CURRENT_BROWSER_STATUSES)
        ]

    async def control(self, *, session_id: str, resource_id: str, action: SessionResourceAction, operation_id: str) -> SessionResourceControlResultDTO:
        browser = await self._owner.get_resource(resource_id)
        refs = [item for item in browser["thread_associations"] if item["session_id"] == session_id]
        if not refs:
            raise ValueError(f"Browser 不属于当前 Session 的 Thread 关联: {resource_id}")
        if action == "cancel":
            result = await self._owner.command(
                resource_id, action="stop", expected_revision=browser["owner_revision"],
                holder_id=f"request:{operation_id}", operation_id=operation_id,
            )
        elif action == "resume":
            receipt = await self._owner.perform(
                resource_id, action="wake", payload={}, expected_revision=browser["owner_revision"],
                thread=ThreadRef(**refs[0]), holder_id=f"request:{operation_id}", operation_id=operation_id,
            )
            result = receipt["browser"]
        elif action == "delete":
            raise ResourceConflict("Browser 仍有 Thread 关联；请在工作区 Browser 面板解除关联后删除")
        else:
            raise ValueError(f"Browser 不支持操作: {action}")
        resource = self._to_resource(result, session_id)
        return SessionResourceControlResultDTO(session_id=session_id, resource_id=resource_id, kind=self.kind, action=action, status=resource.status, resource=resource)

    async def cleanup_session(self, session_id: str) -> int:
        return await self._owner.cleanup_session(session_id)

    def _to_resource(self, browser: dict[str, object], session_id: str) -> SessionResourceDTO:
        actions = []
        if browser["reconciliation_status"] == "verified" and browser["status"] == "running":
            actions.append("cancel")
            if browser.get("resource_state") in {"frozen", "discarded"}:
                actions.append("resume")
        return self._resource_mapper.browser_to_resource({**browser, "session_id": session_id}, available_actions=actions)
