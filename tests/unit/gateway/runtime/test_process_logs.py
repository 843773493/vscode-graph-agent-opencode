from __future__ import annotations

import asyncio
import errno
import io
import json
import os
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest

from app.core import file_permissions
from app.core.logging_contract import load_runtime_log_contract
from app.gateway.runtime.process_lifecycle import (
    AdoptedManagedProcess,
    LoggedProcessCleanupError,
    ManagedProcess,
    _spawn_logged_process,
)
from app.gateway.runtime.process_logs import ProcessLogOwnership, ProcessLogStore


@pytest.fixture
def log_path_samples() -> list[dict[str, str | int | None]]:
    return json.loads((Path.cwd() / "resources/logging/path-samples.json").read_text())


def test_shared_log_contract_paths(
    tmp_path: Path, log_path_samples: list[dict[str, str | int | None]],
) -> None:
    contract = load_runtime_log_contract()
    store = ProcessLogStore(contract.root_for(tmp_path))
    for sample in log_path_samples:
        assert store.path_for(str(sample["role"]), sample["instance"]) == tmp_path / str(sample["path"])


@pytest.mark.parametrize("instance", ["", "../outside", r"C:\outside", "/etc", r"a\b", ".", "中文"])
def test_log_instance_cannot_escape_role(tmp_path: Path, instance: str) -> None:
    with pytest.raises(ValueError, match="进程日志实例"):
        ProcessLogStore(tmp_path).open("backend", instance)
    assert list(tmp_path.iterdir()) == []


def test_process_log_store_appends_and_restricts_permissions(tmp_path: Path) -> None:
    log_root = tmp_path / "gateway" / "logs"
    store = ProcessLogStore(log_root)

    with store.open("backend", 12345) as stream:
        stream.write("first\n")
    with store.open("backend", 12345) as stream:
        stream.write("second\n")

    log_path = log_root / "backend/local-backend-12345.log"
    assert log_path.read_text(encoding="utf-8") == "first\nsecond\n"
    if os.name == "posix":
        assert log_path.parent.stat().st_mode & 0o777 == 0o700
        assert log_path.stat().st_mode & 0o777 == 0o600


