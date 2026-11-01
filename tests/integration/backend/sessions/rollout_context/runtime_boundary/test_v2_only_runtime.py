"""完整产品工作负载复用原 owner，只增加全新进程的动态导入拒绝证明。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.harness.python.run_context import TestRunContext
from tests.integration.backend.sessions.rollout_context.test_execution_history_storage import (
    history_target as history_target,  # noqa: PLC0414 — fixture按本文件隔离全部资源。
)
from tests.integration.backend.sessions.rollout_context.test_execution_history_storage import (
    test_same_workload_tools_restart_and_storage_measurements as test_same_workload_tools_restart_and_storage_measurements,  # noqa: PLC0414 — 原20次请求、工具配对、hash及历史断言完整复用。
)

_LEGACY = "app.services.infrastructure.rollout_context.migration"
_BACKEND = "tests.integration.backend.sessions.rollout_context._history_storage_app"
_OWNERS = {
    "app.services.infrastructure.rollout_context.checkpoint.saver",
    "app.services.infrastructure.rollout_context.storage.service",
    "app.services.mapping.itemized",
}


@pytest.fixture(autouse=True)
def v2_runtime_imports(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch,
) -> Iterator[None]:
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    audit = context.artifacts_dir / context.runtime_root_for_node(request.node.nodeid).parent.name / "v2-import-audit"
    audit.mkdir(parents=True, exist_ok=False)
    hook = Path(request.node.path).resolve().parent
    pythonpath = os.pathsep.join(filter(None, (str(hook), os.environ.get("PYTHONPATH"))))
    monkeypatch.setenv("PYTHONPATH", pythonpath)
    monkeypatch.setenv("BOXTEAM_TEST_V2_IMPORT_AUDIT", str(audit))
    # 独立探针实际触发旧导入，并故意捕获拒绝，证明观察不会因业务捕获而消失。
    probe = subprocess.run(
        [sys.executable, "-c", (
            "import importlib,os\n"
            "print(os.getpid(),flush=True)\n"
            "try:\n"
            f" importlib.import_module('{_LEGACY}.importer')\n"
            "except ModuleNotFoundError as error:\n"
            f" assert error.name == '{_LEGACY}',repr(error)\n"
            "else:\n"
            " raise AssertionError('一次性迁移链未被拒绝')\n"
        )], check=True, capture_output=True, text=True, timeout=15,
    )
    probe_path = audit / f"{int(probe.stdout.strip())}.jsonl"
    probe_events = [json.loads(line) for line in probe_path.read_text().splitlines()]
    assert probe_events[0] == {"event": "ready", "module": "sitecustomize"}
    assert [event for event in probe_events if event["event"] == "blocked"] == [
        {"event": "blocked", "module": _LEGACY},
    ]
    yield
    processes = {
        path.stem: [json.loads(line) for line in path.read_text().splitlines()]
        for path in sorted(audit.glob("*.jsonl")) if path != probe_path
    }
    backends = {
        pid: events for pid, events in processes.items()
        if {"event": "observed", "module": _BACKEND} in events
    }
    (audit / "summary.json").write_text(json.dumps({
        "probe_pid": probe_path.stem, "processes": processes,
        "backend_pids": list(backends),
    }, ensure_ascii=False, indent=2))
    assert len(backends) == 4, "必须实际覆盖首次Backend和三次进程重启"
    for pid, events in processes.items():
        assert events[0] == {"event": "ready", "module": "sitecustomize"}, pid
        assert not any(event["event"] == "blocked" for event in events), (pid, events)
    for pid, events in backends.items():
        assert _OWNERS <= {event["module"] for event in events if event["event"] == "observed"}, pid
