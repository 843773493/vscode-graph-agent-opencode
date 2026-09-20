import json
import os
import tempfile
from pathlib import Path

import pytest

from app.core.exceptions import ForbiddenError
from app.core.path_segments import physical_segment
from app.core.path_utils import (
    get_boxteam_home,
    get_gateway_root,
    get_session_path,
    get_sessions_dir,
    get_user_config_root,
    get_user_gateway_config_path,
    get_user_gateway_local_config_path,
    get_user_gateway_schema_path,
    get_user_workspace_config_path,
    get_user_workspace_local_config_path,
    get_user_workspace_root,
    get_user_workspace_schema_path,
    initialize_directories,
    safe_join,
)
from app.core.storage_migration import (
    migrate_legacy_trace_timestamps,
    migrate_user_storage_layout,
)
from tests.support.catalog_session_bundle import seed_catalog_session_bundle


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
            assert (
                safe_join(self.base_path, "..\\windows\\system32").parent
                == self.base_path
            )

    def test_safe_join_absolute_path_attack(self):
        """测试绝对路径攻击防护"""
        with pytest.raises(ForbiddenError, match="Path traversal detected"):
            safe_join(self.base_path, "/etc/passwd")

        if os.name == "nt":
            with pytest.raises(ForbiddenError, match="Path traversal detected"):
                safe_join(self.base_path, "C:\\windows\\system32")
        else:
            assert (
                safe_join(self.base_path, "C:\\windows\\system32").parent
                == self.base_path
            )

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

    def test_get_session_path_resolves_physical_tree(self, tmp_path, monkeypatch):
        """稳定 ID 应解析到真实层级目录，而不是拼接固定扁平路径。"""
        workspace_root = tmp_path / "workspace"
        monkeypatch.setenv("WORKSPACE_ROOT", str(workspace_root))

        from app.core.path_utils import get_session_path_resolver

        initialize_directories()
        resolver = get_session_path_resolver()
        folder_name = '项目/会话:*?"<>'
        session_title = '测试/会话:*?"<>'
        folder = resolver.create_folder(name=folder_name, parent_node_id=None)
        # R17：canonical ID（一次性 uuid4 形态常量，直接写入；非任务书
        # md5 映射 ses_test_session_12345678 的产物——该函数反算不出此值，
        # 生成脚本未留存，R17 审查 E3/处置必改 2 更正）
        session_id = "ses_58a5607fd562454a932d851c95b73cc4"
        session_dir = seed_catalog_session_bundle(
            workspace_root / ".boxteam" / "sessions",
            session_id,
            title=session_title,
            parent_node_id=folder.node_id,
        ).directory

        path = get_session_path(session_id)

        assert path == session_dir
        assert path.name == session_id
        assert path != workspace_root / ".boxteam" / "sessions" / session_id
        # catalog 节点的文件夹不占用物理目录，会话目录使用日期桶布局。
        node = resolver.get_node(session_id)
        assert node.parent_node_id == folder.node_id
        assert node.name == session_title
        assert not hasattr(folder, "path")
        assert path.parent.parent.parent.parent == get_sessions_dir()
        with pytest.raises(RuntimeError, match="folder 无物理目录"):
            resolver.resolve_folder_dir(folder.node_id)

    def test_get_session_path_rejects_unknown_session(self, tmp_path, monkeypatch):
        workspace_root = tmp_path / "workspace"
        monkeypatch.setenv("WORKSPACE_ROOT", str(workspace_root))
        initialize_directories()

        with pytest.raises(FileNotFoundError, match="会话物理目录不存在"):
            get_session_path("ses_missing")

    def test_get_session_path_detects_manual_directory_move(
        self,
        tmp_path,
        monkeypatch,
    ):
        workspace_root = tmp_path / "workspace"
        monkeypatch.setenv("WORKSPACE_ROOT", str(workspace_root))
        initialize_directories()
        from app.core.path_utils import get_session_path_resolver

        resolver = get_session_path_resolver()
        source_folder = resolver.create_folder(name="移动前", parent_node_id=None)
        resolver.create_folder(name="移动后", parent_node_id=None)
        # R17：canonical ID（一次性 uuid4 形态常量，直接写入；非任务书
        # md5 映射 ses_manual_move_12345678 的产物，R17 处置必改 2 更正）
        session_id = "ses_5ce2590d35c74fd9a71e8d7526be328c"
        source = seed_catalog_session_bundle(
            workspace_root / ".boxteam" / "sessions",
            session_id,
            title="手工移动",
            parent_node_id=source_folder.node_id,
        ).directory
        # 手工挪走日期桶目录后解析必须 fail closed。
        target = tmp_path / "手工挪走" / source.name
        target.parent.mkdir(parents=True, exist_ok=True)
        source.replace(target)

        with pytest.raises(RuntimeError, match="会话物理目录缺失"):
            get_session_path(session_id)

    def test_safe_join_case_sensitivity(self):
        """测试大小写敏感路径处理"""
        # 创建混合大小写目录
        mixed_dir = self.base_path / "TestDir"
        mixed_dir.mkdir()

        result = safe_join(self.base_path, "testdir")
        # 在Windows上不区分大小写，在Linux上区分
        if os.name == "nt":
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

    def test_physical_segment_is_windows_safe_and_stable(self):
        assert physical_segment("CON", "fld_12345678") == "fld_12345678"
        assert physical_segment('日报<>:"/\\|?*', "ses_abcdefgh") == "ses_abcdefgh"
        assert physical_segment("名称. ", "ses_12345678") == "ses_12345678"

    def test_get_user_workspace_root_uses_hidden_directory_under_home(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """测试用户级持久工作区根目录命名"""
        monkeypatch.delenv("BOXTEAM_HOME")
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
        assert get_user_gateway_config_path() == boxteam_home / "config/gateway.jsonc"
        assert get_user_gateway_local_config_path() == (
            boxteam_home / "config/gateway_local.jsonc"
        )
        assert get_user_gateway_schema_path() == (
            boxteam_home / "config/gateway_schema.jsonc"
        )
        assert get_user_workspace_config_path() == (
            boxteam_home / "config/workspace.jsonc"
        )
        assert get_user_workspace_local_config_path() == (
            boxteam_home / "config/workspace_local.jsonc"
        )
        assert get_user_workspace_schema_path() == (
            boxteam_home / "config/workspace_schema.jsonc"
        )
        assert get_gateway_root() == boxteam_home.resolve() / "state" / "gateway"
        assert get_user_workspace_root() == boxteam_home.resolve() / "boxteam_workspace"

    def test_migrate_legacy_trace_timestamps_keeps_backup(self, tmp_path):
        boxteam_root = tmp_path / ".boxteam"
        sessions_root = boxteam_root / "sessions"
        trace_file = sessions_root / "session-1" / "logs" / "traces" / "events.jsonl"
        trace_file.parent.mkdir(parents=True)
        legacy_event = {
            "event_id": "evt_legacy",
            "job_id": "job_legacy",
            "step_id": None,
            "agent_id": "job_service",
            "timestamp": "2026-06-29T02:07:35.569434",
            "type": "job_created",
            "payload": {
                "session_id": "ses_legacy",
                "message": "旧消息",
                "agent_id": "default",
            },
        }
        trace_file.write_text(
            json.dumps(legacy_event, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

        migrate_legacy_trace_timestamps(
            boxteam_root=boxteam_root,
            sessions_root=sessions_root,
        )

        migrated_event = json.loads(trace_file.read_text(encoding="utf-8"))
        assert migrated_event["timestamp"] == "2026-06-29T02:07:35.569434+00:00"
        backup_file = (
            boxteam_root
            / "migrations"
            / "trace-timestamps-v1-backup"
            / "session-1"
            / "logs"
            / "traces"
            / "events.jsonl"
        )
        assert json.loads(backup_file.read_text(encoding="utf-8"))["timestamp"] == (
            "2026-06-29T02:07:35.569434"
        )
        assert (
            json.loads(
                (boxteam_root / "migrations" / "trace-timestamps-v1.json").read_text(
                    encoding="utf-8"
                )
            )["normalized_timestamps"]
            == 1
        )

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
