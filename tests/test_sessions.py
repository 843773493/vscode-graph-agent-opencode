#!/usr/bin/env python3
"""
End-to-end tests for session management with isolated workspace.
All tests run in playground environment without polluting development workspace.
"""
import os
import json
import uuid
from pathlib import Path
import pytest

# Setup test environment BEFORE importing any app modules
from scripts.setup_test_env import setup_test_environment
setup_test_environment()

# Now import app modules
from app.core.path_utils import (
    WORKSPACE_ROOT,
    get_session_path,
    get_session_file,
    ensure_session_dir,
    validate_workspace_path
)
from app.services.session_service import SessionService
from app.schemas.session import SessionCreateRequest as SessionCreate, SessionUpdateRequest as SessionUpdate


@pytest.fixture
def session_service():
    """Fixture for session service instance"""
    return SessionService()


@pytest.fixture
def test_session_id():
    """Generate unique test session ID"""
    return f"test_session_{uuid.uuid4().hex[:8]}"


class TestSessionCRUD:
    """Test complete session CRUD lifecycle"""
    
    def test_workspace_isolation(self):
        """Verify tests are running in isolated playground workspace"""
        workspace_path = Path(os.environ["WORKSPACE_ROOT"])
        assert "playground" in str(workspace_path), "Test should run in playground workspace"
        assert WORKSPACE_ROOT == workspace_path, "WORKSPACE_ROOT should match environment variable"
        assert WORKSPACE_ROOT.exists(), "Workspace directory should exist"
    
    def test_create_session(self, session_service, test_session_id):
        """Test session creation"""
        # Create session
        session_data = SessionCreate(
            id=test_session_id,
            name="Test Session",
            description="Test session for E2E testing"
        )
        
        session = session_service.create(session_data)
        
        # Verify session exists
        assert session.id == test_session_id
        assert session.name == "Test Session"
        
        # Verify directory and file were created
        session_dir = get_session_path(test_session_id)
        session_file = get_session_file(test_session_id)
        
        assert session_dir.exists()
        assert session_file.exists()
        
        # Verify file content
        with open(session_file, "r", encoding="utf-8") as f:
            saved_data = json.load(f)
        assert saved_data["id"] == test_session_id
        assert saved_data["name"] == "Test Session"
    
    def test_get_session(self, session_service, test_session_id):
        """Test retrieving existing session"""
        # First create session
        session_data = SessionCreate(
            id=test_session_id,
            name="Get Test Session"
        )
        session_service.create(session_data)
        
        # Retrieve session
        retrieved = session_service.get(test_session_id)
        
        assert retrieved.id == test_session_id
        assert retrieved.name == "Get Test Session"
    
    def test_update_session(self, session_service, test_session_id):
        """Test updating session properties"""
        # Create initial session
        session_data = SessionCreate(
            id=test_session_id,
            name="Original Name",
            description="Original description"
        )
        session_service.create(session_data)
        
        # Update session
        update_data = SessionUpdate(
            name="Updated Name",
            description="Updated description"
        )
        updated = session_service.update(test_session_id, update_data)
        
        assert updated.name == "Updated Name"
        assert updated.description == "Updated description"
        
        # Verify persistence
        retrieved = session_service.get(test_session_id)
        assert retrieved.name == "Updated Name"
        assert retrieved.description == "Updated description"
    
    def test_list_sessions(self, session_service):
        """Test listing all sessions"""
        # Create multiple test sessions
        session_ids = []
        for i in range(3):
            sid = f"list_test_{uuid.uuid4().hex[:6]}"
            session_ids.append(sid)
            session_service.create(SessionCreate(id=sid, name=f"Session {i}"))
        
        # List sessions
        sessions = session_service.list()
        
        # Verify all created sessions are present
        found_ids = [s.id for s in sessions]
        for sid in session_ids:
            assert sid in found_ids
    
    def test_delete_session(self, session_service, test_session_id):
        """Test session deletion"""
        # Create session
        session_service.create(SessionCreate(id=test_session_id, name="To Delete"))
        
        # Verify it exists
        assert session_service.exists(test_session_id)
        
        # Delete session
        session_service.delete(test_session_id)
        
        # Verify it's gone
        assert not session_service.exists(test_session_id)
        assert not get_session_path(test_session_id).exists()
    
    def test_session_persistence(self, session_service, test_session_id):
        """Test session data persists across service instances"""
        # Create session with first service instance
        session_service.create(SessionCreate(
            id=test_session_id,
            name="Persistence Test",
            metadata={"test_key": "test_value"}
        ))
        
        # Create new service instance (simulate fresh process)
        new_service = SessionService()
        
        # Retrieve session
        session = new_service.get(test_session_id)
        assert session.name == "Persistence Test"
        assert session.metadata["test_key"] == "test_value"
    
    def test_path_validation(self):
        """Test workspace path validation works correctly"""
        # Valid path inside workspace
        valid_path = validate_workspace_path("test_file.txt")
        assert str(valid_path).startswith(str(WORKSPACE_ROOT))
        
        # Path traversal attempt should be blocked
        with pytest.raises(Exception):
            validate_workspace_path("../outside_workspace.txt")


class TestSessionLifecycle:
    """Test complete session lifecycle flow"""
    
    def test_full_session_lifecycle(self, session_service):
        """Test complete create -> read -> update -> delete flow"""
        session_id = f"lifecycle_{uuid.uuid4().hex[:8]}"
        
        # 1. Create
        session = session_service.create(SessionCreate(
            id=session_id,
            name="Lifecycle Test",
            status="created"
        ))
        assert session.status == "created"
        
        # 2. Read
        retrieved = session_service.get(session_id)
        assert retrieved.id == session_id
        
        # 3. Update
        updated = session_service.update(session_id, SessionUpdate(status="running"))
        assert updated.status == "running"
        
        # 4. Verify persistence
        reloaded = session_service.get(session_id)
        assert reloaded.status == "running"
        
        # 5. Delete
        session_service.delete(session_id)
        assert not session_service.exists(session_id)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
