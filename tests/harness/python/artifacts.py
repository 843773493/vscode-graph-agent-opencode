"""pytest 证据归属、失败现场保留及成功运行的临时目录回收。"""
from __future__ import annotations

import json
import os
import sys
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tests.harness.python.retention import RunRetention, write_receipt as _write
from tests.harness.python.run_context import (
    TestRunContext,
    _PROCESS_RUN_ID,
    _PROCESS_STARTED_AT,
    _node_directory_name,
    test_path_observer,
)

_fixture: ContextVar[pytest.FixtureDef | None] = ContextVar("artifact_fixture", default=None)
_session_key: pytest.StashKey[ArtifactSession] = pytest.StashKey()


@dataclass
class NodeRun:
    context: TestRunContext
    node_id: str
    receipt: dict[str, object] = field(default_factory=dict)
    reports: dict[str, str] = field(default_factory=dict)

    @property
    def directory(self) -> Path:
        return self.context.run_root / "nodes" / _node_directory_name(self.node_id)

    def save(self) -> None:
        _write(self.directory / "run.json", self.receipt)


@dataclass
class WorkingState:
    context: TestRunContext
    paths: set[Path] = field(default_factory=set)
    leases: set[pytest.FixtureDef] = field(default_factory=set)
    pending: list[NodeRun] = field(default_factory=list)
    claimed: bool = False
    blocked: bool = False

    @property
    def marker(self) -> Path:
        return self.context.output_root / "runtime" / "workspace-owner.json"

    def preserve(self, current: NodeRun) -> None:
        if not self.pending or self.leases or self.blocked:
            return
        target = self.pending[0].directory / "state"
        target.mkdir(parents=True, exist_ok=False)
        paths = {}
        # 装具已退出，整目录改名同时保留 SQLite 主文件、WAL 和原始其它文件。
        sources = sorted(self.paths, key=lambda path: len(path.parts))
        for source in sources:
            if any(source.is_relative_to(parent) for parent in sources if parent != source):
                continue
            if source.exists():
                destination = target / source.relative_to(self.context.output_root)
                destination.parent.mkdir(parents=True, exist_ok=True)
                source.rename(destination)
                paths[str(source)] = str(destination.relative_to(self.context.output_root))
        for failed in self.pending:
            failed.receipt.update(
                state="saved", state_paths=paths,
                state_boundary="node_end" if failed is current else "shared_fixture_end",
            )
            failed.save()
        self.pending.clear()

    def finish_scope(self, current: NodeRun) -> None:
        if not self.claimed:
            return
        self.preserve(current)
        if not self.leases and not self.blocked:
            self.marker.unlink()
            self.claimed = False
            self.paths.clear()


