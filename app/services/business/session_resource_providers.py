from __future__ import annotations

from datetime import datetime, timezone

from app.abstractions.session_resources import (
    BackgroundTaskRegistryProtocol,
    BrowserManagerClientProtocol,
    HistoricalTerminalRecordReaderProtocol,
    SessionResourceMessageProtocol,
    TerminalManagerClientProtocol,
)
from app.core.background_task_registry import BackgroundTaskHandle
from app.schemas.public_v2.session_resource import (
    SessionResourceAction,
    SessionResourceControlResultDTO,
    SessionResourceDTO,
    SessionResourceKind,
)
from app.services.business.session_resource_actions import (
    background_task_available_actions,
    browser_available_actions,
    terminal_available_actions,
)
from app.services.mapping.session_resource_mapper import SessionResourceMapper


class BackgroundTaskResourceProvider:
    kind: SessionResourceKind = "background_task"

    def __init__(
        self,
        *,
        task_registry: BackgroundTaskRegistryProtocol,
        message_service: SessionResourceMessageProtocol,
        resource_mapper: SessionResourceMapper,
    ) -> None:
        self._task_registry = task_registry
        self._message_service = message_service
        self._resource_mapper = resource_mapper

    async def list_resources(self, session_id: str) -> list[SessionResourceDTO]:
        handles = [
            *self._task_registry.list_handles(session_id),
            *[
                handle
                for handle in self._task_registry.list_closed_handles(session_id)
                if handle.status != "deleted"
            ],
        ]
        return [self._to_resource(handle) for handle in handles]

    async def control(
        self,
        *,
        session_id: str,
        resource_id: str,
        action: SessionResourceAction,
    ) -> SessionResourceControlResultDTO:
        if action == "cancel":
            existing_handle = self._task_registry.get_handle(session_id, resource_id)
            if existing_handle is None:
                raise ValueError(
                    f"后台任务不存在: session_id={session_id}, task_id={resource_id}"
                )
            handle = await self._task_registry.cancel(session_id, resource_id)
            if existing_handle.task_name == "monitor_session_agent_end":
                self._append_monitor_cancel_reminder(handle)
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

    def _append_monitor_cancel_reminder(self, handle: BackgroundTaskHandle) -> None:
        cancelled_at = datetime.now(timezone.utc).isoformat()
        target_session_id = handle.metadata.get("target_session_id")
        target_text = (
            f"目标 session：{target_session_id}。"
            if isinstance(target_session_id, str) and target_session_id
            else ""
        )
        reminder = (
            f"用户于 {cancelled_at} 通过后台连接面板手动取消了后台任务 "
            f"{handle.task_name}（task_id={handle.task_id}）。"
            f"{target_text}"
            "该监控任务不会继续监听 AGENT_END，也不会再自动转发目标回复。"
            "请不要等待这个后台任务继续产生消息，后续按用户最新请求继续。"
        )
        injected = self._message_service.append_system_reminder(
            session_id=handle.session_id,
            reminder=reminder,
            response_metadata={
                "phase": "background_task",
                "source": "resource_cancel",
                "user_initiated": True,
                "task_id": handle.task_id,
                "task_name": handle.task_name,
                "action": "cancel",
                "target_session_id": target_session_id,
                "cancelled_at": cancelled_at,
            },
            checkpoint_source="resource_cancel",
        )
        if not injected:
            raise RuntimeError(
                "后台任务已取消，但未找到可注入 system_reminder 的 checkpoint: "
                f"session_id={handle.session_id}, task_id={handle.task_id}"
            )


