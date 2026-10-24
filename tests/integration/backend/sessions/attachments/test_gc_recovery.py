"""产品生成附件后，真实 GC 进程硬退出只能沿原删除记录恢复。"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from textwrap import dedent

import pytest

from app.container import AppContainer
from app.core.resource_uri import parse_attachment_uri
from app.core.trace_middleware import get_current_gateway_id
from app.schemas.internal_v2.session import SessionCreateRequest
from app.services.infrastructure.attachment_blob_catalog.locator import (
    resolve_attachment_directory,
)
from app.services.infrastructure.attachment_blob_catalog.store import (
    AttachmentBlobStore,
)
from tests.harness.python.run_context import TestRunContext
from tests.integration.backend.sessions.fork_execution_source import (
    fork_execution_message as fork_execution_message,  # noqa: PLC0414
)
from tests.integration.backend.sessions.test_attachment_directory_entry import upload
from tests.integration.backend.sessions.test_rollout_fork_modes import (
    fork_gateway_request_context as fork_gateway_request_context,  # noqa: PLC0414
)
from tests.integration.backend.sessions.test_rollout_fork_modes import (
    production_fork_container as production_fork_container,  # noqa: PLC0414
)


@pytest.mark.parametrize("boundary", ["before_remove", "after_remove"])
async def test_gc_hard_exit_resumes_original_deletion_without_reviving_directory(
    production_fork_container: AppContainer, fork_execution_message,
    request: pytest.FixtureRequest, boundary: str,
) -> None:
    container = production_fork_container
    session = await container.session_service.create(SessionCreateRequest(title="目录回收中断"))
    accepted = await fork_execution_message(
        container, session.session_id, content="保存附件并完成执行。",
        attachments=[upload(b"gc-restart-original")],
        gateway_id=get_current_gateway_id(),
    )
    items = container.checkpointer._storage.storage.read_items(session.session_id, accepted.thread_id)
    ref, = next(item.payload["attachments"] for item in items if item.payload.get("attachments"))
    attachment_id = parse_attachment_uri(ref["file_id"], require_file=True).attachment_id
    store = container.attachment_blob_store
    record = store._catalog.get_directory(attachment_id)
    assert record is not None and record.gc_after is not None
    directory = resolve_attachment_directory(store._attachments_root, record.relative_locator)
    assert (directory / "original/content").read_bytes() == b"gc-restart-original"
    expires = datetime.fromisoformat(record.gc_after) + timedelta(seconds=1)
    canonical = container.session_service.path_resolver.resolve_thread_node(
        session.session_id, accepted.thread_id,
    ) / "rollout/rollout.jsonl"
    original_history = canonical.read_bytes()
    unknown = directory.with_name("att_" + "f" * 32)
    unknown.mkdir()
    (unknown / "keep.txt").write_bytes(b"not-owned-by-catalog")

    # 只注入真实物理删除边界；deleting 与 tombstone 都必须由产品 owner 写入。
    program = dedent("""\
        import asyncio
        import json
        import os
        import sys
        from datetime import datetime
        from pathlib import Path
        from app.core.path_utils import build_session_catalog_components
        from app.core.workspace_identity import load_or_create_workspace_id
        from app.services.infrastructure.attachment_blob_catalog import store as module
        from app.services.infrastructure.external_resource_leases import ExternalResourceLeaseLedger

        root = Path(sys.argv[1])
        now = datetime.fromisoformat(sys.argv[2])
        boundary, attachment_id = sys.argv[3:5]
        catalog = build_session_catalog_components(root / ".boxteam/sessions", load_or_create_workspace_id(root))
        store = module.AttachmentBlobStore(root, path_resolver=catalog.resolver, clock=lambda: now)
        remove = module.remove_directory
        def interrupt(path):
            if boundary == "after_remove":
                remove(path)
            record = store._catalog.get_directory(attachment_id)
            print(json.dumps({"state": record.availability, "gc_after": record.gc_after,
                              "relative_locator": record.relative_locator, "exists": path.exists()}), flush=True)
            os._exit(91)
        if boundary != "resume":
            module.remove_directory = interrupt
        try:
            collected = asyncio.run(store.collect_garbage(leases=ExternalResourceLeaseLedger(
                state_path=root / ".boxteam/resources.json",
            )))
            print(json.dumps({"collected": collected,
                              "state": store._catalog.get_directory(attachment_id).availability}), flush=True)
        finally:
            store.close()
            catalog.close()
    """)
    artifacts = TestRunContext.from_test_file(Path(request.node.path)).prepare().artifacts_dir

    async def invoke(phase: str) -> subprocess.CompletedProcess[str]:
        command = [sys.executable, "-c", program, str(container.workspace_root),
                   expires.isoformat(), phase, attachment_id]
        result = await asyncio.to_thread(
            subprocess.run, command, capture_output=True, text=True, timeout=30, check=False,
        )
        prefix = f"gc-{boundary}-{phase}"
        (artifacts / f"{prefix}.stdout.raw").write_text(result.stdout, encoding="utf-8")
        (artifacts / f"{prefix}.stderr.raw").write_text(result.stderr, encoding="utf-8")
        (artifacts / f"{prefix}.json").write_text(json.dumps({
            "command": command, "exit_code": result.returncode, "boundary": boundary, "phase": phase,
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return result

    result = await invoke(boundary)
    assert result.returncode == 91, (result.stdout, result.stderr)
    assert json.loads(result.stdout) == {
        "state": "deleting", "gc_after": record.gc_after,
        "relative_locator": record.relative_locator, "exists": boundary == "before_remove",
    }
    interrupted = store._catalog.get_directory(attachment_id)
    assert interrupted.availability == "deleting"
    assert (interrupted.relative_locator, interrupted.gc_after) == (record.relative_locator, record.gc_after)
    assert directory.exists() == (boundary == "before_remove")
    with pytest.raises(FileNotFoundError):
        store.read(session.session_id, ref["file_id"])

    recovered = await invoke("resume")
    assert recovered.returncode == 0, (recovered.stdout, recovered.stderr)
    assert json.loads(recovered.stdout) == {"collected": [attachment_id], "state": "tombstoned"}
    with closing(AttachmentBlobStore(
        container.workspace_root, path_resolver=container.session_service.path_resolver,
        clock=lambda: expires,
    )) as restarted:
        # 独立进程恢复后再次冷读与回收，不重建目录或扫描吸收旁支。
        final = restarted._catalog.get_directory(attachment_id)
        assert final.availability == "tombstoned"
        assert (final.relative_locator, final.gc_after) == (record.relative_locator, record.gc_after)
        assert not directory.exists()
        assert await restarted.collect_garbage(leases=container.external_resource_leases) == ()
        with pytest.raises(FileNotFoundError):
            restarted.read(session.session_id, ref["file_id"])
    assert (unknown / "keep.txt").read_bytes() == b"not-owned-by-catalog"
    assert canonical.read_bytes() == original_history
