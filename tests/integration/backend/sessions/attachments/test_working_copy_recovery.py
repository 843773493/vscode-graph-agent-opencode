"""产品生成附件后，释放/重建进程硬退出仍沿原身份与原版本恢复。"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from contextlib import closing
from pathlib import Path
from textwrap import dedent

import pytest

from app.core.resource_uri import parse_attachment_uri
from app.core.trace_middleware import get_current_gateway_id
from app.services.infrastructure.attachment_blob_catalog.store import (
    AttachmentBlobStore,
)
from tests.harness.python.run_context import TestRunContext
from tests.integration.backend.sessions.test_attachment_runtime_lifecycle import (
    attachment_directory,
    execute_upload,
    isolate_unused_external_managers,
)
from tests.integration.backend.sessions.test_thread_runtime_runner import RuntimeCase
from tests.integration.backend.sessions.test_thread_runtime_runner import (
    runner_case as runner_case,  # noqa: PLC0414
)
from tests.integration.backend.sessions.test_thread_runtime_runner import (
    runtime_case as runtime_case,  # noqa: PLC0414
)


@pytest.mark.parametrize("boundary", ["before_remove", "after_remove", "before_publish", "after_publish"])
async def test_hard_exit_resumes_working_copy_without_changing_resource_or_history(
    runtime_case: RuntimeCase, monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest, boundary: str,
) -> None:
    case = runtime_case
    isolate_unused_external_managers(case, monkeypatch)
    container = case.runner.container
    sid, tid, ref = await execute_upload(case)
    aid = parse_attachment_uri(ref.file_id, require_file=True).attachment_id
    assert aid is not None
    store = container.attachment_blob_store
    directory = attachment_directory(store, aid)
    record = store._catalog.get_directory(aid)
    assert record is not None and record.availability == "available"
    token = case.tracker.runtime_binding_token(case.tracker.snapshot(sid, tid).generation)
    canonical = container.session_service.path_resolver.resolve_thread_node(sid, tid) / "rollout/rollout.jsonl"
    original_history = canonical.read_bytes()
    unknown = directory.with_name("att_" + "f" * 32)
    unknown.mkdir()
    (unknown / "keep.txt").write_bytes(b"not-owned-by-catalog")

    # 只注入物理删除/发布边界；pending、释放及重建状态必须由产品 owner 提交。
    program = dedent("""\
        import asyncio
        import json
        import os
        import sys
        from contextlib import ExitStack, closing
        from pathlib import Path
        from app.core.path_utils import build_session_catalog_components, catalog_provider_scope
        from app.core.trace_middleware import set_current_gateway_id
        from app.core.workspace_identity import read_workspace_id
        from app.services.infrastructure.attachment_blob_catalog import store as module
        from app.services.infrastructure.attachment_blob_catalog import working_copies
        from app.services.infrastructure.external_resource_leases import ExternalResourceLeaseLedger
        from app.services.infrastructure.rollout_context.checkpoint.saver import RolloutCheckpointSaver

        root = Path(sys.argv[1])
        phase, attachment_id, session_id, thread_id, token, file_id, gateway_id = sys.argv[2:9]
        if gateway_id:
            set_current_gateway_id(gateway_id)
        with ExitStack() as cleanup:
            catalog = cleanup.enter_context(closing(build_session_catalog_components(
                root / ".boxteam/sessions", read_workspace_id(root))))
            cleanup.enter_context(catalog_provider_scope(catalog))
            ledger = ExternalResourceLeaseLedger(state_path=root / ".boxteam/resources.json")
            store = cleanup.enter_context(closing(module.AttachmentBlobStore(
                root, path_resolver=catalog.resolver, leases=ledger)))
            saver = cleanup.enter_context(RolloutCheckpointSaver(
                root / ".boxteam/sessions", attachment_store=store))
            def receipt(**values):
                record = store._catalog.get_directory(attachment_id)
                print(json.dumps({"state": record.availability,
                                  "relative_locator": record.relative_locator, **values}), flush=True)
            remove = module.remove_directory
            def interrupt_removal(path):
                if phase == "after_remove":
                    remove(path)
                receipt(exists=path.exists())
                os._exit(91)
            rename = working_copies.os.rename
            def interrupt_publish(source, target):
                if phase == "after_publish":
                    rename(source, target)
                receipt(exists=Path(target).exists())
                os._exit(92)
            if phase in {"before_remove", "after_remove"}:
                module.remove_directory = interrupt_removal
                asyncio.run(store.release_runtime_binding(session_id, thread_id, token, messages=saver))
            elif phase == "resume_release":
                released = asyncio.run(store.process_pending_directories())
                receipt(released=list(released))
            else:
                if phase in {"before_publish", "after_publish"}:
                    working_copies.os.rename = interrupt_publish
                content = store.read(session_id, file_id)
                receipt(body=content.data.decode("utf-8"), runtime_bindings=store._catalog.runtime_bindings())
    """)
    artifacts = TestRunContext.from_test_file(Path(request.node.path)).prepare().artifacts_dir

    async def invoke(phase: str) -> subprocess.CompletedProcess[str]:
        command = [sys.executable, "-c", program, str(container.workspace_root), phase,
                   aid, sid, tid, token, ref.file_id, get_current_gateway_id() or ""]
        result = await asyncio.to_thread(
            subprocess.run, command, capture_output=True, text=True, timeout=30, check=False,
        )
        prefix = f"working-copy-{boundary}-{phase}"
        (artifacts / f"{prefix}.stdout.raw").write_text(result.stdout, encoding="utf-8")
        (artifacts / f"{prefix}.stderr.raw").write_text(result.stderr, encoding="utf-8")
        (artifacts / f"{prefix}.json").write_text(json.dumps({
            "command": command, "exit_code": result.returncode, "boundary": boundary, "phase": phase,
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return result

    if boundary.endswith("remove"):
        result = await invoke(boundary)
        assert result.returncode == 91, (result.stdout, result.stderr)
        assert json.loads(result.stdout) == {
            "state": "releasing", "relative_locator": record.relative_locator,
            "exists": boundary == "before_remove",
        }
        interrupted = store._catalog.get_directory(aid)
        assert interrupted is not None and interrupted.availability == "releasing"
        assert directory.exists() == (boundary == "before_remove")
        recovered = await invoke("resume_release")
        assert recovered.returncode == 0, (recovered.stdout, recovered.stderr)
        assert json.loads(recovered.stdout) == {
            "state": "released", "relative_locator": record.relative_locator, "released": [aid],
        }
        assert not directory.exists()
    else:
        await case.tracker.close(session_id=sid)
        assert not directory.exists()
        result = await invoke(boundary)
        assert result.returncode == 92, (result.stdout, result.stderr)
        assert json.loads(result.stdout) == {
            "state": "rebuilding", "relative_locator": record.relative_locator,
            "exists": boundary == "after_publish",
        }
        interrupted = store._catalog.get_directory(aid)
        assert interrupted is not None and interrupted.availability == "rebuilding"

    recovered = await invoke("resume_read")
    assert recovered.returncode == 0, (recovered.stdout, recovered.stderr)
    assert json.loads(recovered.stdout) == {
        "state": "available", "relative_locator": record.relative_locator,
        "body": "runtime original", "runtime_bindings": [],
    }
    with closing(AttachmentBlobStore(container.workspace_root, path_resolver=container.session_service.path_resolver)) as restarted:
        assert attachment_directory(restarted, aid) == directory
        assert restarted.describe_reference(ref, session_id=sid) == ref
        assert restarted.read(sid, ref.file_id).data == b"runtime original"
    assert (unknown / "keep.txt").read_bytes() == b"not-owned-by-catalog"
    assert canonical.read_bytes() == original_history