class TerminalResourceProvider:
    kind: SessionResourceKind = "terminal"

    def __init__(
        self,
        *,
        terminal_manager: TerminalManagerClientProtocol,
        historical_reader: HistoricalTerminalRecordReaderProtocol,
        message_service: SessionResourceMessageProtocol,
        resource_mapper: SessionResourceMapper,
    ) -> None:
        self._terminal_manager = terminal_manager
        self._historical_reader = historical_reader
        self._message_service = message_service
        self._resource_mapper = resource_mapper

    async def list_resources(self, session_id: str) -> list[SessionResourceDTO]:
        active_terminals = self._terminal_manager.list_terminals_from_state(session_id)
        records = await self._message_service.list_agent_state_records(session_id)
        historical_terminals = self._historical_reader.read_records(
            session_id=session_id,
            active_terminals=active_terminals,
            agent_state_records=records,
        )
        return [
            *[self._to_resource(terminal) for terminal in active_terminals],
            *[self._to_resource(terminal) for terminal in historical_terminals],
        ]

    async def control(
        self,
        *,
        session_id: str,
        resource_id: str,
        action: SessionResourceAction,
    ) -> SessionResourceControlResultDTO:
        terminal = self._get_record(session_id, resource_id)
        if action == "cancel":
            result = await self._terminal_manager.kill_terminal(resource_id)
            resource = self._to_resource(result["terminal"])
            return SessionResourceControlResultDTO(
                session_id=session_id,
                resource_id=resource_id,
                kind=self.kind,
                action=action,
                status=resource.status,
                resource=resource,
            )
        if action == "delete":
            result = await self._terminal_manager.delete_terminal(resource_id)
            deleted_terminal = result.get("terminal") if isinstance(result, dict) else None
            resource = self._to_resource(
                deleted_terminal
                if isinstance(deleted_terminal, dict)
                else {**terminal, "status": "deleted"}
            )
            return SessionResourceControlResultDTO(
                session_id=session_id,
                resource_id=resource_id,
                kind=self.kind,
                action=action,
                status="deleted",
                resource=resource,
            )
        raise ValueError(f"terminal 资源不支持操作: {action}")

    async def cleanup_session(self, session_id: str) -> int:
        terminals = self._terminal_manager.list_terminals_from_state(session_id)
        for terminal in terminals:
            terminal_id = terminal.get("terminal_id")
            if not isinstance(terminal_id, str) or not terminal_id:
                raise RuntimeError(f"终端记录缺少 terminal_id: session_id={session_id}")
            await self._terminal_manager.delete_terminal(terminal_id)
        return len(terminals)

    def _get_record(self, session_id: str, terminal_id: str) -> dict[str, object]:
        for terminal in self._terminal_manager.list_terminals_from_state(session_id):
            if terminal.get("terminal_id") == terminal_id:
                return terminal
        raise ValueError(f"terminal 不存在或不属于当前 session: {terminal_id}")

    def _to_resource(self, terminal: dict[str, object]) -> SessionResourceDTO:
        status = str(terminal.get("status") or "unknown")
        return self._resource_mapper.terminal_to_resource(
            terminal,
            available_actions=terminal_available_actions(status),
        )


class BrowserResourceProvider:
    kind: SessionResourceKind = "browser"

    def __init__(
        self,
        *,
        browser_manager: BrowserManagerClientProtocol,
        resource_mapper: SessionResourceMapper,
    ) -> None:
        self._browser_manager = browser_manager
        self._resource_mapper = resource_mapper

    async def list_resources(self, session_id: str) -> list[SessionResourceDTO]:
        return [
            self._to_resource(dict(browser))
            for browser in self._browser_manager.list_browsers_from_state(session_id)
            if browser.get("status") != "deleted"
        ]

    async def control(
        self,
        *,
        session_id: str,
        resource_id: str,
        action: SessionResourceAction,
    ) -> SessionResourceControlResultDTO:
        browser = self._get_record(session_id, resource_id)
        if action == "cancel":
            result = await self._browser_manager.close_browser(resource_id)
            resource = self._to_resource(dict(result))
            return SessionResourceControlResultDTO(
                session_id=session_id,
                resource_id=resource_id,
                kind=self.kind,
                action=action,
                status=resource.status,
                resource=resource,
            )
        if action == "delete":
            result = await self._browser_manager.delete_browser(resource_id)
            deleted_browser = result.get("browser") if isinstance(result, dict) else None
            resource = self._to_resource(
                dict(deleted_browser)
                if isinstance(deleted_browser, dict)
                else {**browser, "status": "deleted"}
            )
            return SessionResourceControlResultDTO(
                session_id=session_id,
                resource_id=resource_id,
                kind=self.kind,
                action=action,
                status="deleted",
                resource=resource,
            )
        raise ValueError(f"browser 资源不支持操作: {action}")

    async def cleanup_session(self, session_id: str) -> int:
        browsers = self._browser_manager.list_browsers_from_state(session_id)
        for browser in browsers:
            browser_id = browser.get("browser_id")
            if not isinstance(browser_id, str) or not browser_id:
                raise RuntimeError(f"浏览器记录缺少 browser_id: session_id={session_id}")
            await self._browser_manager.delete_browser(browser_id)
        return len(browsers)

    def _get_record(self, session_id: str, browser_id: str) -> dict[str, object]:
        for browser in self._browser_manager.list_browsers_from_state(session_id):
            if browser.get("browser_id") == browser_id:
                return dict(browser)
        raise ValueError(f"browser 不存在或不属于当前 session: {browser_id}")

    def _to_resource(self, browser: dict[str, object]) -> SessionResourceDTO:
        status = str(browser.get("status") or "unknown")
        return self._resource_mapper.browser_to_resource(
            browser,
            available_actions=browser_available_actions(status),
        )