class ArtifactSession:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.files: dict[Path, WorkingState] = {}
        self.nodes: list[NodeRun] = []
        self.runtime_paths: dict[Path, set[Path | None]] = {}
        self.fixture_states: dict[pytest.FixtureDef, set[Path]] = {}
        self.ready_states: set[Path] = set()
        self.current: NodeRun | None = None
        self.artifact_paths: set[Path] = set()
        context = TestRunContext.from_test_file(Path("tests/conftest.py"), project_root=root)
        self.retention = RunRetention(root, context.run_root / "artifacts/pytest-session.json")
        self.observer_token = None
        self.environment = pytest.MonkeyPatch()

    def observe(self, path: Path, kind: str) -> None:
        path = path.absolute()
        if kind == "artifact":
            if path.is_relative_to(self.root / "out/tests"):
                self.artifact_paths.add(path)
            return
        node = self.current
        if node is None:
            return
        if kind == "runtime":
            if path.is_relative_to(self.root / "out/tests"):
                owners = self.runtime_paths.setdefault(path, set())
                owners.add(node.context.output_root)
                fixture = _fixture.get()
                if fixture is not None and fixture.scope == "session":
                    owners.add(None)
            return
        state = self.files[node.context.output_root]
        if not path.is_relative_to(node.context.output_root):
            return
        relative = path.relative_to(node.context.output_root)
        if not relative.parts or relative.parts[0] in {"runtime", "runs", "artifacts"}:
            return
        if node.context.output_root in self.ready_states and not state.leases:
            # 参数切换会在 setup 内关闭旧 fixture；必须在新 fixture 清空同路径前归档。
            state.finish_scope(node)
            self.ready_states.discard(node.context.output_root)
        if state.blocked:
            raise RuntimeError(f"前一用例未完成资源清理，禁止重建现场: {state.marker}")
        if not state.claimed:
            state.marker.parent.mkdir(parents=True, exist_ok=True)
            # 排他创建让异常退出的现场先被看见；不自动删除或接管旧 owner。
            try:
                with state.marker.open("x", encoding="utf-8") as stream:
                    json.dump({"schema_version": 1, "pid": os.getpid(),
                               "run": str(node.directory / "run.json")}, stream)
            except FileExistsError as error:
                raise RuntimeError(
                    f"工作区仍被占用或上次运行未完成，已保护原现场。请核对 owner 后显式归档: {state.marker}",
                ) from error
            state.claimed = True
        state.paths.add(path)
        owner = _fixture.get()
        if owner is not None and owner.scope != "function":
            state.leases.add(owner)
            self.fixture_states.setdefault(owner, set()).add(node.context.output_root)
        if node.receipt["state"] != "working":
            node.receipt["state"] = "working"
            node.save()

    def release(self, fixture: pytest.FixtureDef) -> None:
        for root in self.fixture_states.pop(fixture, ()):
            state = self.files[root]
            state.leases.discard(fixture)
            if not state.leases:
                self.ready_states.add(root)

    def start(self, item: pytest.Item) -> NodeRun:
        context = TestRunContext.from_test_file(Path(item.path), project_root=self.root)
        node = NodeRun(context, item.nodeid)
        node.receipt = {
            "schema_version": 1, "owner": "tests.harness.python.artifacts",
            "run_id": context.run_root.name, "node_id": item.nodeid,
            "status": "running", "started_at": datetime.now(UTC).isoformat(),
            "reports": node.reports, "state": "not_saved", "runtime": "retained",
            "artifacts": str((node.directory / "artifacts").relative_to(context.output_root)),
            "performance": str(context.performance_dir_for_node(item.nodeid).relative_to(context.output_root)),
            "suite_receipt": str(Path(os.environ["BOXTEAM_TEST_PERF_DIR"]) / "run.json") if os.getenv("BOXTEAM_TEST_PERF_DIR") else None,
            "preserve_state": item.get_closest_marker("preserve_test_state") is not None,
        }
        self.current = node
        if context.output_root not in self.files:
            self.retention.start(context.run_root, node.receipt["started_at"])
        state = self.files.setdefault(context.output_root, WorkingState(context))
        if state.claimed:
            node.receipt["state"] = "working"
        self.nodes.append(node)
        node.directory.mkdir(parents=True, exist_ok=True)
        # 同一 run/node 被重复执行时明确拒绝，不能覆盖旧收据和现场。
        with (node.directory / "run.json").open("x", encoding="utf-8") as stream:
            json.dump(node.receipt, stream, ensure_ascii=False)
        if state.claimed:
            _write(state.marker, {"schema_version": 1, "pid": os.getpid(),
                                  "run": str(node.directory / "run.json")})
        _write(context.output_root / "latest.json", {
            "schema_version": 1, "run": str((node.directory / "run.json").relative_to(context.output_root)),
        })
        return node

    def finish(self, node: NodeRun, protocol_completed: bool) -> None:
        state = self.files[node.context.output_root]
        complete = protocol_completed and "setup" in node.reports and "teardown" in node.reports and (
            node.reports["setup"] != "passed" or "call" in node.reports
        )
        status = "incomplete" if not complete else (
            "failed" if "failed" in node.reports.values() else
            "skipped" if "skipped" in node.reports.values() else "passed"
        )
        node.receipt.update(status=status, finished_at=datetime.now(UTC).isoformat())
        if node.receipt["state"] == "working" and (
            status in {"failed", "incomplete"} or node.receipt["preserve_state"]
        ):
            state.pending.append(node)
        if not complete or node.reports.get("teardown") == "failed":
            for root in self.ready_states | {node.context.output_root}:
                blocked = self.files[root]
                blocked.blocked = True
                for pending in blocked.pending:
                    pending.receipt["state"] = "retained_in_place"
                    pending.save()
        for root in self.ready_states | {node.context.output_root}:
            self.files[root].finish_scope(node)
        self.ready_states.clear()
        if node.receipt["state"] == "working":
            node.receipt["state"] = "retained_in_place" if state.blocked else (
                "deferred_shared_fixture" if any(pending is node for pending in state.pending) else "not_saved"
            )
        node.save()
        pointer = {"schema_version": 1, "run": str((node.directory / "run.json").relative_to(node.context.output_root))}
        if status in {"failed", "incomplete"}:
            _write(node.context.output_root / "latest-failed.json", pointer)
        self.current = None

    def close(self, exitstatus: int) -> None:
        if not self.nodes and not self.artifact_paths and not self.runtime_paths:
            return
        # 所有装具退出后，当前及历史运行复用同一套回收入口。
        finished = datetime.now(UTC).isoformat()
        files = {}
        owned_artifacts = set()
        by_root = {}
        for root, state in self.files.items():
            nodes = [node for node in self.nodes if node.context.output_root == root]
            status = "incomplete" if exitstatus not in {0, 1} or state.blocked else (
                "failed" if any(node.receipt["status"] == "failed" for node in nodes) else
                "skipped" if all(node.receipt["status"] == "skipped" for node in nodes) else "passed"
            )
            path = state.context.run_root / "run.json"
            by_root[root] = str(path.relative_to(self.root))
            performance = state.context.performance_dir_for_node(None)
            owned_artifacts.update((state.context.run_root, performance))
            files[path] = {
                "status": status, "started_at": nodes[0].receipt["started_at"], "finished_at": finished,
                "pinned": any(node.receipt["preserve_state"] for node in nodes),
                "retain_runtime": status in {"failed", "incomplete"},
                "nodes": [str((node.directory / "run.json").relative_to(root)) for node in nodes],
                "last_node": str((nodes[-1].directory / "run.json").relative_to(root)),
                "last_failed_node": next((str((node.directory / "run.json").relative_to(root)) for node in reversed(nodes)
                                          if node.receipt["status"] in {"failed", "incomplete"}), None),
                "performance": [str(performance.relative_to(root))],
            }
        shared = {path for path in self.artifact_paths if not any(path.is_relative_to(owned) for owned in owned_artifacts)}
        # 父目录已经登记时不再重复删除其子目录。
        shared = {path for path in shared if not any(path != other and path.is_relative_to(other) for other in shared)}
        self.retention.finish(files, {
            "command": sys.orig_argv, "test_exit_code": exitstatus,
            "nodes": [str((node.directory / "run.json").relative_to(self.root)) for node in self.nodes],
            "shared_artifacts": [str(path.relative_to(self.root)) for path in shared],
            "runtime_resources": [{"path": str(path.relative_to(self.root)), "shared": None in owners,
                                   "files": [by_root[owner] for owner in owners if owner is not None]}
                                  for path, owners in self.runtime_paths.items()],
        }, prune=exitstatus in {0, 1} and bool(self.nodes))