def test_process_log_store_uses_private_windows_acl(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[Path] = []

    def set_acl(path: Path, *, is_directory: bool) -> None:
        calls.append(path)

    monkeypatch.setattr(file_permissions, "_is_windows", lambda: True)
    monkeypatch.setattr(
        file_permissions.windows_fs,
        "private_acl_is_current",
        lambda *_args, **_kwargs: False,
    )
    monkeypatch.setattr(file_permissions.windows_fs, "set_private_acl", set_acl)

    with ProcessLogStore(tmp_path / "logs").open("gateway") as stream:
        stream.write("private log\n")

    assert calls[:3] == [
        file_permissions.native_io_path(tmp_path / "logs/gateway"),
        file_permissions.native_io_path(tmp_path / "logs/gateway/gateway.log.owner.lock"),
        file_permissions.native_io_path(tmp_path / "logs/gateway/gateway.log"),
    ]
    assert calls[3].parent == calls[0] and calls[3].name.startswith("gateway.log.owner.json.")
    assert calls[4:] == calls[1:2]


def test_process_log_store_rotates_an_oversized_previous_log(tmp_path: Path) -> None:
    log_root = tmp_path / "logs"
    log_path = log_root / "backend/local-backend-12345.log"
    log_path.parent.mkdir(parents=True)
    max_bytes = load_runtime_log_contract().max_bytes
    with log_path.open("wb") as stream:
        stream.truncate(max_bytes)

    with ProcessLogStore(log_root).open("backend", 12345) as stream:
        stream.write("new\n")

    assert log_path.read_text(encoding="utf-8") == "new\n"
    assert (
        log_root / "backend/local-backend-12345.log.1"
    ).stat().st_size == max_bytes


@pytest.mark.parametrize(
    "file_name",
    [
        "",
        "../outside.log",
        "nested/log.log",
        r"..\outside.log",
        r"nested\log.log",
        r"C:\outside.log",
        r"\\server\share.log",
        "log.txt",
    ],
)
def test_process_log_store_rejects_invalid_file_name(
    tmp_path: Path,
    file_name: str,
) -> None:
    store = ProcessLogStore(tmp_path / "logs")

    with pytest.raises(ValueError, match="进程日志") as failure:
        store.path_for(file_name)
    if file_name:
        assert file_name not in str(failure.value)


@pytest.mark.skipif(os.name != "posix", reason="Windows 符号链接权限模型不同")
def test_process_log_store_rejects_symbolic_link(tmp_path: Path) -> None:
    log_root = tmp_path / "logs"
    (log_root / "backend").mkdir(parents=True)
    outside = tmp_path / "outside.log"
    outside.write_text("outside\n", encoding="utf-8")
    (log_root / "backend/local-backend-12345.log").symlink_to(outside)

    with pytest.raises(RuntimeError, match="符号链接") as failure:
        ProcessLogStore(log_root).open("backend", 12345)
    assert str(log_root) not in str(failure.value)
    assert outside.read_text() == "outside\n"


@pytest.mark.skipif(os.name != "posix", reason="Windows 不使用 fchmod")
def test_process_log_store_closes_descriptor_when_permission_update_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_open = os.open
    opened_descriptors: list[int] = []

    def tracked_open(path: Path, flags: int, mode: int) -> int:
        descriptor = real_open(path, flags, mode)
        opened_descriptors.append(descriptor)
        return descriptor

    def fail_fchmod(_: int, __: int) -> None:
        if len(opened_descriptors) == 2:
            raise PermissionError(errno.EACCES, "测试权限更新失败", str(tmp_path / "logs/gateway.log"))

    monkeypatch.setattr(os, "open", tracked_open)
    monkeypatch.setattr(os, "fchmod", fail_fchmod)

    with pytest.raises(PermissionError, match="stage=file_permissions") as failure:
        ProcessLogStore(tmp_path / "logs").open("gateway")
    assert failure.value.errno == errno.EACCES
    assert str(tmp_path) not in str(failure.value)
    assert "测试权限更新失败" in str(failure.value.__cause__)

    assert len(opened_descriptors) == 2
    for descriptor in opened_descriptors:
        with pytest.raises(OSError):
            os.fstat(descriptor)


@pytest.mark.parametrize("stage", ["create_directory", "rotate", "open"])
def test_log_filesystem_failure_keeps_location_in_cause_and_diagnostics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture, stage: str,
) -> None:
    log_root = tmp_path / "private-log-root"
    role_dir = log_root / "gateway"
    contract = load_runtime_log_contract()
    if stage == "create_directory":
        log_root.write_text("obstruction")
    else:
        role_dir.mkdir(parents=True)
        if stage == "open":
            (role_dir / "gateway.log").mkdir()
        else:
            contract = replace(contract, max_bytes=1)
            (role_dir / "gateway.log").write_text("old")
            (role_dir / "gateway.log.2").write_text("previous")
            (role_dir / "gateway.log.3").mkdir()
    with pytest.raises(OSError, match=f"stage={stage}") as failure:
        ProcessLogStore(log_root, contract=contract).open("gateway")
    original = failure.value.__cause__
    assert isinstance(original, OSError)
    assert type(failure.value) is type(original)
    assert failure.value.errno == original.errno
    assert failure.value.filename is None
    assert str(log_root) not in str(failure.value)
    assert str(log_root) in str(original)
    assert str(log_root) in caplog.text
    if stage == "rotate":
        assert (role_dir / "gateway.log").read_text() == "old"


