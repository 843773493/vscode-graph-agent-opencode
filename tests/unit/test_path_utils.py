import os
import tempfile
import json
from datetime import UTC, datetime
from pathlib import Path
import pytest
from app.core.path_utils import (
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
from app.core.session_paths import SessionPathResolver, physical_segment


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

    def test_get_session_path_resolves_physical_tree(self, tmp_path, monkeypatch):
        """稳定 ID 应解析到真实层级目录，而不是拼接固定扁平路径。"""
        workspace_root = tmp_path / "workspace"
        monkeypatch.setenv("WORKSPACE_ROOT", str(workspace_root))

        from app.core.path_utils import get_session_path_resolver

        initialize_directories()
        resolver = get_session_path_resolver()
        folder = resolver.create_folder(name="项目会话", parent_node_id=None)
        session_id = "ses_test_session_12345678"
        session_dir = resolver.allocate_session_dir(
            session_id=session_id,
            title="测试会话",
            parent_node_id=folder.node_id,
        )
        now = datetime.now(UTC).isoformat()
        (session_dir / "session.json").write_text(
            json.dumps(
                {
                    "session_id": session_id,
                    "title": "测试会话",
                    "created_at": now,
                    "updated_at": now,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        resolver.register_session(session_id, session_dir)

        path = get_session_path(session_id)

        assert path == session_dir
        assert path.parent == folder.path
        assert path.name == "测试会话--12345678"
        assert path != workspace_root / ".boxteam" / "sessions" / session_id

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
        target_folder = resolver.create_folder(name="移动后", parent_node_id=None)
        session_id = "ses_manual_move_12345678"
        source = resolver.allocate_session_dir(
            session_id=session_id,
            title="手工移动",
            parent_node_id=source_folder.node_id,
        )
        now = datetime.now(UTC).isoformat()
        (source / "session.json").write_text(
            json.dumps(
                {
                    "session_id": session_id,
                    "title": "手工移动",
                    "created_at": now,
                    "updated_at": now,
                }
            ),
            encoding="utf-8",
        )
        resolver.register_session(session_id, source)
        target = target_folder.path / source.name

        source.replace(target)

        assert get_session_path(session_id) == target

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

    def test_physical_segment_is_windows_safe_and_stable(self):
        assert physical_segment("CON", "fld_12345678") == "_CON--12345678"
        assert physical_segment('日报<>:"/\\|?*', "ses_abcdefgh") == (
            "日报_________--abcdefgh"
        )
        assert physical_segment("名称. ", "ses_12345678") == "名称--12345678"

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
        now = datetime.now(UTC).isoformat()
        (session_root / "session.json").write_text(
            json.dumps(
                {
                    "session_id": session_id,
                    "title": "迁移会话",
                    "created_at": now,
                    "updated_at": now,
                }
            ),
            encoding="utf-8",
        )
        (session_root / "pending_requests.json").write_text(
            json.dumps(
                {
                    "file_id": (
                        f"/.boxteam/sessions/{session_id}/attachments/legacy.png"
                    ),
                    "read_path": (
                        f"{workspace_root}/.boxteam/sessions/{session_id}/"
                        "tool-results/legacy.txt"
                    ),
                }
            ),
            encoding="utf-8",
        )
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

        migrated_session_root = get_session_path(session_id)
        assert migrated_session_root != session_root
        assert (migrated_session_root / "checkpoints" / "checkpoints.jsonl").is_file()
        assert (migrated_session_root / "logs" / "traces" / "events.jsonl").is_file()
        migrated_references = json.loads(
            (migrated_session_root / "pending_requests.json").read_text(
                encoding="utf-8"
            )
        )
        assert migrated_references == {
            "file_id": (
                f"boxteam-session://{session_id}/attachments/legacy.png"
            ),
            "read_path": (
                f"/session-artifacts/{session_id}/tool-results/legacy.txt"
            ),
        }
        assert not legacy_checkpoint.exists()
        assert (
            boxteam_root
            / "orphaned"
            / "legacy-checkpoints"
            / "ses_orphaned"
            / "checkpoints.jsonl"
        ).is_file()

    def test_session_layout_migration_reuses_unlocked_advisory_lock(self, tmp_path):
        sessions_root = tmp_path / ".boxteam" / "sessions"
        migrations_root = tmp_path / ".boxteam" / "migrations"
        migrations_root.mkdir(parents=True)
        lock_path = migrations_root / "session-physical-layout-v1.lock"
        lock_path.write_text(
            json.dumps({"pid": 2_147_483_647, "started_at": "2026-01-01T00:00:00Z"}),
            encoding="utf-8",
        )

        SessionPathResolver(sessions_root).initialize()

        assert lock_path.exists()
        owner = json.loads(lock_path.read_text(encoding="utf-8"))
        assert owner["pid"] == os.getpid()
        record = json.loads(
            (migrations_root / "session-physical-layout-v1.json").read_text(
                encoding="utf-8"
            )
        )
        assert record["status"] == "completed"

    def test_parent_manifest_migrates_to_physical_children_tree_and_detects_drift(
        self,
        tmp_path,
    ):
        sessions_root = tmp_path / ".boxteam" / "sessions"
        parent_id = "ses_parent_physical_12345678"
        child_id = "ses_child_physical_87654321"
        now = datetime.now(UTC).isoformat()
        for session_id, title, parent_session_id, kind in (
            (parent_id, "父会话", None, "normal"),
            (child_id, "子会话", parent_id, "context_fork"),
        ):
            session_dir = sessions_root / session_id
            session_dir.mkdir(parents=True)
            (session_dir / "session.json").write_text(
                json.dumps(
                    {
                        "session_id": session_id,
                        "workspace_id": "ws_local",
                        "title": title,
                        "parent_session_id": parent_session_id,
                        "kind": kind,
                        "created_at": now,
                        "updated_at": now,
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

        resolver = SessionPathResolver(sessions_root)
        resolver.initialize()

        parent_path = resolver.resolve_session_dir(parent_id)
        child_path = resolver.resolve_session_dir(child_id)
        assert child_path.parent == parent_path / "children"
        assert resolver.get_node(child_id).parent_node_id == parent_id
        migrated_child_manifest = json.loads(
            (child_path / "session.json").read_text(encoding="utf-8")
        )
        assert migrated_child_manifest["context_source_session_id"] == parent_id
        migration_record = json.loads(
            (
                sessions_root.parent
                / "migrations"
                / "session-physical-parents-v2.json"
            ).read_text(encoding="utf-8")
        )
        assert migration_record["status"] == "completed"

        migrated_child_manifest["parent_session_id"] = None
        (child_path / "session.json").write_text(
            json.dumps(migrated_child_manifest, ensure_ascii=False),
            encoding="utf-8",
        )
        with pytest.raises(RuntimeError, match="与物理祖先不一致"):
            resolver.refresh()

    def test_resolver_detects_manual_session_manifest_update(
        self,
        tmp_path,
    ):
        sessions_root = tmp_path / ".boxteam" / "sessions"
        resolver = SessionPathResolver(sessions_root)
        resolver.initialize()
        session_id = "ses_manifest_refresh_12345678"
        session_dir = resolver.allocate_session_dir(
            session_id=session_id,
            title="修改前",
        )
        now = datetime.now(UTC).isoformat()
        manifest_path = session_dir / "session.json"
        manifest_path.write_text(
            json.dumps(
                {
                    "session_id": session_id,
                    "title": "修改前",
                    "created_at": now,
                    "updated_at": now,
                }
            ),
            encoding="utf-8",
        )
        resolver.register_session(session_id, session_dir)
        revision_before = resolver.revision
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["title"] = "人工修改后"
        manifest["updated_at"] = datetime.now(UTC).isoformat()
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        assert resolver.get_node(session_id).name == "人工修改后"
        assert resolver.revision > revision_before

    def test_resolver_recovers_empty_stale_allocation_directory(self, tmp_path):
        sessions_root = tmp_path / ".boxteam" / "sessions"
        resolver = SessionPathResolver(sessions_root)
        resolver.initialize()
        session_dir = resolver.allocate_session_dir(
            session_id="ses_stale_alloc_12345678",
            title="中断创建",
        )
        marker_path = session_dir / ".boxteam-session-allocating.json"
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        marker["pid"] = 2_147_483_647
        marker["process_identity"] = "stale-process"
        marker_path.write_text(json.dumps(marker), encoding="utf-8")

        SessionPathResolver(sessions_root).initialize()

        assert not session_dir.exists()

    def test_subtree_delete_freezes_create_allocate_and_move(self, tmp_path):
        sessions_root = tmp_path / ".boxteam" / "sessions"
        resolver = SessionPathResolver(sessions_root)
        resolver.initialize()
        deleting = resolver.create_folder(name="待删除", parent_node_id=None)
        outside = resolver.create_folder(name="外部", parent_node_id=None)
        movable = resolver.create_folder(name="准备移动", parent_node_id=None)

        resolver.begin_subtree_delete(deleting.node_id)
        try:
            with pytest.raises(RuntimeError, match="正在递归删除"):
                resolver.create_folder(
                    name="竞态子目录",
                    parent_node_id=deleting.node_id,
                )
            with pytest.raises(RuntimeError, match="正在递归删除"):
                resolver.allocate_session_dir(
                    session_id="ses_delete_race_12345678",
                    title="竞态会话",
                    parent_node_id=deleting.node_id,
                )
            with pytest.raises(RuntimeError, match="正在递归删除"):
                resolver.move_node(
                    node_id=movable.node_id,
                    parent_node_id=deleting.node_id,
                )
            with pytest.raises(RuntimeError, match="正在递归删除"):
                resolver.move_node(
                    node_id=deleting.node_id,
                    parent_node_id=outside.node_id,
                )
            with pytest.raises(RuntimeError, match="正在递归删除"):
                resolver.delete_folder(deleting.node_id)
        finally:
            resolver.finish_subtree_delete(deleting.node_id)

        created = resolver.create_folder(
            name="删除失败后可继续",
            parent_node_id=deleting.node_id,
        )
        assert created.parent_node_id == deleting.node_id

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
