"""独立进程 os._exit 覆盖原上传 operation 的 durable acceptance 与发布恢复。"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock
from uuid import uuid4

import pytest

from app.services.infrastructure.config_service import ConfigService
from app.services.infrastructure.workspace_service import WorkspaceService
from tests.harness.python.run_context import TestRunContext
from tests.support.workspaces import prepare_default_test_workspace

PROBE = r'''
import asyncio, io, os, sys
from pathlib import Path
from unittest.mock import Mock, patch
from app.schemas.internal_v2.workspace_file_operations import WorkspaceFileMutationIntentDTO
from app.services.infrastructure.config_service import ConfigService
from app.services.infrastructure.workspace_service import WorkspaceService
from app.services.infrastructure.workspace_files import operations

async def main():
    workspace, boundary, operation_id = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
    config = Mock(spec=ConfigService)
    config.get_workspace_file_default_limit.return_value = 500
    service = WorkspaceService(config_service=config, workspace_root=workspace)
    await service.start()
    owner = service.file_operations
    snapshot = await service.list_files(path="target")
    intent = WorkspaceFileMutationIntentDTO(client_operation_id=operation_id, client_sequence=1,
        directory_key=snapshot.directory_key, expected_directory_revision=snapshot.directory_revision)
    accept, check, publish, settle = owner.store.accept, owner._check_preconditions, operations.publish_step, owner._settle
    checks = publications = 0
    def acceptance(**values):
        if boundary == "before_accept": os._exit(86)
        receipt = accept(**values)
        if boundary == "after_accept": os._exit(86)
        return receipt
    def preconditions(record):
        nonlocal checks
        checks += 1
        check(record)
        if boundary == "after_staging" and checks == 2: os._exit(86)
    def publication(stage, target, kind):
        nonlocal publications
        publications += 1
        if boundary == "publishing_before": os._exit(86)
        if boundary == "partial" and publications == 2:
            with target.open("xb") as output:
                output.write(b"external"); output.flush(); os.fsync(output.fileno())
            os._exit(86)
        publish(stage, target, kind)
    def terminal(record, **values):
        if boundary == "after_effect": os._exit(86)
        return settle(record, **values)
    def cleanup(record):
        if boundary == "terminal_before_cleanup": os._exit(86)
        raise AssertionError("非清理边界不应触达此探针")
    with patch.object(owner.store, "accept", acceptance), patch.object(owner, "_check_preconditions", preconditions), patch.object(operations, "publish_step", publication), patch.object(owner, "_settle", terminal):
        if boundary == "terminal_before_cleanup": owner._cleanup = cleanup
        await service.upload_file_entries(directory_path="target", scope="workspace", actor="local", intent=intent,
            entries=[("a.txt", io.BytesIO(b"a")), ("b.txt", io.BytesIO(b"b"))])
        await asyncio.sleep(10)
    raise AssertionError("崩溃探针没有命中")
asyncio.run(main())
'''


@pytest.fixture
def recovery_case(request):
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    workspace = prepare_default_test_workspace(workspace_root=context.workspace_root,
        template_root=Path.cwd() / "tests/fixtures/workspaces/model_tool_test_workspace")
    (workspace / "target").mkdir()
    config = Mock(spec=ConfigService)
    config.get_workspace_file_default_limit.return_value = 500
    yield context, workspace, config
    context.close()


@pytest.mark.parametrize("boundary", ["before_accept", "after_accept", "after_staging", "publishing_before",
                                      "partial", "after_effect", "terminal_before_cleanup"])
async def test_upload_crash_resumes_original_operation_and_retains_external_effect(recovery_case, boundary):
    context, workspace, config = recovery_case
    operation_id = "op_" + uuid4().hex
    child = await asyncio.to_thread(subprocess.run, [sys.executable, "-c", PROBE, str(workspace), boundary, operation_id],
                                   cwd=Path.cwd(), env=os.environ.copy(), capture_output=True, timeout=25, check=False)
    (context.artifacts_dir / f"{boundary}.log").write_bytes(child.stdout + child.stderr)
    assert child.returncode == 86, child.stderr.decode(errors="replace")
    service = WorkspaceService(config_service=config, workspace_root=workspace)
    await service.start()
    try:
        owner = service.file_operations
        if boundary == "before_accept":
            with pytest.raises(KeyError):
                owner.status([operation_id], actor="local")
            assert list((workspace / "target").iterdir()) == []
            assert list((workspace / ".boxteam/navigation/workspace-files-staging").iterdir()) == []
            return
        receipt = owner.status([operation_id], actor="local").operations[0]
        assert receipt.operation_id == operation_id and receipt.client_sequence == 1
        assert receipt.state == ("rejected" if boundary == "partial" else "committed")
        assert receipt.effect == ("partial" if boundary == "partial" else "complete")
        assert (workspace / "target/a.txt").read_bytes() == b"a"
        assert (workspace / "target/b.txt").read_bytes() == (b"external" if boundary == "partial" else b"b")
        listing = await service.list_files(path="target")
        assert [item.name for item in listing.items] == ["a.txt", "b.txt"]
        assert listing.directory_revision == next(value.directory_revision for value in receipt.directories if value.directory_key.path == listing.path)
        assert owner.store.pending() == () and owner.store.cleanup_pending() == ()
        assert owner.store.events(0)[-1]["operation_id"] == operation_id
        assert list((workspace / ".boxteam/navigation/workspace-files-staging").iterdir()) == []
        assert not list((workspace / "target").glob(".boxteam-file-*"))
    finally:
        await service.stop()
        service.close()