@pytest.mark.parametrize("marker", ["/private/runtime/node", r"C:\private\node.exe", r"\\server\private\node.exe"])
@pytest.mark.parametrize("with_filename", [False, True])
def test_spawn_logged_process_closes_log_and_preserves_native_failure(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture, marker: str, with_filename: bool,
) -> None:
    log_file = io.StringIO()
    original = (
        PermissionError(errno.EACCES, "access denied", marker)
        if with_filename else PermissionError(errno.EACCES, f"access denied: {marker}")
    )

    def fail_spawn(*_: object, **__: object) -> subprocess.Popen[str]:
        raise original

    monkeypatch.setattr(
        "app.gateway.runtime.process_lifecycle.subprocess.Popen",
        fail_spawn,
    )

    with pytest.raises(PermissionError, match="stage=spawn") as failure:
        _spawn_logged_process([marker, "credential-argument-marker"], log_file=log_file)

    assert log_file.closed is True
    assert failure.value.errno == errno.EACCES
    assert failure.value.filename is None
    assert failure.value.__cause__ is original
    assert marker not in str(failure.value)
    assert str(original) in caplog.text
    assert "credential-argument-marker" not in caplog.text
    assert any(record.exc_info and record.exc_info[1] is original for record in caplog.records)


@pytest.mark.parametrize("system_error", [False, True])
def test_log_close_failure_does_not_replace_startup_failure(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, system_error: bool,
) -> None:
    original = PermissionError(errno.EACCES, "native startup error", "/private/runtime/node") if system_error else ValueError("invalid environment")
    cleanup_error = OSError(errno.EIO, "native close error", "/private/runtime/log")

    class FailingLog(io.StringIO):
        def close(self) -> None:
            super().close()
            raise cleanup_error

    def fail_spawn(*_: object, **__: object) -> subprocess.Popen[str]:
        raise original

    monkeypatch.setattr("app.gateway.runtime.process_lifecycle.subprocess.Popen", fail_spawn)
    log_file = FailingLog()
    with pytest.raises(type(original)) as failure:
        _spawn_logged_process(["configured-node"], log_file=log_file)
    if system_error:
        assert failure.value.__cause__ is original
        assert failure.value.errno == errno.EACCES
    else:
        assert failure.value is original
    assert log_file.closed
    assert "/private/runtime" not in str(failure.value)
    assert any("error_type=OSError, errno=5" in note for note in failure.value.__notes__)
    assert all("/private/runtime" not in note for note in failure.value.__notes__)
    assert any(record.exc_info and record.exc_info[1] is cleanup_error for record in caplog.records)


@pytest.mark.skipif(os.name != "nt", reason="需要真实 Windows CreateProcess")
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_long_working_directory_starts_in_the_original_directory(
    tmp_path: Path, asynchronous: bool,
) -> None:
    from app.core.atomic_fs import native_io_path
    from app.runtime.windows_process import open_windows_process

    directory = tmp_path
    while len(str(directory)) < 280:
        directory /= "插件工作目录 with spaces"
    native_io_path(directory).mkdir(parents=True)
    command = [sys.executable, "-c",
        "from pathlib import Path; Path('relative-output.txt').write_text('same-directory', encoding='utf-8')"]
    with (tmp_path / "child.log").open("w", encoding="utf-8") as output:
        if asynchronous:
            child = open_windows_process(command, cwd=directory, env=dict(os.environ),
                stdout=output, stderr=output)
            try:
                assert await asyncio.wait_for(child.wait(), 10) == 0
            finally:
                if child.returncode is None:
                    child.kill()
                    await asyncio.wait_for(child.wait(), 10)
                await child.aclose()
        else:
            process = _spawn_logged_process(command, log_file=output, cwd=directory, detached=True)
            try:
                assert process.wait(timeout=10) == 0
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=10)
    assert (native_io_path(directory) / "relative-output.txt").read_text(encoding="utf-8") == "same-directory"


@pytest.fixture
def logged_process(tmp_path: Path):
    handles = []

    def start(script: str = "import time; time.sleep(60)", *, role: str = "backend", instance: int = 12345):
        store = ProcessLogStore(tmp_path / "logs", contract=replace(load_runtime_log_contract(), max_bytes=1))
        stream = store.open(role, instance)
        process = _spawn_logged_process([sys.executable, "-u", "-c", script], log_file=stream)
        managed = ManagedProcess(process, stream)
        handles.append(managed)
        return store, managed

    yield start
    for handle in handles:
        handle.close()


