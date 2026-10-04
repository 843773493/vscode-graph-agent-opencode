"""通过真实子进程退出验证导航队列 owner 恢复与 fencing。"""

from __future__ import annotations

import asyncio
import hashlib
import subprocess
import sys
from pathlib import Path

import pytest

from app.core.path_utils import get_session_path_resolver
from app.core.session_catalog_resolver import SessionCatalogPathResolver
from app.core.session_catalog_store import SessionCatalogStore
from app.core.sqlite_state import utc_now_text
from app.schemas.internal_v2.session_navigation.operations import (
    NavigationMutationIntentDTO,
)
from app.services.business.session_navigation.executor import NavigationMutationExecutor
from app.services.business.session_navigation.operations_service import (
    local_navigation_scope,
)
from app.services.business.session_navigation.queue_store import (
    NavigationMutationQueueStore,
    NavigationQueueOwnerError,
)
from tests.harness.python.run_context import TestRunContext
from tests.support.workspaces import prepare_default_test_workspace


def _operation_id(seed: str) -> str:
    return f"op_{hashlib.sha256(seed.encode('utf-8')).hexdigest()[:32]}"


@pytest.fixture
def navigation_workspace(request: pytest.FixtureRequest) -> Path:
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    template = Path.cwd() / "tests" / "fixtures" / "workspaces" / "default_test_workspace"
    return prepare_default_test_workspace(
        workspace_root=context.workspace_root,
        template_root=template,
    )


_CRASH_AFTER_CLAIM = r"""
import asyncio
import os
import sys
from pathlib import Path

from app.core.path_utils import get_session_path_resolver
from app.core.sqlite_state import utc_now_text
from app.services.business.session_navigation.executor import NavigationMutationExecutor
from app.services.business.session_navigation.operations_service import local_navigation_scope
from app.services.business.session_navigation.queue_store import NavigationMutationQueueStore

sessions_root = Path(sys.argv[1])
operation_id = sys.argv[2]
resolver = get_session_path_resolver(sessions_root)
resolver.initialize()
store = resolver.catalog_store
workspace_id = resolver.workspace_id
scope = local_navigation_scope(workspace_id)
queue = NavigationMutationQueueStore(store)
executor = NavigationMutationExecutor(
    store=store,
    workspace_id=workspace_id,
    queue=queue,
    path_resolver=resolver,
)

async def claim_then_exit():
    async with executor.queue_owner() as owner:
        with store.write_transaction() as connection:
            record = queue.next_runnable(connection, workspace_id)
            assert record is not None and record.operation_id == operation_id
            claimed = queue.claim_running(
                connection,
                record=record,
                holder_id=owner.holder_id,
                owner_id=owner.owner_id,
                owner_generation=owner.generation,
                now=utc_now_text(),
            )
            assert claimed is not None and claimed.state == 'running'
        print(
            f'claimed:{owner.owner_id}:{owner.generation}:{claimed.fencing_token}',
            flush=True,
        )
        os._exit(91)

asyncio.run(claim_then_exit())
"""


@pytest.mark.asyncio
async def test_process_exit_releases_owner_and_new_generation_recovers_running(
    navigation_workspace: Path,
    request: pytest.FixtureRequest,
) -> None:
    """子进程崩溃留下 running 后，新 owner 递增 generation 并完成同一操作。"""
    sessions_root = navigation_workspace / ".boxteam" / "sessions"
    resolver: SessionCatalogPathResolver = get_session_path_resolver(sessions_root)
    resolver.initialize()
    store: SessionCatalogStore = resolver.catalog_store
    workspace_id = resolver.workspace_id
    scope = local_navigation_scope(workspace_id)
    queue = NavigationMutationQueueStore(store)
    executor = NavigationMutationExecutor(
        store=store,
        workspace_id=workspace_id,
        queue=queue,
        path_resolver=resolver,
    )
    intent = NavigationMutationIntentDTO(
        client_operation_id=_operation_id("process_exit_navigation_queue"),
        client_sequence=1,
        kind="create_folder",
        base_catalog_revision=0,
        name="进程崩溃后恢复",
    )
    with store.write_transaction() as connection:
        queue.enqueue_batch(
            connection,
            gateway_id=scope.gateway_id,
            workspace_id=workspace_id,
            actor=scope.actor,
            intents=[intent],
            now=utc_now_text(),
        )

    crashed = await asyncio.to_thread(
        subprocess.run,
        [
            sys.executable,
            "-c",
            _CRASH_AFTER_CLAIM,
            str(sessions_root),
            intent.client_operation_id,
        ],
        cwd=Path.cwd(),
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    artifacts = TestRunContext.from_test_file(Path(request.node.path)).artifacts_dir
    artifacts.mkdir(parents=True, exist_ok=True)
    (artifacts / "owner-crash.stdout.log").write_text(crashed.stdout, encoding="utf-8")
    (artifacts / "owner-crash.stderr.log").write_text(crashed.stderr, encoding="utf-8")
    (artifacts / "owner-crash.exitcode.txt").write_text(
        f"{crashed.returncode}\n", encoding="utf-8"
    )
    assert crashed.returncode == 91, crashed.stderr
    assert crashed.stdout.startswith("claimed:")

    with store.read_transaction() as connection:
        old_owner = connection.execute(
            "SELECT owner_id, owner_generation FROM navigation_queue_owners "
            "WHERE workspace_id = ?",
            (workspace_id,),
        ).fetchone()
        abandoned = queue.fetch_record_in(
            connection,
            gateway_id=scope.gateway_id,
            workspace_id=workspace_id,
            actor=scope.actor,
            operation_id=intent.client_operation_id,
        )
    assert old_owner is not None
    assert abandoned is not None and abandoned.state == "running"
    old_owner_id, old_generation = str(old_owner[0]), int(old_owner[1])

    async with executor.queue_owner() as new_owner:
        assert new_owner.owner_id != old_owner_id
        assert new_owner.generation == old_generation + 1
        with (
            pytest.raises(
                NavigationQueueOwnerError, match="fencing token 已失效"
            ),
            store.write_transaction() as connection,
        ):
            queue.require_owner_generation(
                connection,
                workspace_id=workspace_id,
                owner_id=old_owner_id,
                generation=old_generation,
            )
        assert await executor.recover_in_flight() == 1
        recovered = queue.get_record(
            gateway_id=scope.gateway_id,
            workspace_id=workspace_id,
            actor=scope.actor,
            operation_id=intent.client_operation_id,
        )
        assert recovered is not None and recovered.state == "queued"
        assert recovered.holder_id is None
        assert recovered.fencing_token == abandoned.fencing_token
        outcomes = await executor.drain()

    assert len(outcomes) == 1
    committed = queue.get_record(
        gateway_id=scope.gateway_id,
        workspace_id=workspace_id,
        actor=scope.actor,
        operation_id=intent.client_operation_id,
    )
    assert committed is not None and committed.state == "committed"
    assert committed.fencing_token == abandoned.fencing_token + 1
    assert len(resolver.list_nodes()) == 1
    store.close()
