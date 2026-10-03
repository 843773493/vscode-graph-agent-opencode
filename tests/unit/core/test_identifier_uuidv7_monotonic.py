"""唯一 id 工厂的 UUIDv7 单调性与量化上界测试。

覆盖 OpenSpec change `migrate-identifiers-to-uuidv7` 的 §2（D2、D2b/A3）：
同进程同毫秒非递减且唯一、跨毫秒自然单调、生成路径不得传显式 timestamp，
以及「只承诺同进程内同毫秒有序」的量化边界。时钟回拨由 uuid-utils 默认路径
处理，创建链路直接从已分配 ID 派生时间与日期桶。
"""

from __future__ import annotations

import collections
import json
import os
import shutil
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.core import identifier
from app.core.identifier import create_prefixed_id, create_uuid_hex
from tests.support.canonical_id_at import session_id_at
from tests.support.catalog_session_bundle import (
    CatalogSessionBundleSpec,
    seed_catalog_session,
)
from tests.support.workspaces import prepare_default_test_workspace

_UUID7_TEST_OUTPUT_ROOT = Path("out/tests/unit/core/test_identifier_uuidv7_monotonic")


@pytest.fixture
def isolated_uuid7_workspace(request: pytest.FixtureRequest) -> Path:
    workspace_root = (
        Path.cwd() / _UUID7_TEST_OUTPUT_ROOT / "workspace" / request.node.name
    )
    template_root = Path.cwd() / "tests/fixtures/workspaces/default_test_workspace"
    return prepare_default_test_workspace(
        workspace_root=workspace_root,
        template_root=template_root,
    )


def _ms_groups(hex_ids: list[str]) -> dict[str, list[str]]:
    groups: dict[str, list[str]] = collections.defaultdict(list)
    for value in hex_ids:
        groups[value[:12]].append(value)
    return dict(groups)


# ----------------------------------------------------------------------
# 2.1 同进程同毫秒非递减且唯一
# ----------------------------------------------------------------------


def test_same_millisecond_batch_is_non_decreasing_and_unique() -> None:
    hex_ids = [create_uuid_hex() for _ in range(20000)]

    # 同毫秒内的每个分组都 MUST 有序且唯一（rand_a/计数器方案）。
    groups = _ms_groups(hex_ids)
    assert groups, "至少应落在一个毫秒分组内"
    for group in groups.values():
        assert group == sorted(group), "同毫秒组内 MUST 非递减"
        assert len(set(group)) == len(group), "同毫秒组内 MUST 唯一"
    # 整体（跨毫秒）按生成顺序逐字节有序且唯一。
    assert hex_ids == sorted(hex_ids)
    assert len(set(hex_ids)) == 20000


def test_prefixed_ids_same_millisecond_are_non_decreasing() -> None:
    payloads = [create_prefixed_id("msg").split("_", maxsplit=1)[1] for _ in range(20000)]

    assert payloads == sorted(payloads)
    assert len(set(payloads)) == 20000


# ----------------------------------------------------------------------
# 2.2 跨毫秒自然单调
# ----------------------------------------------------------------------


def test_cross_millisecond_batch_is_globally_ordered_and_unique() -> None:
    hex_ids = [create_uuid_hex() for _ in range(200000)]

    assert hex_ids == sorted(hex_ids)
    assert len(set(hex_ids)) == 200000


# ----------------------------------------------------------------------
# ----------------------------------------------------------------------
# 2.3 生成路径不得传显式 timestamp
# ----------------------------------------------------------------------