@pytest.hookimpl(tryfirst=True)
def pytest_sessionstart(session: pytest.Session) -> None:
    state = ArtifactSession(Path.cwd().resolve())
    session.config.stash[_session_key] = state
    # 直接 pytest 也向子进程传递同一身份和 UTC 日期，避免 Python/JS 各建一轮产物。
    state.environment.setenv("BOXTEAM_TEST_RUN_ID", (
        os.getenv("BOXTEAM_TEST_RUN_ID") or os.getenv("PYTEST_XDIST_TESTRUNUID") or _PROCESS_RUN_ID
    ))
    state.environment.setenv("BOXTEAM_TEST_RUN_STARTED_AT", os.getenv("BOXTEAM_TEST_RUN_STARTED_AT") or _PROCESS_STARTED_AT)
    state.observer_token = test_path_observer.set(state.observe)


def pytest_unconfigure(config: pytest.Config) -> None:
    state = config.stash.get(_session_key, None)
    if state is not None:
        if state.observer_token is not None:
            test_path_observer.reset(state.observer_token)
        state.environment.undo()


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "preserve_test_state: 成功后也保留工作区现场和本次运行的 runtime")


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtest_protocol(item: pytest.Item, nextitem: pytest.Item | None):
    session = item.config.stash[_session_key]
    node = session.start(item)
    token = test_path_observer.set(session.observe)
    completed = False
    try:
        result = yield
        completed = True
        return result
    except BaseException as error:
        node.receipt["protocol_error"] = f"{type(error).__name__}: {error}"
        raise
    finally:
        test_path_observer.reset(token)
        session.finish(node, completed)


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_fixture_setup(fixturedef: pytest.FixtureDef, request: pytest.FixtureRequest):
    token = _fixture.set(fixturedef)
    try:
        return (yield)
    finally:
        _fixture.reset(token)


def pytest_fixture_post_finalizer(fixturedef: pytest.FixtureDef, request: pytest.FixtureRequest) -> None:
    session = request.config.stash[_session_key]
    session.release(fixturedef)


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo):
    report = yield
    node = item.config.stash[_session_key].current
    if node is not None:
        node.reports[report.when] = report.outcome
        output = "\n".join(f"{title}\n{content}" for title, content in report.sections
                           if title.endswith(f" {report.when}"))
        if report.failed:
            output += "\n" + report.longreprtext
        if output:
            directory = node.directory / "artifacts"
            directory.mkdir(parents=True, exist_ok=True)
            (directory / f"pytest-{report.when}.log").write_text(output, encoding="utf-8")
    return report


@pytest.hookimpl(trylast=True)
def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    session.config.stash[_session_key].close(int(exitstatus))
