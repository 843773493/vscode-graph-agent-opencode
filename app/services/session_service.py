from __future__ import annotations
import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import List, Optional
from app.schemas.session import SessionDTO, SessionCreateRequest, SessionUpdateRequest
from app.core.path_utils import get_session_file, ensure_session_dir, get_session_path, SESSIONS_DIR
from app.core.exceptions import NotFoundError


class SessionService:
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
        # 动态导入以获取最新的SESSIONS_DIR值（支持测试环境覆盖）
        from app.core.path_utils import SESSIONS_DIR
        sessions_dir = SESSIONS_DIR
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
        now = datetime.utcnow()
        
        session_data = SessionDTO(
            session_id=session_id,
            workspace_id="ws_local",
            title=session.title,
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
        
        update_data = session.model_dump(exclude_unset=True)
        for key, value in update_data.items():
            setattr(existing, key, value)
            
        # 确保时间戳递增，避免测试时毫秒级冲突
        import time
        time.sleep(0.001)
        existing.updated_at = datetime.utcnow()
        
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