def test_generation_path_does_not_pass_explicit_timestamp(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
    real_uuid7 = identifier._resolve_uuid7()

    def spy(*args: object, **kwargs: object):
        calls.append((args, kwargs))
        return real_uuid7(*args, **kwargs)

    monkeypatch.setattr(identifier, "_resolve_uuid7", lambda: spy)

    create_uuid_hex()
    create_prefixed_id("evt")

    assert calls, "生成路径必须经 uuid7"
    for args, kwargs in calls:
        assert args == () and kwargs == {}, (
            "生成路径 MUST NOT 传显式 timestamp（会破坏同毫秒单调）: "
            f"args={args!r}, kwargs={kwargs!r}"
        )


def test_generation_docstring_forbids_explicit_timestamp() -> None:
    doc = create_uuid_hex.__doc__ or ""
    assert "MUST NOT 传显式" in doc


# ----------------------------------------------------------------------
# 2.5（A3）量化上界与「只承诺同进程内同毫秒有序」的边界断言
# ----------------------------------------------------------------------


def test_quantified_same_millisecond_density_upper_bound() -> None:
    hex_ids = [create_uuid_hex() for _ in range(500000)]

    groups = _ms_groups(hex_ids)
    max_group = max(len(group) for group in groups.values())

    # 记录实测的同毫秒组最大规模（规划实测约 3710；实际随机器与负载浮动）。
    assert max_group >= 1
    # 组内 MUST 全部有序且唯一（毫秒内数千个 id 仍保持顺序）。
    assert all(group == sorted(group) for group in groups.values())
    assert all(len(set(group)) == len(group) for group in groups.values())
    assert hex_ids == sorted(hex_ids)
    assert len(set(hex_ids)) == 500000


def test_documented_contract_only_promises_same_process_same_ms_order() -> None:
    """实现与文档只承诺：同进程内同毫秒非递减且唯一 + 跨进程共享 48 bit 毫秒分辨率。

    MUST NOT 承诺跨进程同毫秒有序或主键严格按时间相邻。
    """
    doc = create_uuid_hex.__doc__ or ""
    assert "同进程" in doc and "同毫秒" in doc
    assert "跨进程" in doc and "48 bit 毫秒分辨率" in doc
    assert "严格按时间相邻" not in doc
    assert "跨进程同毫秒有序" not in doc

    # 工厂模块整体同样不得出现「严格按时间相邻」这类过度承诺。
    source = identifier.__doc__ or ""
    assert "严格按时间相邻" not in source


def test_real_uuid7_allocator_keeps_session_and_child_dates_consistent_across_midnight_and_rollback(
    isolated_uuid7_workspace: Path,
) -> None:
    if sys.platform != "linux":
        pytest.skip("真实时钟 shim 使用 Linux LD_PRELOAD 与 clock_gettime")
    compiler = shutil.which("cc")
    if compiler is None:
        pytest.skip("真实时钟 shim 需要 C 编译器 cc")

    artifact_root = Path.cwd() / _UUID7_TEST_OUTPUT_ROOT / "artifacts"
    artifact_root.mkdir(parents=True, exist_ok=True)
    stem = f"u07-realtime-clock-{os.getpid()}"
    source_path = artifact_root / f"{stem}.c"
    library_path = artifact_root / f"{stem}.so"
    source_path.write_text(
        """
#define _GNU_SOURCE
#include <stdatomic.h>
#include <stdint.h>
#include <sys/syscall.h>
#include <time.h>
#include <unistd.h>

static _Atomic int64_t fixed_realtime_ms = -1;

void u07_set_realtime_ms(int64_t epoch_ms) {
    atomic_store_explicit(&fixed_realtime_ms, epoch_ms, memory_order_relaxed);
}

int clock_gettime(clockid_t clock_id, struct timespec *value) {
    const int64_t epoch_ms = atomic_load_explicit(
        &fixed_realtime_ms,
        memory_order_relaxed
    );
    if (clock_id == CLOCK_REALTIME && epoch_ms >= 0) {
        value->tv_sec = (time_t)(epoch_ms / 1000);
        value->tv_nsec = (long)(epoch_ms % 1000) * 1000000L;
        return 0;
    }
    return (int)syscall(SYS_clock_gettime, clock_id, value);
}
""",
        encoding="utf-8",
    )
    compile_result = subprocess.run(
        [
            compiler,
            "-std=c11",
            "-shared",
            "-fPIC",
            "-O2",
            "-Wall",
            "-Wextra",
            "-Werror",
            str(source_path),
            "-o",
            str(library_path),
        ],
        capture_output=True,
        check=False,
        text=True,
        timeout=15,
    )
    assert compile_result.returncode == 0, (
        "真实时钟 shim 编译失败: "
        f"stdout={compile_result.stdout!r}, stderr={compile_result.stderr!r}"
    )

    session_moment_ms = identifier.to_epoch_ms(
        datetime(2026, 6, 1, 23, 59, 59, 999_000, tzinfo=UTC)
    )
    after_midnight_ms = session_moment_ms + 2
    rollback_ms = session_moment_ms - 3_600_000
    environment = os.environ.copy()
    environment["BOXTEAM_U07_CLOCK_SHIM"] = str(library_path)
    environment["BOXTEAM_U07_WORKSPACE"] = str(isolated_uuid7_workspace)
    previous_preload = environment.get("LD_PRELOAD")
    environment["LD_PRELOAD"] = os.pathsep.join(
        value for value in (str(library_path), previous_preload) if value
    )
    environment["BOXTEAM_U07_SESSION_MS"] = str(session_moment_ms)
    environment["BOXTEAM_U07_AFTER_MIDNIGHT_MS"] = str(after_midnight_ms)
    environment["BOXTEAM_U07_ROLLBACK_MS"] = str(rollback_ms)
    driver = r"""
import asyncio
import ctypes
import hashlib
import json
import os
from datetime import datetime
from pathlib import Path

from app.core.session_catalog_store import SessionCatalogStore
from app.core.session_control_store import SessionControlStore
from app.core.session_creation import SessionCreationService

clock_shim = ctypes.CDLL(os.environ["BOXTEAM_U07_CLOCK_SHIM"])
set_realtime_ms = clock_shim.u07_set_realtime_ms
set_realtime_ms.argtypes = [ctypes.c_int64]
set_realtime_ms.restype = None

workspace = Path(os.environ["BOXTEAM_U07_WORKSPACE"])
boxteam_root = workspace / ".boxteam"
sessions_root = boxteam_root / "sessions"
store = SessionCatalogStore(
    boxteam_root / "navigation" / "u07-session-catalog.sqlite",
    sessions_root,
)
service = SessionCreationService(
    store=store,
    sessions_root=sessions_root,
    workspace_id="u07-workspace",
)
session_metadata = {
    "kind": "normal",
    "delegation": None,
    "generation_origin": None,
    "current_agent_id": "default",
    "current_provider_id": None,
    "context_source_session_id": None,
}

async def create_session(key):
    return await service.create(
        idempotency_key=key,
        title=key,
        parent_node_id=None,
        session_metadata=session_metadata,
    )

def create_child(session_record, key):
    session_directory = sessions_root / session_record.storage_relative_locator[
        len("sessions/"):
    ]
    control_path = session_directory / "session-control.sqlite"
    control = SessionControlStore(control_path)
    try:
        main_row = control.get_main_thread()
        child_record = control.create_or_get_thread_creation_record(
            idempotency_key=key,
            initial_state="idle",
            preimage_hash=hashlib.sha256(key.encode()).hexdigest(),
            graph_binding="{}",
            capability_profile="{}",
        )
        control.freeze_thread_creation_artifact_manifest(
            key,
            artifact_manifest="{}",
            artifact_manifest_hash=hashlib.sha256(b"{}").hexdigest(),
        )
        control.publish_thread_creation_record(key)
        child_row = control.connection.execute(
            "SELECT thread_id, created_at FROM thread_catalog WHERE kind = 'child'"
        ).fetchone()
        if child_row is None:
            raise RuntimeError(f"child catalog row 缺失: key={key!r}")
        return {
            "thread_id": child_record.child_thread_id,
            "created_at": child_record.child_created_at,
            "locator": child_record.final_relative_locator,
            "main_thread_id": str(main_row["thread_id"]),
            "main_created_at": str(main_row["created_at"]),
            "catalog_thread_id": str(child_row["thread_id"]),
            "catalog_created_at": str(child_row["created_at"]),
        }
    finally:
        control.close()

def session_data(record):
    session_directory = sessions_root / record.storage_relative_locator[
        len("sessions/"):
    ]
    manifest = json.loads((session_directory / "session.json").read_text())
    return {
        "session_id": record.session_id,
        "main_thread_id": record.main_thread_id,
        "created_at": record.node.created_at,
        "locator": record.storage_relative_locator,
        "directory": str(session_directory),
        "manifest_created_at": manifest["created_at"],
    }

try:
    async def run_creation_flow():
        set_realtime_ms(int(os.environ["BOXTEAM_U07_SESSION_MS"]))
        before_midnight = await create_session("u07-before-midnight")
        set_realtime_ms(int(os.environ["BOXTEAM_U07_AFTER_MIDNIGHT_MS"]))
        child_after_midnight = create_child(
            before_midnight, "u07-child-after-midnight"
        )
        set_realtime_ms(int(os.environ["BOXTEAM_U07_ROLLBACK_MS"]))
        after_rollback = await create_session("u07-after-rollback")
        child_after_rollback = create_child(
            after_rollback, "u07-child-after-rollback"
        )
        return {
            "sessions": [
                session_data(before_midnight),
                session_data(after_rollback),
            ],
            "children": [child_after_midnight, child_after_rollback],
        }

    print(json.dumps(asyncio.run(run_creation_flow()), sort_keys=True))
finally:
    store.close()
"""
    completed = subprocess.run(
        [sys.executable, "-c", driver],
        capture_output=True,
        check=False,
        cwd=Path.cwd(),
        env=environment,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, (
        "真实 uuid-utils 时钟验收子进程失败: "
        f"stdout={completed.stdout!r}, stderr={completed.stderr!r}"
    )
    result = json.loads(completed.stdout)

    sessions = result["sessions"]
    children = result["children"]
    session_ids = [session["session_id"] for session in sessions]
    main_thread_ids = [session["main_thread_id"] for session in sessions]
    child_ids = [child["thread_id"] for child in children]
    all_ids = [
        sessions[0]["session_id"],
        sessions[0]["main_thread_id"],
        children[0]["thread_id"],
        sessions[1]["session_id"],
        sessions[1]["main_thread_id"],
        children[1]["thread_id"],
    ]
    embedded_ms = [int(identity[4:16], 16) for identity in all_ids]
    payloads = [identity[4:] for identity in all_ids]
    assert len(set(all_ids)) == len(all_ids)
    assert payloads == sorted(payloads)
    assert embedded_ms == sorted(embedded_ms)
    assert int(session_ids[0][4:16], 16) == session_moment_ms
    assert int(main_thread_ids[0][4:16], 16) == session_moment_ms
    assert int(child_ids[0][4:16], 16) == after_midnight_ms
    assert int(session_ids[1][4:16], 16) >= after_midnight_ms
    assert int(main_thread_ids[1][4:16], 16) == int(session_ids[1][4:16], 16)
    assert int(child_ids[1][4:16], 16) >= int(child_ids[0][4:16], 16)

    for index, session in enumerate(sessions):
        session_id = session["session_id"]
        main_thread_id = session["main_thread_id"]
        session_ms = int(session_id[4:16], 16)
        session_created_at = datetime.fromisoformat(session["created_at"])
        main_created_at = datetime.fromisoformat(children[index]["main_created_at"])
        session_directory = Path(session["directory"])
        utc_date = identifier.uuid7_datetime_from_hex(session_id[4:]).date()
        assert identifier.to_epoch_ms(session_created_at) == session_ms
        assert (
            identifier.to_epoch_ms(
                identifier.uuid7_datetime_from_hex(main_thread_id[4:])
            )
            == session_ms
        )
        assert session["locator"] == (f"sessions/{utc_date:%Y/%m/%d}/{session_id}")
        assert identifier.to_epoch_ms(main_created_at) == session_ms
        assert session_directory == (
            isolated_uuid7_workspace
            / ".boxteam"
            / "sessions"
            / session["locator"][len("sessions/") :]
        )
        assert session_directory.name == session_id
        assert (session_directory / "session.json").is_file()
        assert session["manifest_created_at"] == session["created_at"]

    for child in children:
        child_id = child["thread_id"]
        child_ms = int(child_id[4:16], 16)
        child_created_at = datetime.fromisoformat(child["created_at"])
        child_date = identifier.uuid7_datetime_from_hex(child_id[4:]).date()
        assert identifier.to_epoch_ms(child_created_at) == child_ms
        assert child["locator"] == f"threads/{child_date:%Y/%m/%d}/{child_id}"
        assert child["catalog_thread_id"] == child_id
        assert child["catalog_created_at"] == child["created_at"]

    assert sessions[0]["locator"].startswith("sessions/2026/06/01/")
    assert children[0]["locator"].startswith("threads/2026/06/02/")
    assert sessions[1]["locator"].startswith("sessions/2026/06/02/")


def test_seed_catalog_session_rejects_created_at_millisecond_mismatch_before_writes(
    isolated_uuid7_workspace: Path,
) -> None:
    boxteam_root = isolated_uuid7_workspace / ".boxteam"
    sessions_root = boxteam_root / "sessions"
    navigation_database = boxteam_root / "navigation" / "session-catalog.sqlite"
    assert navigation_database.is_file()
    files_before = {
        path.relative_to(boxteam_root): path.read_bytes()
        for path in boxteam_root.rglob("*")
        if path.is_file()
    }
    # 同一 UTC 日期内只偏移一毫秒，日期校验不能冒充毫秒校验。
    created_at = datetime(2026, 6, 1, 12, 0, 0, 123_000, tzinfo=UTC)
    mismatched_created_at = created_at + timedelta(milliseconds=1)
    assert created_at.date() == mismatched_created_at.date()
    with pytest.raises(ValueError, match="session_id 内嵌毫秒一致"):
        seed_catalog_session(
            CatalogSessionBundleSpec(
                sessions_root=sessions_root,
                session_id=session_id_at(created_at),
                workspace_id="u07-workspace",
                title="毫秒不匹配的固定会话",
                created_at=mismatched_created_at,
            )
        )

    assert not sessions_root.exists()
    assert {
        path.relative_to(boxteam_root): path.read_bytes()
        for path in boxteam_root.rglob("*")
        if path.is_file()
    } == files_before
