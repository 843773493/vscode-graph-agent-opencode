"""产品执行生成独立目录，记录目录增长、冷恢复及到期回收的实测成本。"""

from __future__ import annotations

import json
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from time import perf_counter_ns

import pytest

from app.container import AppContainer
from app.core.resource_uri import parse_attachment_uri
from app.core.trace_middleware import get_current_gateway_id
from app.schemas.internal_v2.session import SessionCreateRequest
from app.services.infrastructure.attachment_blob_catalog.catalog import (
    AttachmentBlobCatalog,
)
from app.services.infrastructure.attachment_blob_catalog.locator import (
    resolve_attachment_directory,
)
from app.services.infrastructure.attachment_blob_catalog.store import (
    AttachmentBlobStore,
)
from app.services.orchestration.thread_residency import ThreadResidencyTracker
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


def catalog_size(catalog: AttachmentBlobCatalog) -> dict[str, object]:
    connection = catalog.connection
    tables = [row[0] for row in connection.execute(
        "SELECT name FROM sqlite_schema WHERE type='table' AND name LIKE 'attachment_%' ORDER BY name",
    )]
    return {
        "rows": {table: connection.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0] for table in tables},
        "allocated_bytes": connection.execute("PRAGMA page_count").fetchone()[0]
        * connection.execute("PRAGMA page_size").fetchone()[0],
        "free_pages": connection.execute("PRAGMA freelist_count").fetchone()[0],
        "files": {suffix: path.stat().st_size if path.exists() else 0
                  for suffix in ("", "-wal", "-shm")
                  for path in (Path(str(catalog.database_path) + suffix),)},
    }


@pytest.mark.parametrize("count", [1, 32], ids=["one-directory", "thirty-two-directories"])
async def test_product_directory_growth_cold_recovery_and_due_gc(
    production_fork_container: AppContainer, fork_execution_message,
    request: pytest.FixtureRequest, count: int,
) -> None:
    container = production_fork_container
    store = container.attachment_blob_store
    before = catalog_size(store._catalog)
    session = await container.session_service.create(SessionCreateRequest(title="目录回收测量"))
    body = b"independent-directory-original\n" * 128
    started = perf_counter_ns()
    accepted = await fork_execution_message(
        container, session.session_id, content="接受附件并完成本轮执行。",
        attachments=[upload(body) for _ in range(count)], gateway_id=get_current_gateway_id(),
    )
    execution_ns = perf_counter_ns() - started
    items = container.checkpointer._storage.storage.read_items(session.session_id, accepted.thread_id)
    references, = [item.payload["attachments"] for item in items
                   if isinstance(item.payload, dict) and item.payload.get("attachments")]
    identities = {parse_attachment_uri(ref["file_id"], require_file=True).attachment_id for ref in references}
    assert len(references) == len(identities) == count
    records = [store._catalog.get_directory(identity) for identity in sorted(identities)]
    assert all(record is not None and record.gc_after is not None for record in records)
    directories = [resolve_attachment_directory(store._attachments_root, record.relative_locator) for record in records]
    assert all((directory / "original/content").read_bytes() == body for directory in directories)
    physical_files = [path for directory in directories for path in directory.rglob("*") if path.is_file()]
    assert len(physical_files) == count
    assert sum(path.stat().st_size for path in physical_files) == count * len(body)
    published = catalog_size(store._catalog)
    assert published["rows"]["attachment_directories"] == count
    assert published["rows"]["attachment_directory_files"] == count
    assert published["rows"]["attachment_directory_associations"] == count
    canonical = container.session_service.path_resolver.resolve_thread_node(
        session.session_id, accepted.thread_id,
    ) / "rollout/rollout.jsonl"
    original_history = canonical.read_bytes()
    due = max(datetime.fromisoformat(record.gc_after) for record in records)

    # 截止前的索引查询和冷恢复都消费产品事实，不手填目录关联、执行或 pin。
    started = perf_counter_ns()
    assert store._catalog.gc_candidates(now=due - timedelta(days=1)) == ()
    not_due_query_ns = perf_counter_ns() - started
    tracker = ThreadResidencyTracker()
    started = perf_counter_ns()
    with closing(AttachmentBlobStore(
        container.workspace_root, path_resolver=container.session_service.path_resolver,
        clock=lambda: due,
    )) as cold:
        assert await cold.recover_pending_ingests(messages=container.checkpointer) == 0
        assert cold.reconcile_runtime_bindings(
            messages=container.checkpointer, is_current=tracker.is_current_runtime_binding,
        ) == ()
        recovery_ns = perf_counter_ns() - started
        assert {record.attachment_id: (record.relative_locator, record.gc_after) for record in records} == {
            identity: (cold._catalog.get_directory(identity).relative_locator, cold._catalog.get_directory(identity).gc_after)
            for identity in identities
        }
        started = perf_counter_ns()
        assert {record.attachment_id for record in cold._catalog.gc_candidates(now=due)} == identities
        due_query_ns = perf_counter_ns() - started
        started = perf_counter_ns()
        assert set(await cold.collect_garbage(leases=container.external_resource_leases)) == identities
        gc_ns = perf_counter_ns() - started
        assert not any(directory.exists() for directory in directories)
        assert all(cold._catalog.get_directory(identity).availability == "tombstoned" for identity in identities)
        assert await cold.collect_garbage(leases=container.external_resource_leases) == ()
        after_gc = catalog_size(cold._catalog)
    assert canonical.read_bytes() == original_history
    artifacts = TestRunContext.from_test_file(Path(request.node.path)).prepare().artifacts_dir
    (artifacts / f"retention-{count}.json").write_text(json.dumps({
        "directory_count": count, "original_bytes_per_directory": len(body),
        "live_original_bytes": count * len(body), "original_files": len(physical_files),
        "catalog_before": before, "catalog_published": published, "catalog_after_gc": after_gc,
        "elapsed_ns": {"upload_execute_unload": execution_ns, "not_due_query": not_due_query_ns,
                       "cold_owner_recovery": recovery_ns, "due_query": due_query_ns, "gc": gc_ns},
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
