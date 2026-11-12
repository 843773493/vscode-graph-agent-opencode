"""测试阶段的唯一标量记录入口；业务观察器只补充事实，不改变被测调用。"""
from __future__ import annotations

import json
import os
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from time import perf_counter_ns, process_time_ns
from uuid import uuid4

from tests.harness.python.run_context import TestRunContext

Scalar = str | int | float | bool | None
_parent: ContextVar[str | None] = ContextVar("performance_parent", default=None)
_active: Perf | None = None


def active_perf() -> Perf | None:
    return _active


def set_active(perf: Perf | None) -> None:
    global _active
    _active = perf


class Perf:
    def __init__(self, context: TestRunContext, *, mode: str = "basic", test_id: str | None = None) -> None:
        self.mode = mode
        self.directory = context.performance_dir
        self.path = self.directory / f"performance-{os.getpid()}-{uuid4().hex[:8]}.jsonl"
        self._stream = None
        self._lock = threading.Lock()
        self._bytes = 0
        self._dropped = 0
        self._limit = int(os.getenv("BOXTEAM_TEST_PERF_LIMIT", str(64 * 1024 * 1024)))
        self.identity = {
            "schema_version": 1,
            "run_id": os.getenv("BOXTEAM_TEST_RUN_ID", context.run_root.name),
            "suite_id": os.getenv("BOXTEAM_TEST_SUITE"),
            "test_id": test_id or context.test_file.relative_to(context.project_root).as_posix(),
            "worker_id": os.getenv("PYTEST_XDIST_WORKER", "main"),
            "pid": os.getpid(),
            "clock": "perf_counter_ns",
        }
        if mode != "off":
            self.directory.mkdir(parents=True, exist_ok=True)
            self._stream = self.path.open("x", encoding="utf-8", buffering=1)
            try:
                suite_dir = os.getenv("BOXTEAM_TEST_PERF_DIR")
                if suite_dir:
                    links = Path(suite_dir) / "links"
                    links.mkdir(parents=True, exist_ok=True)
                    (links / f"{uuid4().hex}.json").write_text(
                        json.dumps({"path": str(self.path.relative_to(context.project_root))}) + "\n", encoding="utf-8",
                    )
                self.record("capabilities", "collection", mode=mode, process_resources="external_runner" if suite_dir else "not_enabled")
            except BaseException:
                self._stream.close()
                self._stream = None
                raise

    def record(self, event: str, stage: str, *, workload: dict[str, Scalar] | None = None, **fields: Scalar) -> None:
        if self._stream is None:
            return
        if any(not isinstance(value, (str, int, float, bool, type(None))) for value in (*fields.values(), *(workload or {}).values())):
            raise TypeError("性能记录只接受标量，不接受业务对象或正文")
        line = json.dumps({**self.identity, "event": event, "stage": stage, "monotonic_ns": perf_counter_ns(),
                           "workload": workload or {}, **fields}, ensure_ascii=False, separators=(",", ":")) + "\n"
        with self._lock:
            if self._bytes + len(line.encode("utf-8")) > self._limit:
                self._dropped += 1
                return
            self._stream.write(line)
            self._stream.flush()
            self._bytes += len(line.encode("utf-8"))

    @contextmanager
    def stage(self, name: str, *, workload: dict[str, Scalar] | None = None,
              context: dict[str, Scalar] | None = None) -> Iterator[dict[str, Scalar]]:
        result: dict[str, Scalar] = {}
        if self.mode == "off":
            yield result
            return
        identity = uuid4().hex
        parent = _parent.get()
        token = _parent.set(identity)
        start, cpu = perf_counter_ns(), process_time_ns()
        try:
            self.record("start", name, stage_id=identity, parent_stage_id=parent, inclusive=True, workload=workload, **(context or {}))
        except BaseException:
            _parent.reset(token)
            raise
        status, error_type = "passed", None
        failure: BaseException | None = None
        try:
            yield result
        except BaseException as error:
            failure = error
            status = "cancelled" if type(error).__name__ == "CancelledError" else "failed"
            error_type = type(error).__name__
            raise
        finally:
            _parent.reset(token)
            try:
                self.record("end", name, stage_id=identity, parent_stage_id=parent, inclusive=True,
                            status=status, error_type=error_type, elapsed_ns=perf_counter_ns() - start,
                            process_cpu_ns=process_time_ns() - cpu, resource_scope="shared_process", workload=workload, **(context or {}), **result)
            except Exception as capture_error:
                if failure is None:
                    raise
                failure.add_note(f"性能阶段完成记录失败: {capture_error!r}")
                print(f"性能阶段完成记录失败: {capture_error!r}", file=sys.stderr)

    def close(self) -> None:
        if self._stream is not None:
            stream = self._stream
            self._stream = None
            try:
                stream.write(json.dumps({**self.identity, "event": "closed", "stage": "collection",
                                         "dropped_records": self._dropped}) + "\n")
                stream.flush()
            finally:
                stream.close()


@contextmanager
def stage(name: str, **workload: Scalar) -> Iterator[dict[str, Scalar]]:
    perf = active_perf()
    if perf is None:
        yield {}
    else:
        context = {key: workload.pop(key) for key in tuple(workload)
                   if key in {"session_id", "thread_id", "turn_id", "execution_id", "model_call_id", "plan_key"}}
        with perf.stage(name, workload=workload, context=context) as result:
            yield result
