import json
import tempfile
import uuid
from pathlib import Path
import pytest
from app.services.session_service import SessionService
from app.schemas.session import SessionCreateRequest, SessionUpdateRequest
from app.core.exceptions import NotFoundError
from app.core.path_utils import get_session_path, get_session_file


class TestSessionService:
    """测试会话服务功能"""

    def setup_method(self):
        """每个测试前设置临时会话目录"""
        # 保存原始会话目录
        import app.core.path_utils
        self.original_sessions_dir = app.core.path_utils.SESSIONS_DIR
        
        # 创建临时目录作为会话根目录
        self.temp_dir = tempfile.mkdtemp()
        app.core.path_utils.SESSIONS_DIR = Path(self.temp_dir) / "sessions"
        app.core.path_utils.SESSIONS_DIR.mkdir(exist_ok=True, parents=True)

    def teardown_method(self):
        """测试后恢复原始目录"""
        import app.core.path_utils
        app.core.path_utils.SESSIONS_DIR = self.original_sessions_dir
        
        # 清理临时目录
        import shutil
        shutil.rmtree(self.temp_dir)

    @pytest.mark.asyncio
    async def test_create_session(self):
        """测试创建会话"""
        request = SessionCreateRequest(title="Test Session")
        session = await SessionService.create(request)
        
        assert session.session_id.startswith("ses_")
        assert len(session.session_id) == 16  # ses_ + 12 hex chars
        assert session.title == "Test Session"
        assert session.created_at is not None
        assert session.updated_at == session.created_at
        
        # 验证文件已创建
        session_file = get_session_file(session.session_id)
        assert session_file.exists()
        
        # 验证文件内容
        with open(session_file, "r", encoding="utf-8") as f:
            data = json.load(f)
            assert data["session_id"] == session.session_id
            assert data["title"] == "Test Session"

    @pytest.mark.asyncio
    async def test_get_session(self):
        """测试获取会话"""
        # 先创建会话
        create_request = SessionCreateRequest(title="Get Test")
        created = await SessionService.create(create_request)
        
        # 获取会话
        retrieved = await SessionService.get(created.session_id)
        
        assert retrieved.session_id == created.session_id
        assert retrieved.title == created.title
        assert retrieved.created_at == created.created_at

    @pytest.mark.asyncio
    async def test_get_nonexistent_session(self):
        """测试获取不存在的会话"""
        with pytest.raises(NotFoundError, match="Session .* not found"):
            await SessionService.get("nonexistent-session-id")

    @pytest.mark.asyncio
    async def test_update_session(self):
        """测试更新会话"""
        # 创建会话
        create_request = SessionCreateRequest(title="Original Title")
        created = await SessionService.create(create_request)
        original_updated_at = created.updated_at
        
        # 更新会话
        update_request = SessionUpdateRequest(title="Updated Title")
        updated = await SessionService.update(created.session_id, update_request)
        
        assert updated.title == "Updated Title"
        assert updated.session_id == created.session_id
        assert updated.updated_at > original_updated_at
        
        # 验证文件已更新
        session_file = get_session_file(created.session_id)
        with open(session_file, "r", encoding="utf-8") as f:
            data = json.load(f)
            assert data["title"] == "Updated Title"

    @pytest.mark.asyncio
    async def test_delete_session(self):
        """测试删除会话"""
        # 创建会话
        create_request = SessionCreateRequest(title="Delete Test")
        created = await SessionService.create(create_request)
        
        session_dir = get_session_path(created.session_id)
        assert session_dir.exists()
        
        # 删除会话
        await SessionService.delete(created.session_id)
        
        # 验证目录已删除
        assert not session_dir.exists()
        
        # 验证无法再获取
        with pytest.raises(NotFoundError):
            await SessionService.get(created.session_id)

    @pytest.mark.asyncio
    async def test_delete_nonexistent_session(self):
        """测试删除不存在的会话"""
        with pytest.raises(NotFoundError, match="Session .* not found"):
            await SessionService.delete("nonexistent-session-id")

    @pytest.mark.asyncio
    async def test_list_sessions(self):
        """测试列出会话"""
        # 创建多个会话
        import asyncio
        session_ids = []
        for i in range(5):
            request = SessionCreateRequest(title=f"Session {i}")
            session = await SessionService.create(request)
            session_ids.append(session.session_id)
            await asyncio.sleep(0.001)  # 确保时间戳不同
        
        # 列出会话
        result = await SessionService.list()
        
        assert result["total"] == 5
        assert len(result["items"]) == 5
        
        # 验证按创建时间倒序排列
        titles = [s.title for s in result["items"]]
        assert titles == ["Session 4", "Session 3", "Session 2", "Session 1", "Session 0"]

    @pytest.mark.asyncio
    async def test_list_sessions_pagination(self):
        """测试会话列表分页"""
        # 创建15个会话
        import asyncio
        for i in range(15):
            request = SessionCreateRequest(title=f"Session {i}")
            await SessionService.create(request)
            await asyncio.sleep(0.001)  # 确保时间戳不同
        
        # 第一页 (skip=0, limit=5)
        page1 = await SessionService.list(skip=0, limit=5)
        assert len(page1["items"]) == 5
        assert page1["total"] == 15
        assert page1["items"][0].title == "Session 14"
        assert page1["items"][4].title == "Session 10"
        
        # 第二页 (skip=5, limit=5)
        page2 = await SessionService.list(skip=5, limit=5)
        assert len(page2["items"]) == 5
        assert page2["items"][0].title == "Session 9"
        assert page2["items"][4].title == "Session 5"

    @pytest.mark.asyncio
    async def test_session_persistence(self):
        """测试会话持久化 - 重启后仍然存在"""
        # 创建会话
        request = SessionCreateRequest(title="Persistence Test")
        session = await SessionService.create(request)
        
        # 模拟服务重启 - 重新获取
        retrieved = await SessionService.get(session.session_id)
        assert retrieved.session_id == session.session_id
        assert retrieved.title == "Persistence Test"

    @pytest.mark.asyncio
    async def test_session_path_isolation(self):
        """测试会话路径隔离 - 每个会话有独立目录"""
        # 创建两个会话
        session1 = await SessionService.create(SessionCreateRequest(title="Session 1"))
        session2 = await SessionService.create(SessionCreateRequest(title="Session 2"))
        
        path1 = get_session_path(session1.session_id)
        path2 = get_session_path(session2.session_id)
        
        assert path1 != path2
        assert path1.exists()
        assert path2.exists()
        assert path1.is_dir()
        assert path2.is_dir()
        
        # 验证目录内容隔离
        file1 = get_session_file(session1.session_id)
        file2 = get_session_file(session2.session_id)
        
        assert file1.parent == path1
        assert file2.parent == path2

    @pytest.mark.asyncio
    async def test_delete_cleans_all_files(self):
        """测试删除会话时清理所有文件"""
        # 创建会话
        session = await SessionService.create(SessionCreateRequest(title="Cleanup Test"))
        session_dir = get_session_path(session.session_id)
        
        # 在会话目录中创建额外文件
        extra_file = session_dir / "extra.txt"
        extra_file.write_text("test content")
        sub_dir = session_dir / "subdir"
        sub_dir.mkdir()
        (sub_dir / "nested.txt").write_text("nested content")
        
        # 删除会话
        await SessionService.delete(session.session_id)
        
        # 验证整个目录都被删除
        assert not session_dir.exists()

    @pytest.mark.asyncio
    async def test_control_session(self):
        """测试会话控制操作"""
        session = await SessionService.create(SessionCreateRequest(title="Control Test"))
        
        result = await SessionService.control(session.session_id, "pause", {"reason": "test"})
        
        assert result["session_id"] == session.session_id
        assert result["action"] == "pause"
        assert result["status"] == "executed"

    @pytest.mark.asyncio
    async def test_control_nonexistent_session(self):
        """测试控制不存在的会话"""
        with pytest.raises(NotFoundError):
            await SessionService.control("nonexistent", "pause")
