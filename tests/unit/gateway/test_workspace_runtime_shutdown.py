from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from app.gateway.registry import GatewayWorkspaceRegistry, WorkspaceTarget
from app.gateway.runtime.process_lifecycle import ManagedProcess
from app.gateway.runtime.workspace import WorkspaceRuntime


class ProcessProbe:
    pid = 100_003
    def __init__(self, name: str, events: list[str]) -> None:
        self.name = name
        self.events = events
        self.alive = True
        self.failure = False

    def is_alive(self) -> bool:
        return self.alive

    def request_terminate(self) -> None:
        self.events.append(f"terminate:{self.name}")

    def close(self, *, timeout_seconds: float = 8) -> None:
        assert timeout_seconds > 0
        self.events.append(f"close:{self.name}")
        if self.failure:
            raise RuntimeError(f"{self.name} 尚未关闭")
        self.alive = False

    def detach(self) -> None:
        self.events.append(f"detach:{self.name}")


@pytest.fixture
def runtime_bundle(tmp_path: Path, managed_backend_factory):
    events: list[str] = []
    handles = {name: ProcessProbe(name, events) for name in (
        "terminal_manager", "browser_manager", "workspace_api",
    )}
    target = WorkspaceTarget(
        workspace_id="managed", name="managed", root_path=str(tmp_path),
        backend_url="http://127.0.0.1:41000", connection_kind="local",
        backend_workspace_id="00000000-0000-4000-8000-000000000001", managed=True,
    )
    registry = GatewayWorkspaceRegistry(storage_path=tmp_path / "gateway.json")
    registry.upsert(target)
    graph = managed_backend_factory(
        (target,), backend_url=target.backend_url, process=handles["workspace_api"],
        auxiliary_processes={"managed": {name: handles[name] for name in ("terminal_manager", "browser_manager")}},
    )
    graph.publish(registry)
    return registry, graph, handles, events


def test_auxiliary_services_survive_until_backend_has_completed_close(runtime_bundle) -> None:
    registry, graph, handles, events = runtime_bundle
    registry.close()
    backend_closed = events.index("close:workspace_api")
    for name in ("terminal_manager", "browser_manager"):
        assert all(index > backend_closed for index, event in enumerate(events) if event == f"terminate:{name}")
        assert events.index(f"close:{name}") > backend_closed
        assert not handles[name].alive
    assert graph.owner.process is None
    assert graph.runtimes["managed"].processes == {}


def test_failed_backend_retains_all_handles_and_auxiliaries_for_retry(runtime_bundle) -> None:
    registry, graph, handles, events = runtime_bundle
    handles["workspace_api"].failure = True
    with pytest.raises(RuntimeError, match="workspace_api"):
        registry.close()
    assert graph.owner.process is handles["workspace_api"]
    assert graph.runtimes["managed"].processes == {name: handles[name] for name in ("terminal_manager", "browser_manager")}
    assert not any(event.endswith((":terminal_manager", ":browser_manager")) for event in events)
    assert all(handle.alive for handle in handles.values())
    handles["workspace_api"].failure = False
    registry.close()
    assert graph.owner.process is None
    assert graph.runtimes["managed"].processes == {}
    assert all(not handle.alive for handle in handles.values())


def test_registry_retains_failed_auxiliary_runtime_until_retry(runtime_bundle) -> None:
    registry, graph, handles, _ = runtime_bundle
    runtime = graph.runtimes["managed"]
    handles["browser_manager"].failure = True
    with pytest.raises(RuntimeError, match="browser_manager"):
        registry.close()
    assert registry._runtimes["managed"] is runtime
    assert runtime.processes == {"browser_manager": handles["browser_manager"]}
    assert graph.owner.process is None
    assert not handles["workspace_api"].alive
    assert not handles["terminal_manager"].alive
    handles["browser_manager"].failure = False
    registry.close()
    assert registry._runtimes == {}
    assert runtime.processes == {}