@pytest.fixture
def windows_service_log(tmp_path, monkeypatch, managed_process_stub_factory, windows_process_tree_stub):
    tree = windows_process_tree_stub
    root = tree.add_process(4321, parent_pid=None, starttime=1.0)
    worker = tree.add_process(5001, parent_pid=4321, starttime=2.0)
    store = ProcessLogStore(tmp_path / "logs")
    stream = store.open("backend", 12345)
    process = managed_process_stub_factory(timeout_once=False)
    if os.name != "nt":
        # 只模拟进程平台；日志仍使用宿主文件权限，不加载 Windows ACL。
        monkeypatch.setattr(file_permissions, "_is_windows", lambda: False)
        monkeypatch.setattr(file_permissions, "native_io_path", lambda path: path)
    monkeypatch.setattr(os, "name", "nt")
    stream.begin_spawn()
    stream.ownership.bind_process(root.pid)
    managed = ManagedProcess(process, stream)
    stopped = []
    def stop(pid, *, force):
        stopped.append((pid, force))
        assert pid == root.pid
        root.alive = worker.alive = False
        process.returncode = 0
    monkeypatch.setattr("app.gateway.runtime.process_lifecycle._taskkill_process_tree", stop)
    try:
        yield store, managed, tree, stopped
    finally:
        stream.close()
        stream.release_ownership()


def test_windows_writer_and_root_survive_detach_adopt_and_complete_close(windows_service_log) -> None:
    store, managed, tree, stopped = windows_service_log
    stream = managed.log_file
    sink = dict(stream.sink)
    inode = stream.ownership.path.stat().st_ino
    managed.confirm_service_identity(5001)
    record = stream.ownership.read()
    assert record["pid"] == 5001 and record["created_at"] == 2.0
    assert record["process_tree_root"] == {"pid": 4321, "created_at": 1.0}
    assert stream.sink == sink and stream.ownership.path.stat().st_ino == inode
    managed.detach()
    with pytest.raises(RuntimeError, match="写入者不匹配"):
        store.adopt("backend", 12345, {"pid": 4321, "log_sink": sink})
    ownership = store.adopt("backend", 12345, {"pid": 5001, "log_sink": sink})
    adopted = AdoptedManagedProcess(5001, log_ownership=ownership)
    assert adopted.pid == 5001 and adopted.is_alive()
    adopted.close(timeout_seconds=0.01)
    assert stopped == [(4321, False)]
    assert not tree.processes[4321].alive
    assert not tree.processes[5001].alive
    assert not ownership.record_path.exists()
    managed.close()


def test_windows_single_pid_record_keeps_root_equal_to_writer(windows_service_log) -> None:
    store, managed, _tree, stopped = windows_service_log
    managed.confirm_service_identity(4321)
    stream = managed.log_file
    assert "process_tree_root" not in stream.ownership.read()
    ownership = store.adopt("backend", 12345, {"pid": 4321, "log_sink": dict(stream.sink)})
    adopted = AdoptedManagedProcess(4321, log_ownership=ownership)
    adopted.close(timeout_seconds=0.01)
    assert stopped == [(4321, False)]


@pytest.mark.parametrize(("fault", "error_type"), [
    ("foreign-root", RuntimeError), ("root-recycled", RuntimeError),
    ("writer-recycled", RuntimeError), ("missing-root-pid", RuntimeError),
    ("boolean-root-time", RuntimeError), ("boolean-writer-time", RuntimeError),
    ("invalid-root", TypeError),
])
def test_windows_adoption_rejects_changed_or_incomplete_tree_identity(windows_service_log, fault, error_type) -> None:
    store, managed, tree, stopped = windows_service_log
    managed.confirm_service_identity(5001)
    stream = managed.log_file
    record = stream.ownership.read()
    if fault == "foreign-root":
        tree.add_process(6001, parent_pid=None, starttime=1.0)
        record["process_tree_root"]["pid"] = 6001
    elif fault == "root-recycled":
        record["process_tree_root"]["created_at"] = 9.0
    elif fault == "writer-recycled":
        record["created_at"] = 9.0
    elif fault == "missing-root-pid":
        record["process_tree_root"].pop("pid")
    elif fault == "boolean-root-time":
        record["process_tree_root"]["created_at"] = True
    elif fault == "boolean-writer-time":
        record["created_at"] = True
    else:
        record["process_tree_root"] = None
    stream.ownership.record_path.write_text(json.dumps(record), encoding="utf-8")
    with pytest.raises(error_type, match="不属于|已被替换|已变化|不完整|非法"):
        ownership = store.adopt("backend", 12345, {"pid": 5001, "log_sink": dict(stream.sink)})
        AdoptedManagedProcess(5001, log_ownership=ownership)
    assert stopped == [] and tree.processes[5001].alive


