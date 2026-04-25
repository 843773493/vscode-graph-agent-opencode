from __future__ import annotations
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List, Optional
from app.schemas.session import SessionDTO, SessionCreateRequest, SessionUpdateRequest
from app.core.path_utils import get_session_file, ensure_session_dir, get_session_path, get_sessions_dir, get_logs_dir
from app.core.exceptions import NotFoundError
from app.services.config_service import ConfigService


class SessionService:
    _instance: Optional["SessionService"] = None

    def __init__(self):
        pass

    @classmethod
    def get_instance(cls) -> "SessionService":
        if cls._instance is None:
            cls._instance = SessionService()
        return cls._instance
    @staticmethod
    async def get(session_id: str) -> SessionDTO:
        """Get session by ID"""
        session_file = get_session_file(session_id)
        
        if not session_file.exists():
            raise NotFoundError(f"Session {session_id} not found")
            
        with open(session_file, "r", encoding="utf-8") as f:
            data = json.load(f)
            
        # Pydantic will automatically handle ISO string to datetime conversion
        return SessionDTO.model_validate(data)

    @staticmethod
    async def list(workspace_id: Optional[str] = None, skip: int = 0, limit: int = 100, cursor: Optional[str] = None) -> dict:
        """List all sessions"""
        sessions_dir = get_sessions_dir()
        sessions_dir.mkdir(exist_ok=True)
        
        sessions = []
        for session_dir in sessions_dir.iterdir():
            if session_dir.is_dir():
                session_file = session_dir / "session.json"
                if session_file.exists():
                    try:
                        with open(session_file, "r", encoding="utf-8") as f:
                            data = json.load(f)
                            sessions.append(SessionDTO.model_validate(data))
                    except Exception:
                        continue
        
        # Sort by created_at descending
        sessions.sort(key=lambda s: s.created_at, reverse=True)
        
        # Apply pagination
        paginated = sessions[skip:skip+limit]
        
        return {
            "items": paginated,
            "total": len(sessions),
            "cursor": None
        }

    @staticmethod
    async def create(session: SessionCreateRequest) -> SessionDTO:
        """Create new session"""
        session_id = f"ses_{uuid.uuid4().hex[:12]}"
        now = datetime.now(timezone.utc)
        config_service = ConfigService.get_instance()
        resolved_agent_id = config_service.validate_agent_id(session.agent_id)
        
        session_data = SessionDTO(
            session_id=session_id,
            workspace_id="ws_local",
            title=session.title,
            current_agent_id=resolved_agent_id,
            created_at=now,
            updated_at=now
        )
        
        # Create session directory and save metadata
        ensure_session_dir(session_id)
        session_file = get_session_file(session_id)
        
        with open(session_file, "w", encoding="utf-8") as f:
            json.dump(session_data.model_dump(), f, ensure_ascii=False, indent=2, default=str)
            
        return session_data

    @staticmethod
    async def update(session_id: str, session: SessionUpdateRequest) -> SessionDTO:
        """Update existing session"""
        existing = await SessionService.get(session_id)

        if session.agent_id is not None:
            ConfigService.get_instance().validate_agent_id(session.agent_id)
        
        update_data = session.model_dump(exclude_unset=True)
        
        # 记录旧 agent_id，用于缓存清理
        old_agent_id = existing.current_agent_id
        new_agent_id = update_data.get("agent_id")
        agent_id_changed = new_agent_id is not None and new_agent_id != old_agent_id
        
        # 批量更新字段
        for key, value in update_data.items():
            if key == "agent_id":
                existing.current_agent_id = value
            else:
                setattr(existing, key, value)
        
        # 当 agent_id 变更时，清除旧 agent 的运行时缓存，确保下次使用新 agent
        if agent_id_changed:
            try:
                from app.services.agent_execution_service import AgentExecutionService
                old_cache_key = f"{session_id}::{old_agent_id}"
                AgentExecutionService._agent_cache.pop(old_cache_key, None)
            except Exception:
                pass  # 缓存清除失败不影响主流程
            
        # 确保时间戳递增，避免测试时毫秒级冲突
        import time
        time.sleep(0.001)
        existing.updated_at = datetime.now(timezone.utc)
        
        session_file = get_session_file(session_id)
        with open(session_file, "w", encoding="utf-8") as f:
            json.dump(existing.model_dump(), f, ensure_ascii=False, indent=2, default=str)
            
        return existing

    @staticmethod
    async def delete(session_id: str) -> None:
        """Delete session"""
        session_dir = get_session_path(session_id)
        
        if not session_dir.exists():
            raise NotFoundError(f"Session {session_id} not found")
            
        # Delete session directory and all contents
        import shutil
        shutil.rmtree(session_dir)

    @staticmethod
    async def control(session_id: str, action: str, payload: dict = None) -> dict:
        """Control session action"""
        # Verify session exists
        await SessionService.get(session_id)
        return {"session_id": session_id, "action": action, "status": "executed"}

    @staticmethod
    async def list_trace_events(session_id: str) -> list[dict[str, Any]]:
        """Read the stored execution trace for a session."""
        trace_file = get_logs_dir() / "traces" / f"trace_{session_id}.jsonl"

        if not trace_file.exists():
            return []

        events: list[dict[str, Any]] = []
        with open(trace_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue

                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue

                if isinstance(event, dict):
                    events.append(event)

        events.sort(key=lambda item: item.get("timestamp", 0))
        return events