@pytest.mark.skipif(os.name != "posix", reason="需要独立 POSIX 进程组")
def test_forced_backend_close_never_becomes_graceful_on_retry() -> None:
    child = subprocess.Popen(
        [sys.executable, "-u", "-c", "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); print('ready', flush=True); time.sleep(60)"],
        stdout=subprocess.PIPE, text=True, start_new_session=True,
    )
    handle = ManagedProcess(process=child, log_file=io.StringIO())
    try:
        assert child.stdout is not None
        assert child.stdout.readline().strip() == "ready"
        with pytest.raises(TimeoutError) as initial:
            handle.close(timeout_seconds=0.1)
        assert child.poll() is not None, "强杀后必须已核实回收"
        with pytest.raises(TimeoutError) as retried:
            handle.close(timeout_seconds=0.1)
        assert retried.value is initial.value
    finally:
        if child.poll() is None:
            os.killpg(child.pid, 9)
            child.wait(timeout=5)
        if child.stdout is not None:
            child.stdout.close()


@pytest.fixture
def surviving_group_probe() -> str:
    # subreaper 只作用于独立探针；实例回执供外层超时后的精确回收。
    return '''
import ctypes, io, json, os, signal, subprocess, sys, threading
from pathlib import Path
from app.gateway.runtime.process_lifecycle import AdoptedManagedProcess, ManagedProcess

assert ctypes.CDLL(None, use_errno=True).prctl(36, 1, 0, 0, 0) == 0
manifest = Path(sys.argv[2])
source = """import json, os, subprocess, sys, time
child = subprocess.Popen([sys.executable, '-u', '-c',
    "import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);print('ready',flush=True);time.sleep(30)"],
    stdout=subprocess.PIPE, text=True)
assert child.stdout.readline().strip() == 'ready'
print(json.dumps({'child': child.pid, 'pgid': os.getpgrp()}), flush=True)
time.sleep(30)
"""
if sys.argv[1] == 'unreported_child':
    source = source.rsplit('time.sleep(30)', 1)[0] + 'sys.exit(0)\\n'
root = None
child = None
instances = []
def register(pid, group):
    fields = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
    assert os.getpgid(pid) == group
    instances.append({'pid': pid, 'group': group, 'starttime': fields[19]})
    pending = manifest.with_suffix('.next')
    pending.write_text(json.dumps(instances))
    pending.replace(manifest)
def child_active():
    if child is None:
        return False
    path = Path(f'/proc/{child}/stat')
    if not path.exists():
        return False
    fields = path.read_text().rsplit(')', 1)[1].split()
    assert fields[19] == instances[-1]['starttime']
    return fields[0] not in {'Z', 'X'}
try:
    root = subprocess.Popen([sys.executable, '-u', '-c', source],
        stdout=subprocess.PIPE, text=True, start_new_session=True)
    register(root.pid, root.pid)
    owned = json.loads(root.stdout.readline())
    if sys.argv[1] == 'unreported_child':
        root.wait(timeout=3)
        raise RuntimeError('injected before child receipt')
    assert owned['pgid'] == root.pid
    child = owned['child']
    register(child, root.pid)
    handle = (ManagedProcess(root, io.StringIO()) if sys.argv[1] == 'managed'
              else AdoptedManagedProcess(root.pid))
    if sys.argv[1] == 'adopted':
        threading.Thread(target=root.wait, daemon=True).start()
    try:
        handle.close(timeout_seconds=0.15)
    except TimeoutError as error:
        original = error
    else:
        raise AssertionError('根退出但活子进程仍在，不得伪报关闭成功')
    assert root.poll() is not None
    assert not child_active(), '强制收尾后同组活子进程仍存在'
    try:
        handle.close(timeout_seconds=0.15)
    except TimeoutError as error:
        assert error is original
    else:
        raise AssertionError('重试不能洗掉强制回收的失败事实')
    print(json.dumps({'kind': sys.argv[1], 'root_exited': True,
                      'child_active': False, 'failure_preserved': True}))
finally:
    if root is not None:
        # root 可能已退出，child 也可能尚未登记；原组必须整体回收。
        leader = Path(f'/proc/{root.pid}/stat')
        if instances and leader.exists():
            assert leader.read_text().rsplit(')', 1)[1].split()[19] == instances[0]['starttime']
        try:
            os.killpg(root.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        root.wait(timeout=3)
        while True:
            try:
                os.waitpid(-root.pid, 0)
            except ChildProcessError:
                break
        root.stdout.close()
'''


