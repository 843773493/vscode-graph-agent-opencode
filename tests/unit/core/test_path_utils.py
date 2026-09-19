import json
import os
import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.core.exceptions import ForbiddenError
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
from app.core.session_catalog_resolver import SessionCatalogPathResolver
from app.core.session_paths import SessionPathResolver, physical_segment
from app.core.storage_migration import (
    migrate_legacy_trace_timestamps,
    migrate_user_storage_layout,
)


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
        session_dir = resolver.allocate_session_dir(
            session_id=session_id,
            title=session_title,
            parent_node_id=folder.node_id,
        )
        now = datetime.now(UTC).isoformat()
        (session_dir / "session.json").write_text(
            json.dumps(
                {
                    "session_id": session_id,
                    "title": session_title,
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
        assert path.name == session_id
        assert path != workspace_root / ".boxteam" / "sessions" / session_id
        if isinstance(resolver, SessionCatalogPathResolver):
            # 新模型：folder 无物理目录（catalog-only 节点），session 物理
            # 目录在日期桶下；权威索引是 SQLite catalog，JSON 断言不适用。
            node = resolver.get_node(session_id)
            assert node.parent_node_id == folder.node_id
            assert node.name == session_title
            assert folder.path is None
            # 新模型：物理目录在日期桶（sessions/YYYY/MM/DD/<session_id>）。
            assert path.parent.parent.parent.parent == get_sessions_dir()
            assert resolver.get_node(folder.node_id).updated_at is None
        else:
            assert path.parent == folder.path
            assert folder.path.name == folder.node_id
            stored_index = json.loads(
                resolver.index_path.read_text(encoding="utf-8")
            )
            assert stored_index["schema_version"] == 3
            names_by_id = {
                node["node_id"]: node["name"] for node in stored_index["nodes"]
            }
            assert names_by_id[folder.node_id] == folder_name
            assert names_by_id[session_id] == session_title

    def test_get_session_path_rejects_unknown_session(self, tmp_path, monkeypatch):
        workspace_root = tmp_path / "workspace"
        monkeypatch.setenv("WORKSPACE_ROOT", str(workspace_root))
        initialize_directories()

        with pytest.raises(FileNotFoundError, match="会话物理目录不存在"):
            get_session_path("ses_missing")

    def test_resolver_rejects_rebuilding_deleted_authoritative_index(self, tmp_path):
        sessions_root = tmp_path / ".boxteam" / "sessions"
        resolver = SessionPathResolver(sessions_root)
        resolver.initialize()
        resolver.create_folder(name="保留目录", parent_node_id=None)
        assert resolver.authority_marker_path.is_file()
        resolver.index_path.unlink()

        with pytest.raises(RuntimeError, match="拒绝根据磁盘目录重建"):
            SessionPathResolver(sessions_root).initialize()

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
        # R17：canonical ID（一次性 uuid4 形态常量，直接写入；非任务书
        # md5 映射 ses_manual_move_12345678 的产物，R17 处置必改 2 更正）
        session_id = "ses_5ce2590d35c74fd9a71e8d7526be328c"
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
        if isinstance(resolver, SessionCatalogPathResolver):
            # 新模型：folder 无物理目录；手工挪走日期桶目录后解析必须
            # fail closed（防篡改收敛到物理解析点，不扫盘比对）。
            target = tmp_path / "手工挪走" / source.name
        else:
            target = target_folder.path / source.name
        target.parent.mkdir(parents=True, exist_ok=True)
        source.replace(target)

        if isinstance(resolver, SessionCatalogPathResolver):
            with pytest.raises(RuntimeError, match="会话物理目录缺失"):
                get_session_path(session_id)
        else:
            with pytest.raises(RuntimeError, match="绕过软件修改会话目录结构"):
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

    def test_initialize_directories_migrates_session_related_files(
        self, tmp_path, monkeypatch
    ):
        workspace_root = tmp_path / "workspace"
        monkeypatch.setenv("WORKSPACE_ROOT", str(workspace_root))
        # R17：session ID 按 canonical 口径取值（一次性 uuid4 形态常量，
        # 非任务书 md5 映射 ses_migrate 的产物，R17 处置必改 2 更正）——
        # catalog 模式迁移机器对非 canonical ID 的会话 quarantine（illegal_id），
        # 该用例的语义（布局迁移收拢 + catalog 化）要求 canonical ID。
        session_id = "ses_8bb1585f58a042dd8ae7bcdb18ad2c4c"
        boxteam_root = workspace_root / ".boxteam"
        session_root = boxteam_root / "sessions" / "迁移会话--migrate"
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
        (legacy_trace / f"trace_{session_id}.jsonl").write_text(
            "{}\n", encoding="utf-8"
        )
        orphaned_checkpoint = boxteam_root / "checkpoints" / "ses_orphaned"
        orphaned_checkpoint.mkdir(parents=True)
        (orphaned_checkpoint / "checkpoints.jsonl").write_text("{}\n", encoding="utf-8")

        initialize_directories()
        if os.environ.get("BOXTEAM_SESSION_CATALOG_RESOLVER") not in ("0", "legacy"):
            # catalog 模式引导（R19 起为默认，仅显式 legacy opt-in 时跳过）：
            # 布局迁移后先以旧 resolver 吸收物理树建旧权威 index，再用
            # SessionCatalogMigrator 一次性导入 SQLite（与生产切换的维护
            # 窗口顺序一致）；legacy 模式无需此段。
            import asyncio

            from app.core.session_catalog_migration import SessionCatalogMigrator
            from app.core.session_paths import SessionPathResolver
            from app.core.workspace_identity import load_or_create_workspace_id

            SessionPathResolver(boxteam_root / "sessions").initialize()
            migrator = SessionCatalogMigrator(
                workspace_id=load_or_create_workspace_id(workspace_root),
                sessions_root=boxteam_root / "sessions",
                database_path=(
                    boxteam_root / "navigation" / "session-catalog.sqlite"
                ),
                maintenance_root=boxteam_root / "maintenance",
            )
            migration_result = asyncio.run(migrator.migrate())
            assert migration_result.migrated_session_nodes == 1

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
            "file_id": (f"boxteam-session://{session_id}/attachments/legacy.png"),
            "read_path": (f"session-artifacts/{session_id}/tool-results/legacy.txt"),
        }
        assert not legacy_checkpoint.exists()
        assert (
            boxteam_root
            / "orphaned"
            / "legacy-checkpoints"
            / "ses_orphaned"
            / "checkpoints.jsonl"
        ).is_file()

    def test_initialize_keeps_unrecoverable_inline_attachment_sessions(
        self, tmp_path, monkeypatch
    ):
        """历史会话的坏 inline 附件不能阻断整个工作区启动。"""
        workspace_root = tmp_path / "workspace"
        monkeypatch.setenv("WORKSPACE_ROOT", str(workspace_root))
        session_id = "ses_bad_inline_video"
        boxteam_root = workspace_root / ".boxteam"
        session_root = boxteam_root / "sessions" / session_id
        session_root.mkdir(parents=True)
        now = datetime.now(UTC).isoformat()
        (session_root / "session.json").write_text(
            json.dumps(
                {
                    "session_id": session_id,
                    "title": "包含失效视频附件的会话",
                    "created_at": now,
                    "updated_at": now,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        (session_root / "message_history").mkdir()
        (session_root / "message_history" / "messages.jsonl").write_text(
            json.dumps(
                {
                    "file_id": (
                        "inline:9e7307e5-d2ca-45a6-b0ea-64ce126a3864:"
                        "codex-ux-video-a.mp4"
                    )
                }
            )
            + "\n",
            encoding="utf-8",
        )
        mime_mismatch_session_id = "ses_bad_mime"
        mime_mismatch_root = boxteam_root / "sessions" / mime_mismatch_session_id
        (mime_mismatch_root / "logs" / "llm_requests").mkdir(parents=True)
        (mime_mismatch_root / "session.json").write_text(
            json.dumps(
                {
                    "session_id": mime_mismatch_session_id,
                    "title": "包含 MIME 不一致附件的会话",
                    "created_at": now,
                    "updated_at": now,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        (mime_mismatch_root / "logs" / "llm_requests" / "request.json").write_text(
            json.dumps(
                {
                    "request": {
                        "messages": [
                            {
                                "content": [
                                    {
                                        "type": "image_url",
                                        "image_url": {
                                            "url": "data:image/jpeg;base64,AA=="
                                        },
                                    }
                                ],
                                "response_metadata": {
                                    "attachments": [
                                        {
                                            "file_id": "inline:bad-mime.png",
                                            "content_type": "image/png",
                                        }
                                    ]
                                },
                            }
                        ]
                    }
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        migrations_root = boxteam_root / "migrations"
        migrations_root.mkdir(parents=True)
        (migrations_root / "session-stable-locators-v1.json").write_text(
            json.dumps({"schema_version": 1, "status": "completed"}),
            encoding="utf-8",
        )

        resolver = SessionPathResolver(boxteam_root / "sessions")
        resolver.initialize()

        migration_record = json.loads(
            (
                migrations_root / "session-inline-attachments-v1.json"
            ).read_text(encoding="utf-8")
        )
        assert migration_record["status"] == "completed_with_errors"
        assert migration_record["errors"] == [
            {
                "session_id": session_id,
                "error": (
                    "旧会话数据引用了无法从请求日志恢复的 inline 附件: "
                    f"session_id={session_id}, file_id="
                    "'inline:9e7307e5-d2ca-45a6-b0ea-64ce126a3864:"
                    "codex-ux-video-a.mp4'"
                ),
            },
            {
                "session_id": mime_mismatch_session_id,
                "error": (
                    "旧会话附件日志中的 MIME 类型不一致: "
                    "file_id='inline:bad-mime.png', declared='image/png', "
                    "actual='image/jpeg'"
                ),
            },
        ]
        assert resolver.resolve_session_node(session_id) == session_root
        assert resolver.resolve_session_node(mime_mismatch_session_id) == (
            mime_mismatch_root
        )
        assert resolver.legacy_inline_attachment_migration_record == migration_record

        # 降级记录是终态；下一次启动不应再次因同一个历史会话失败。
        SessionPathResolver(boxteam_root / "sessions").initialize()

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
        assert json.loads(
            (boxteam_root / "migrations" / "trace-timestamps-v1.json").read_text(
                encoding="utf-8"
            )
        )["normalized_timestamps"] == 1

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

        parent_path = resolver.resolve_session_node(parent_id)
        child_path = resolver.resolve_session_node(child_id)
        assert parent_path.is_absolute()
        assert child_path.is_absolute()
        assert child_path.parent == parent_path / "children"
        assert resolver.get_node(child_id).parent_node_id == parent_id
        migrated_child_manifest = json.loads(
            (child_path / "session.json").read_text(encoding="utf-8")
        )
        assert migrated_child_manifest["context_source_session_id"] == parent_id
        migration_record = json.loads(
            (
                sessions_root.parent / "migrations" / "session-physical-parents-v2.json"
            ).read_text(encoding="utf-8")
        )
        assert migration_record["status"] == "completed"

        migrated_child_manifest["parent_session_id"] = None
        (child_path / "session.json").write_text(
            json.dumps(migrated_child_manifest, ensure_ascii=False),
            encoding="utf-8",
        )
        with pytest.raises(RuntimeError, match="与权威索引父关系不一致"):
            resolver.refresh()

    def test_child_session_summary_counts_logical_children_without_loading_manifests(
        self,
        tmp_path,
    ):
        sessions_root = tmp_path / ".boxteam" / "sessions"
        resolver = SessionPathResolver(sessions_root)
        resolver.initialize()
        now = datetime.now(UTC).isoformat()

        def create_session(
            session_id: str,
            title: str,
            parent_node_id: str | None,
            parent_session_id: str | None,
        ) -> None:
            session_dir = resolver.allocate_session_dir(
                session_id=session_id,
                title=title,
                parent_node_id=parent_node_id,
            )
            (session_dir / "session.json").write_text(
                json.dumps(
                    {
                        "session_id": session_id,
                        "workspace_id": "ws_local",
                        "title": title,
                        "parent_session_id": parent_session_id,
                        "created_at": now,
                        "updated_at": now,
                    }
                ),
                encoding="utf-8",
            )
            resolver.register_session(session_id, session_dir)

        parent_id = "ses_summary_parent_12345678"
        child_one_id = "ses_summary_child_one_12345678"
        child_two_id = "ses_summary_child_two_12345678"
        nested_child_id = "ses_summary_nested_child_12345678"
        create_session(parent_id, "父会话", None, None)
        first_folder = resolver.create_folder(
            name="第一个子树",
            parent_node_id=parent_id,
        )
        second_folder = resolver.create_folder(
            name="第二个子树",
            parent_node_id=parent_id,
        )
        create_session(child_one_id, "子会话一", first_folder.node_id, parent_id)
        create_session(child_two_id, "子会话二", second_folder.node_id, parent_id)
        nested_folder = resolver.create_folder(
            name="孙会话目录",
            parent_node_id=child_one_id,
        )
        create_session(
            nested_child_id,
            "孙会话",
            nested_folder.node_id,
            child_one_id,
        )

        child_count, child_ids, truncated = resolver.child_session_summary(
            parent_id,
            limit=1,
        )

        assert child_count == 2
        assert len(child_ids) == 1
        assert child_ids[0] in {child_one_id, child_two_id}
        assert truncated is True
        with pytest.raises(ValueError, match="limit 必须大于 0"):
            resolver.child_session_summary(parent_id, limit=0)

    def test_runtime_session_resolution_ignores_unrelated_physical_drift(
        self,
        tmp_path,
    ):
        sessions_root = tmp_path / ".boxteam" / "sessions"
        resolver = SessionPathResolver(sessions_root)
        resolver.initialize()
        session_id = "ses_runtime_resolve_12345678"
        session_dir = resolver.allocate_session_dir(
            session_id=session_id,
            title="运行时收尾",
        )
        now = datetime.now(UTC).isoformat()
        (session_dir / "session.json").write_text(
            json.dumps(
                {
                    "session_id": session_id,
                    "title": "运行时收尾",
                    "created_at": now,
                    "updated_at": now,
                }
            ),
            encoding="utf-8",
        )
        resolver.register_session(session_id, session_dir)

        indexed_path = resolver.resolve_session_node(session_id)
        shutil.copytree(indexed_path, sessions_root / "ses_unindexed_12345678")

        with pytest.raises(RuntimeError, match="权威索引与磁盘目录不一致"):
            resolver.refresh()

        restarted = SessionPathResolver(sessions_root)
        restarted.initialize()
        with pytest.raises(RuntimeError, match="权威索引与磁盘目录不一致"):
            restarted.list_nodes()
        assert restarted.resolve_session_node_for_runtime(session_id) == indexed_path

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

        assert resolver.get_node(session_id).name == "修改前"
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

    @pytest.mark.asyncio
    async def test_subtree_delete_freezes_create_allocate_and_move(self, tmp_path):
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
            await resolver.finish_subtree_delete(deleting.node_id)

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