def test_writer_publication_failure_retains_original_record_and_owned_descendants(
    windows_service_log, monkeypatch,
) -> None:
    _store, managed, tree, stopped = windows_service_log
    stream = managed.log_file
    before = stream.ownership.record_path.read_bytes()
    original = ProcessLogOwnership.write
    failure = OSError(5, "writer publication failed")
    def fail_publication(self, payload):
        if "process_tree_root" in payload:
            raise failure
        original(self, payload)
    monkeypatch.setattr(ProcessLogOwnership, "write", fail_publication)
    with pytest.raises(OSError) as caught:
        managed.confirm_service_identity(5001)
    assert caught.value is failure
    assert managed.pid == 4321 and stream.ownership.record_path.read_bytes() == before
    tree.processes[4321].alive = False
    managed.process.returncode = 0
    managed.close(timeout_seconds=0.01)
    assert tree.processes[5001].terminate_calls == 1
    assert not tree.processes[5001].alive and stopped == []
    assert not stream.ownership.record_path.exists()


def test_active_writer_prevents_rotation_and_detach_preserves_ownership(logged_process) -> None:
    store, managed = logged_process()
    stream = managed.log_file
    stream.write("active\n")
    stream.flush()
    path = store.path_for("backend", 12345)
    inode = path.stat().st_ino
    managed.detach()
    with pytest.raises(RuntimeError, match="活动写入者"):
        store.open("backend", 12345)
    assert path.stat().st_ino == inode and path.read_text() == "active\n"
    assert not path.with_name(path.name + ".1").exists()
    managed.close()
    with store.open("backend", 12345) as reopened:
        reopened.write("new\n")
    assert path.with_name(path.name + ".1").read_text() == "active\n"


@pytest.mark.skipif(os.name != "posix", reason="POSIX 进程组后代验证")
def test_dead_root_does_not_release_descendant_writer(logged_process, tmp_path: Path) -> None:
    marker = tmp_path / "child-started"
    script = (
        "import subprocess,sys,time; from pathlib import Path; "
        "subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
        f"Path({str(marker)!r}).touch(); time.sleep(60)"
    )
    store, managed = logged_process(script)
    deadline = time.monotonic() + 5
    while not marker.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert marker.exists()
    managed.process.terminate()
    managed.process.wait(timeout=5)
    managed.detach()
    with pytest.raises(RuntimeError, match="活动写入者"):
        store.open("backend", 12345)
    managed.close()
    with store.open("backend", 12345):
        pass


def test_interrupted_spawn_is_not_assumed_to_be_idle(tmp_path: Path) -> None:
    store = ProcessLogStore(tmp_path)
    stream = store.open("backend", 12345)
    stream.begin_spawn()
    stream.close()
    with pytest.raises(RuntimeError, match="日志交付曾中断"):
        store.open("backend", 12345)
    stream.release_ownership()


def test_controller_threads_cannot_open_the_same_log_twice(tmp_path: Path) -> None:
    store = ProcessLogStore(tmp_path)
    def open_log(_: int):
        try:
            return store.open("backend", 12345)
        except RuntimeError as error:
            assert "活动写入者" in str(error)
            return None
    with ThreadPoolExecutor(max_workers=8) as pool:
        handles = [handle for handle in pool.map(open_log, range(8)) if handle is not None]
    assert len(handles) == 1
    handles[0].close()