@pytest.mark.skipif(sys.platform != "linux", reason="需要 Linux 进程组与独立 subreaper")
@pytest.mark.parametrize("kind", ["managed", "adopted"])
def test_root_exit_does_not_hide_surviving_group_or_clear_failure(
    surviving_group_probe: str, kind: str, tmp_path: Path,
) -> None:
    manifest = tmp_path / "owned-group.json"
    try:
        result = subprocess.run(
            [sys.executable, "-c", surviving_group_probe, kind, str(manifest)],
            capture_output=True, text=True, timeout=15, check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert json.loads(result.stdout) == {
            "kind": kind, "root_exited": True, "child_active": False, "failure_preserved": True,
        }
    finally:
        if manifest.exists():
            leader = json.loads(manifest.read_text())[0]
            if _probe_group_members(leader):
                try:
                    os.killpg(leader["group"], 9)
                except ProcessLookupError:
                    pass


def _probe_group_members(leader: dict[str, object]) -> list[int]:
    """root 回执定位原组，即使 child PID 尚未登记也能精确发现成员。"""
    group = leader["group"]
    leader_path = Path(f"/proc/{leader['pid']}/stat")
    try:
        fields = leader_path.read_text().rsplit(")", 1)[1].split()
    except (FileNotFoundError, ProcessLookupError):
        fields = None
    if fields is not None:
        assert fields[19] == leader["starttime"], "拒绝回收被替换的 leader 实例"
        assert int(fields[2]) == group and int(fields[3]) == group
    members = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdecimal():
            continue
        try:
            fields = (entry / "stat").read_text().rsplit(")", 1)[1].split()
        except (FileNotFoundError, ProcessLookupError):
            continue
        if int(fields[2]) == group and int(fields[3]) == group and fields[0] not in {"Z", "X", "x"}:
            members.append(int(entry.name))
    return members


@pytest.mark.skipif(sys.platform != "linux", reason="需要 Linux 进程组与独立 subreaper")
def test_probe_cleans_unreported_child_after_root_has_exited(
    surviving_group_probe: str, tmp_path: Path,
) -> None:
    manifest = tmp_path / "owned-group.json"
    try:
        result = subprocess.run(
            [sys.executable, "-c", surviving_group_probe, "unreported_child", str(manifest)],
            capture_output=True, text=True, timeout=15, check=False,
        )
        assert result.returncode == 1
        assert "injected before child receipt" in result.stderr
        instances = json.loads(manifest.read_text())
        assert len(instances) == 1, "异常发生时只有 root 回执，child 尚未登记"
        assert _probe_group_members(instances[0]) == [], result.stdout + result.stderr
    finally:
        if manifest.exists():
            leader = json.loads(manifest.read_text())[0]
            if _probe_group_members(leader):
                try:
                    os.killpg(leader["group"], 9)
                except ProcessLookupError:
                    pass


@pytest.mark.parametrize("preserve_backend", [False, True])
def test_unpublished_candidate_is_closed_before_auxiliary_cleanup_and_failure_is_retryable(
    runtime_bundle, preserve_backend: bool,
) -> None:
    registry, graph, handles, events = runtime_bundle
    failed = ProcessProbe("candidate-terminal", events)
    failed.failure = True
    candidate = WorkspaceRuntime(
        service_urls={"terminal_manager": "http://127.0.0.1:41011"},
        processes={"terminal_manager": failed},
    )
    graph.owner.unpublished_runtimes["new-member"] = candidate
    with pytest.raises(RuntimeError, match="candidate-terminal"):
        registry.close(preserve_workspace_backends=preserve_backend)
    assert graph.owner.process is None
    assert events.index("close:workspace_api") < events.index("close:candidate-terminal")
    assert graph.owner.unpublished_runtimes == {"new-member": candidate}
    assert candidate.processes == {"terminal_manager": failed}
    assert handles["terminal_manager"].alive and handles["browser_manager"].alive
    assert "detach:workspace_api" not in events
    failed.failure = False
    registry.close(preserve_workspace_backends=preserve_backend)
    assert graph.owner.unpublished_runtimes == {}
    assert not failed.alive and all(not handle.alive for handle in handles.values())
