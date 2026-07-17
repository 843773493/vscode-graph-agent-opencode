import os
import tempfile
import uuid
from pathlib import Path
import pytest
from app.core.path_utils import (
    ensure_session_dir,
    get_boxteam_home,
    get_gateway_root,
    get_session_path,
    get_user_config_root,
    get_user_workspace_root,
    initialize_directories,
    safe_join,
    validate_workspace_path,
)
from app.core.exceptions import ForbiddenError
from app.core.storage_migration import migrate_user_storage_layout


class TestPathUtils:
    """测试路径工具类安全功能"""

    def setup_method(self):
        """每个测试前创建临时目录作为测试根目录"""
        self.temp_dir = tempfile.mkdtemp()
        self.base_path = Path(self.temp_dir).resolve()

    def test_safe_join_normal_paths(self):
        """测试正常路径拼接"""
        result = safe_join(self.base_path, "test", "file.txt")
        assert result == self.base_path / "test" / "file.txt"
        assert result.exists() is False  # 只返回路径，不创建

    def test_safe_join_directory_traversal_attack(self):
        """测试目录遍历防护 - 阻止 ../ 攻击"""
        with pytest.raises(ForbiddenError, match="Path traversal detected"):
            safe_join(self.base_path, "../etc/passwd")

        with pytest.raises(ForbiddenError, match="Path traversal detected"):
            safe_join(self.base_path, "test/../../etc/passwd")

        if os.name == "nt":
            with pytest.raises(ForbiddenError, match="Path traversal detected"):
                safe_join(self.base_path, "..\\windows\\system32")
        else:
            assert safe_join(self.base_path, "..\\windows\\system32").parent == self.base_path

    def test_safe_join_absolute_path_attack(self):
        """测试绝对路径攻击防护"""
        with pytest.raises(ForbiddenError, match="Path traversal detected"):
            safe_join(self.base_path, "/etc/passwd")

        if os.name == "nt":
            with pytest.raises(ForbiddenError, match="Path traversal detected"):
                safe_join(self.base_path, "C:\\windows\\system32")
        else:
            assert safe_join(self.base_path, "C:\\windows\\system32").parent == self.base_path

    def test_safe_join_symlink_attack(self):
        """测试符号链接攻击防护"""
        # 创建指向系统目录的符号链接
        symlink_path = self.base_path / "link"
        try:
            symlink_path.symlink_to("/etc")
        except (OSError, AttributeError):
            pytest.skip("Symlinks not supported on this platform")

        try:
            with pytest.raises(ForbiddenError, match="Path traversal detected"):
                safe_join(self.base_path, "link/passwd")
        except OSError:
            # Windows上resolve()对不存在的符号链接会抛出错误，这也是预期行为
            pytest.skip("Windows path resolution behavior differs")

    def test_safe_join_exact_base_path(self):
        """测试允许访问基础目录本身"""
        result = safe_join(self.base_path)
        assert result == self.base_path

    def test_validate_workspace_path(self):
        """测试工作区路径验证"""
        # 先设置临时工作区环境变量
        original_env = os.environ.get("WORKSPACE_ROOT")
        os.environ["WORKSPACE_ROOT"] = str(self.base_path)

        try:
            # 重新导入以刷新环境变量
            from importlib import reload
            import app.core.path_utils
            reload(app.core.path_utils)
            from app.core.path_utils import validate_workspace_path

            # 正常路径
            result = validate_workspace_path("test/file.txt")
            assert result == self.base_path / "test" / "file.txt"

            # 遍历攻击
            with pytest.raises(ForbiddenError):
                validate_workspace_path("../outside.txt")

        finally:
            if original_env:
                os.environ["WORKSPACE_ROOT"] = original_env
            else:
                del os.environ["WORKSPACE_ROOT"]

    def test_get_session_path_isolation(self):
        """测试会话路径隔离"""
        session_id = "test-session-123"
        path = get_session_path(session_id)
        assert session_id in str(path)
        assert path.parent.name == "sessions"

    def test_ensure_session_dir_creates_directory(self):
        """测试确保会话目录存在"""
        # 先设置临时工作区环境变量
        original_env = os.environ.get("WORKSPACE_ROOT")
        os.environ["WORKSPACE_ROOT"] = str(self.base_path)
        
        try:
            # 重新导入以刷新环境变量
            from importlib import reload
            import app.core.path_utils
            reload(app.core.path_utils)
            from app.core.path_utils import ensure_session_dir
            
            session_id = f"ses_{uuid.uuid4().hex}"
            path = ensure_session_dir(session_id)
            assert path.exists()
            assert path.is_dir()
    
            # 重复调用不会报错
            path2 = ensure_session_dir(session_id)
            assert path2 == path
        finally:
            if original_env:
                os.environ["WORKSPACE_ROOT"] = original_env
            else:
                del os.environ["WORKSPACE_ROOT"]

    def test_safe_join_case_sensitivity(self):
        """测试大小写敏感路径处理"""
        # 创建混合大小写目录
        mixed_dir = self.base_path / "TestDir"
        mixed_dir.mkdir()

        result = safe_join(self.base_path, "testdir")
        # 在Windows上不区分大小写，在Linux上区分
        if os.name == 'nt':
            assert result.resolve() == mixed_dir.resolve()
        else:
            assert result != mixed_dir

    def test_safe_join_empty_components(self):
        """测试空路径组件处理"""
        result = safe_join(self.base_path, "", "test", "", "file.txt")
        assert result == self.base_path / "test" / "file.txt"

    def test_safe_join_special_characters(self):
        """测试特殊字符路径处理"""
        # Windows不允许路径中包含某些特殊字符，使用安全的特殊字符测试
        special_path = "test with spaces and_safe-special.chars"
        result = safe_join(self.base_path, special_path)
        assert result.name == special_path

    def test_get_user_workspace_root_uses_hidden_directory_under_home(self):
        """测试用户级持久工作区根目录命名"""
        root = get_user_workspace_root()
        assert root.name == "boxteam_workspace"
        assert root.parent == Path.home().resolve() / ".boxteams"

    def test_global_paths_share_boxteam_home(self, tmp_path, monkeypatch):
        boxteam_home = tmp_path / "boxteam-home"
        monkeypatch.setenv("BOXTEAM_HOME", str(boxteam_home))
        monkeypatch.delenv("BOXTEAM_USER_WORKSPACE_ROOT", raising=False)
        monkeypatch.delenv("BOXTEAM_GATEWAY_ROOT", raising=False)

        assert get_boxteam_home() == boxteam_home.resolve()
        assert get_user_config_root() == boxteam_home.resolve() / "config"
        assert get_gateway_root() == boxteam_home.resolve() / "state" / "gateway"
        assert get_user_workspace_root() == boxteam_home.resolve() / "boxteam_workspace"

    def test_initialize_directories_migrates_session_related_files(self, tmp_path, monkeypatch):
        workspace_root = tmp_path / "workspace"
        monkeypatch.setenv("WORKSPACE_ROOT", str(workspace_root))
        session_id = "ses_migrate"
        boxteam_root = workspace_root / ".boxteam"
        session_root = boxteam_root / "sessions" / session_id
        session_root.mkdir(parents=True)
        (session_root / "session.json").write_text("{}", encoding="utf-8")
        legacy_checkpoint = boxteam_root / "checkpoints" / session_id
        legacy_checkpoint.mkdir(parents=True)
        (legacy_checkpoint / "checkpoints.jsonl").write_text("{}\n", encoding="utf-8")
        legacy_trace = boxteam_root / "logs" / "traces"
        legacy_trace.mkdir(parents=True)
        (legacy_trace / f"trace_{session_id}.jsonl").write_text("{}\n", encoding="utf-8")
        orphaned_checkpoint = boxteam_root / "checkpoints" / "ses_orphaned"
        orphaned_checkpoint.mkdir(parents=True)
        (orphaned_checkpoint / "checkpoints.jsonl").write_text("{}\n", encoding="utf-8")

        initialize_directories()

        assert (session_root / "checkpoints" / "checkpoints.jsonl").is_file()
        assert (session_root / "logs" / "traces" / "events.jsonl").is_file()
        assert not legacy_checkpoint.exists()
        assert (
            boxteam_root
            / "orphaned"
            / "legacy-checkpoints"
            / "ses_orphaned"
            / "checkpoints.jsonl"
        ).is_file()

    def test_migrate_user_storage_layout_moves_global_data(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        boxteam_home = home / ".boxteams"
        default_workspace = boxteam_home / "boxteam_workspace"
        monkeypatch.setenv("BOXTEAM_HOME", str(boxteam_home))
        legacy_config = home / ".boxteam"
        legacy_config.mkdir(parents=True)
        (legacy_config / "boxteam.jsonc").write_text("{}", encoding="utf-8")
        legacy_gateway = default_workspace / ".boxteam" / "gateway"
        legacy_gateway.mkdir(parents=True)
        (legacy_gateway / "workspaces.json").write_text("{}", encoding="utf-8")

        migrate_user_storage_layout(
            home=home,
            boxteam_home=boxteam_home,
            default_workspace_root=default_workspace,
        )

        assert (boxteam_home / "config" / "boxteam.jsonc").is_file()
        assert (boxteam_home / "state" / "gateway" / "workspaces.json").is_file()
        assert not legacy_gateway.exists()