def test_cross_process_open_cannot_rotate_an_active_file(tmp_path: Path) -> None:
    store = ProcessLogStore(tmp_path, contract=replace(load_runtime_log_contract(), max_bytes=1))
    with store.open("backend", 12345) as stream:
        stream.write("old\n")
        stream.flush()
        script = f"from pathlib import Path; from app.gateway.runtime.process_logs import ProcessLogStore; ProcessLogStore(Path({str(tmp_path)!r})).open('backend', 12345)"
        result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=10, check=False)
        assert result.returncode != 0 and "活动写入者" in result.stderr
        assert store.path_for("backend", 12345).read_text() == "old\n"
        assert not store.path_for("backend", 12345).with_suffix(".log.1").exists()


def test_adoption_keeps_file_and_releases_only_after_process_group_closes(logged_process) -> None:
    store, managed = logged_process()
    proof = {"pid": managed.process.pid, "log_sink": dict(managed.log_file.sink)}
    managed.log_file.write("history\n")
    managed.log_file.flush()
    managed.detach()
    ownership = store.adopt("backend", 12345, proof)
    adopted = AdoptedManagedProcess(managed.process.pid, log_ownership=ownership)
    adopted.detach()
    with pytest.raises(RuntimeError, match="活动写入者"):
        store.open("backend", 12345)
    # 原父进程负责 wait，避免把已退出的 zombie 当作尚未释放的进程句柄。
    managed.process.terminate()
    managed.process.wait(timeout=5)
    adopted.close()
    managed.close()
    with store.open("backend", 12345):
        pass
    assert store.path_for("backend", 12345).with_suffix(".log.1").read_text() == "history\n"


@pytest.mark.parametrize("field", ["pid", "owner_id", "inode", "path", "device", "source", "role"])
def test_adoption_rejects_changed_sink_identity(logged_process, field: str) -> None:
    store, managed = logged_process()
    proof = {"pid": managed.process.pid, "log_sink": dict(managed.log_file.sink)}
    if field == "pid":
        proof["pid"] = managed.process.pid + 1
    else:
        proof["log_sink"][field] = "changed"
    with pytest.raises(RuntimeError, match="所有权已变化|不匹配|缺少"):
        store.adopt("backend", 12345, proof)


def test_constructor_failure_releases_only_its_own_record(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*args, **kwargs):
        raise RuntimeError("构造失败")
    store = ProcessLogStore(tmp_path)
    with monkeypatch.context() as patch:
        patch.setattr("app.gateway.runtime.process_logs.ProcessLogFile", fail)
        with pytest.raises(RuntimeError, match="构造失败"):
            store.open("backend", 12345)
    with store.open("backend", 12345):
        pass


@pytest.mark.skipif(os.name != "posix", reason="Windows 符号链接权限模型不同")
@pytest.mark.parametrize("suffix", [".owner.lock", ".owner.json"])
def test_ownership_files_reject_symbolic_links(tmp_path: Path, suffix: str) -> None:
    store = ProcessLogStore(tmp_path)
    path = store.path_for("backend", 12345)
    path.parent.mkdir()
    outside = tmp_path / "outside"
    outside.write_text("unchanged")
    path.with_name(path.name + suffix).symlink_to(outside)
    with pytest.raises(RuntimeError, match="符号链接"):
        store.open("backend", 12345)
    assert outside.read_text() == "unchanged"


@pytest.mark.parametrize("runtime", ["python", "node"])
@pytest.mark.parametrize("mismatch", [None, "stdout", "stderr", "device", "inode", "path"])
def test_process_state_verifies_inherited_descriptors(
    tmp_path: Path, runtime: str, mismatch: str | None,
) -> None:
    if runtime == "python":
        command = [sys.executable, "-c", """
from dataclasses import asdict
import json
from app.core.logging_config import configure_application_logging, runtime_logging_state
configure_application_logging(service='backend')
print(json.dumps(asdict(runtime_logging_state())), flush=True)
"""]
    else:
        node = shutil.which("node")
        assert node is not None
        command = [node, "--input-type=module", "--eval", """
import { runtimeLoggingState } from './src/shared/serverRuntimeLogging.mjs';
console.log(JSON.stringify(runtimeLoggingState('terminal')));
"""]
    store = ProcessLogStore(tmp_path)
    with store.open("backend", 12345) as stream:
        sink = dict(stream.sink)
        if mismatch in {"device", "inode"}:
            sink[mismatch] = "changed"
        elif mismatch == "path":
            other = tmp_path / "other.log"
            other.write_text("other", encoding="utf-8")
            sink["path"] = str(other)
        result = subprocess.run(
            command, env={**os.environ, "PYTHONIOENCODING": "utf-8", "BOXTEAM_RUNTIME_LOG_SINK": json.dumps(sink)},
            stdout=subprocess.PIPE if mismatch == "stdout" else stream,
            stderr=subprocess.PIPE if mismatch == "stderr" else stream,
            check=False, text=True, encoding="utf-8", timeout=15,
        )
        text = store.path_for("backend", 12345).read_text(encoding="utf-8")
        if mismatch is None:
            assert result.returncode == 0, text
            state = json.loads(text)
            assert state["log_sink"] == sink
        else:
            observed = text + (result.stdout or "") + (result.stderr or "")
            assert result.returncode != 0 and "实际日志描述符与冻结去向不一致" in observed


def test_binding_failure_terminates_spawned_process_before_releasing_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = ProcessLogStore(tmp_path)
    stream = store.open("backend", 12345)
    def fail_bind(*_):
        raise PermissionError(errno.EACCES, "归属登记写入失败")
    monkeypatch.setattr("app.gateway.runtime.process_logs.ProcessLogOwnership.bind_process", fail_bind)
    with pytest.raises(PermissionError) as failure:
        _spawn_logged_process([sys.executable, "-c", "import time; time.sleep(60)"], log_file=stream)
    assert failure.value.errno == errno.EACCES
    assert stream.closed and not stream.ownership.record_path.exists()
    with store.open("backend", 12345):
        pass


def test_failed_startup_cleanup_preserves_actual_handles(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = ProcessLogStore(tmp_path)
    stream = store.open("backend", 12345)
    def fail_bind(*_):
        raise OSError(errno.EIO, "归属登记失败")
    def fail_close(*_):
        raise TimeoutError("进程组回收失败")
    with monkeypatch.context() as patch:
        patch.setattr("app.gateway.runtime.process_logs.ProcessLogOwnership.bind_process", fail_bind)
        patch.setattr(ManagedProcess, "close", fail_close)
        with pytest.raises(LoggedProcessCleanupError) as failure:
            _spawn_logged_process([sys.executable, "-c", "import time; time.sleep(60)"], log_file=stream)
    retained = failure.value
    try:
        assert retained.process.poll() is None and retained.log_file is stream
        assert isinstance(retained.__cause__, ExceptionGroup)
        assert not stream.closed
        with pytest.raises(RuntimeError, match="日志交付曾中断"):
            store.open("backend", 12345)
    finally:
        ManagedProcess(retained.process, retained.log_file).close()
    with store.open("backend", 12345):
        pass


@pytest.mark.skipif(os.name != "posix", reason="POSIX 控制器崩溃后的存活组验证")
def test_controller_death_keeps_child_log_active(tmp_path: Path) -> None:
    script = f"""
import os,sys
from pathlib import Path
from app.gateway.runtime.process_logs import ProcessLogStore
from app.gateway.runtime.process_lifecycle import _spawn_logged_process
stream=ProcessLogStore(Path({str(tmp_path)!r})).open('backend',12345)
process=_spawn_logged_process([sys.executable,'-c','import time; print(\"child\",flush=True); time.sleep(60)'],log_file=stream)
os._exit(0)
"""
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, check=False, timeout=10)
    assert result.returncode == 0, result.stderr
    store = ProcessLogStore(tmp_path)
    path = store.path_for("backend", 12345)
    record = json.loads(path.with_name(path.name + ".owner.json").read_text())
    adopted = AdoptedManagedProcess(record["pid"])
    try:
        assert adopted.is_alive()
        with pytest.raises(RuntimeError, match="活动写入者"):
            store.open("backend", 12345)
        ownership = store.adopt("backend", 12345, {"pid": record["pid"], "log_sink": record})
        adopted.log_ownership = ownership
    finally:
        adopted.close()
    with store.open("backend", 12345):
        pass
