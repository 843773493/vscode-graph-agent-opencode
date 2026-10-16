"""pytest 进程拥有的时钟、phase barrier 和端口租约；不写业务数据。"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import secrets
import select
import socket
import socketserver
import sqlite3
import threading
import time
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from uuid import UUID

from tests.support.processes import ensure_local_port_available


def deterministic_uuid7(namespace: str, ordinal: int, epoch_ms: int) -> str:
    """namespace/ordinal 冻结随机域；版本与 variant 保持 canonical UUIDv7。"""
    random = int.from_bytes(hashlib.sha256(f"{namespace}\0{ordinal}".encode()).digest()[:10], "big")
    value = epoch_ms << 80 | 7 << 76 | (random >> 62 & 0xFFF) << 64 | 2 << 62 | (random & ((1 << 62) - 1))
    return UUID(int=value).hex


def _process_start(pid: int) -> str:
    """Linux 进程 start-time；PID 重用不能继承旧租约。"""
    return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]


@dataclass(slots=True)
class E2EProcessLease:
    path: Path
    ports: tuple[int, ...]
    token: str = field(default_factory=lambda: secrets.token_hex(16))

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        record = {"pid": os.getpid(), "start_time": _process_start(os.getpid()), "token": self.token, "ports": self.ports}
        if self.path.exists():
            previous = json.loads(self.path.read_text())
            previous_pid = previous["pid"]
            process_file = Path(f"/proc/{previous_pid}/stat")
            if process_file.exists() and _process_start(previous_pid) == previous["start_time"]:
                raise RuntimeError(f"E2E 独占租约仍有效，禁止抢占: {previous}")
            self.path.unlink()
        for port in self.ports:
            if 8010 <= port <= 8016:
                raise ValueError(f"正式 E2E 不得租用开发端口: {port}")
            ensure_local_port_available(port, service_name="Section8 owner")
        with self.path.open("x", encoding="utf-8") as stream:
            json.dump(record, stream)

    def release(self) -> None:
        for port in self.ports:
            ensure_local_port_available(port, service_name="Section8 teardown")
        record = json.loads(self.path.read_text())
        if (record["pid"], record["start_time"], record["token"]) != (os.getpid(), _process_start(os.getpid()), self.token):
            raise RuntimeError("E2EProcessLease owner 变化，拒绝删除其它 owner 的租约")
        self.path.unlink()


@dataclass(slots=True)
class _Barrier:
    acknowledged: threading.Event = field(default_factory=threading.Event)
    released: threading.Event = field(default_factory=threading.Event)
    event: dict[str, object] | None = None


@dataclass(slots=True)
class _PhaseMatch:
    phase: str
    details: dict[str, object]
    barrier: _Barrier = field(default_factory=_Barrier)


class _ControlServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class E2ETestControlHarness:
    """所有控制状态位于 pytest 外进程；backend 只读取时钟/观察 phase。"""

    def __init__(self, root: Path) -> None:
        self.root = root
        self._condition = threading.Condition()
        self._barriers: dict[tuple[str, str], _Barrier] = {}
        self._phase_matches: dict[str, _PhaseMatch] = {}
        self._counters: dict[str, int] = {}
        self._residency_seconds = 0.0
        self._wait_ns = 0
        self._wait_utc = datetime.now(UTC)
        self._id_epoch_ms = int(self._wait_utc.timestamp() * 1000)
        self._origin = "section8-" + secrets.token_hex(12)
        self._token = secrets.token_hex(24)
        self._stopped = False
        self.events: list[dict[str, object]] = []
        self._cassette_path: Path | None = None
        self._process_controls: dict[str, Callable[[str], object]] = {}
        self._trace_roots: dict[str, Path] = {}
        harness = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self) -> None:
                line = self.rfile.readline()
                if not line:
                    return
                request = json.loads(line)
                if request.get("token") != harness._token:
                    raise PermissionError("test control token 无效")
                try:
                    def disconnected() -> bool:
                        readable, _, _ = select.select([self.request], [], [], 0)
                        return bool(readable) and self.request.recv(1, socket.MSG_PEEK) == b""
                    value = harness._dispatch(request, disconnected=disconnected)
                    result = {"value": value}
                except Exception as error:  # noqa: BLE001 —— 端口错误完整返回给调用方。
                    result = {"error": f"{type(error).__name__}: {error}"}
                self.wfile.write(json.dumps(result, ensure_ascii=False).encode() + b"\n")

        self._server = _ControlServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, name="section8-test-control", daemon=True)

    @property
    def environment(self) -> dict[str, str]:
        host, port = self._server.server_address
        return {"BOXTEAM_E2E_TEST_CONTROL": json.dumps({"host": host, "port": port, "token": self._token})}

    def start(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self._thread.start()

    def close(self) -> None:
        with self._condition:
            self._stopped = True
            for barrier in self._barriers.values():
                barrier.released.set()
            for match in self._phase_matches.values():
                match.barrier.released.set()
            self._condition.notify_all()
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=10)
        if self._thread.is_alive():
            raise RuntimeError("E2ETestControlHarness 未停止")
        (self.root / "events.json").write_text(json.dumps(self.events, ensure_ascii=False, indent=2) + "\n")

    def arm(self, operation_id: str, phase: str) -> None:
        key = (operation_id, phase)
        with self._condition:
            if key in self._barriers and not self._barriers[key].released.is_set():
                raise RuntimeError(f"phase barrier 已存在: {key}")
            self._barriers[key] = _Barrier()

    def acknowledged(self, operation_id: str, phase: str, timeout: float = 30) -> None:
        if not self._barriers[operation_id, phase].acknowledged.wait(timeout):
            raise TimeoutError(f"业务 owner 未到达 phase: {operation_id}/{phase}")

    def release(self, operation_id: str, phase: str) -> None:
        self._barriers[operation_id, phase].released.set()

    def arm_match(self, barrier_id: str, phase: str, details: dict[str, object]) -> None:
        """浏览器随机 operation ID 在精确 owner phase 首次到达时绑定，不改变业务 identity。"""
        if not barrier_id or not phase or not details:
            raise ValueError("phase match 必须预注册非空 barrier、phase 和精确 owner details")
        with self._condition:
            if barrier_id in self._phase_matches:
                raise RuntimeError(f"phase match identity 已使用: {barrier_id}")
            self._phase_matches[barrier_id] = _PhaseMatch(phase, dict(details))

    def acknowledged_match(self, barrier_id: str) -> dict[str, object]:
        match = self._phase_matches[barrier_id]
        if not match.barrier.acknowledged.wait(30):
            raise TimeoutError(f"精确 owner 未到达 phase match: {barrier_id}")
        if match.barrier.event is None:
            raise RuntimeError(f"phase match ack 缺少实际 operation identity: {barrier_id}")
        return dict(match.barrier.event)

    def advance_residency(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("residency clock 不能回拨")
        with self._condition:
            self._residency_seconds += seconds
            self._condition.notify_all()

    def advance_wait(self, seconds: float, *, change_origin: bool = False) -> None:
        if seconds < 0:
            raise ValueError("wait clock 不能回拨")
        with self._condition:
            self._wait_ns += int(seconds * 1_000_000_000)
            self._wait_utc += timedelta(seconds=seconds)
            if change_origin:
                self._origin = "section8-" + secrets.token_hex(12)
                self._wait_ns = 0
            self._condition.notify_all()

    def set_identifier_date(self, moment: datetime) -> None:
        if moment.tzinfo is None:
            raise ValueError("identifier 时刻必须带时区")
        self._id_epoch_ms = int(moment.timestamp() * 1000)

    def bind_cassette(self, path: Path) -> None:
        self._cassette_path = path

    def plan_model_calls(self, session_id: str, thread_id: str, interactions: list[dict[str, object]]) -> list[str]:
        """真实 API identity + factory 的未来 ID；不按请求先后分派响应。"""
        if self._cassette_path is None:
            raise RuntimeError("精确回放 asset 尚未绑定")
        namespace = f"model_call/{session_id}/{thread_id}"
        with self._condition:
            cassette = json.loads(self._cassette_path.read_text())
            ordinal = self._counters.get(namespace, 0)
            ids = []
            for offset, interaction in enumerate(interactions):
                call_id = str(UUID(hex=deterministic_uuid7(namespace, ordinal + offset, self._id_epoch_ms)))
                entry = copy.deepcopy(interaction)
                entry.pop("replay", None)
                entry["identity"] = {"session_id": session_id, "thread_id": thread_id, "model_call_id": call_id}
                if any(existing.get("identity") == entry["identity"] for existing in cassette["interactions"]):
                    raise RuntimeError(f"model call 已有精确计划，禁止覆盖: {call_id}")
                cassette["interactions"].append(entry)
                ids.append(call_id)
            temporary = self._cassette_path.with_suffix(".writing")
            temporary.write_text(json.dumps(cassette, ensure_ascii=False, indent=2) + "\n")
            temporary.replace(self._cassette_path)
            return ids

    def next_uuid(self, namespace: str, offset: int = 0) -> str:
        with self._condition:
            return deterministic_uuid7(namespace, self._counters.get(namespace, 0) + offset, self._id_epoch_ms)

    def bind_process_control(self, name: str, callback: Callable[[str], object]) -> None:
        """只有 pytest 注册本次拥有的进程；回调在控制锁外执行，允许 startup 请求 harness。"""
        if name in self._process_controls:
            raise RuntimeError(f"外置进程 control 已绑定: {name}")
        self._process_controls[name] = callback

    def unbind_process_control(self, name: str) -> None:
        del self._process_controls[name]

    def bind_trace_root(self, name: str, root: Path) -> None:
        """只读 trace 的根由 pytest 正式 fixture 绑定；调用方不能指定其它工作区。"""
        self._trace_roots[name] = root.resolve(strict=True)

    def unbind_trace_root(self, name: str) -> None:
        del self._trace_roots[name]

    def _dispatch(self, request: dict[str, object], *, disconnected: Callable[[], bool] | None = None) -> object:
        action = request["action"]
        if action == "arm":
            self.arm(cast(str, request["operation_id"]), cast(str, request["phase"]))
            return None
        if action == "arm_match":
            self.arm_match(cast(str, request["barrier_id"]), cast(str, request["phase"]), cast(dict[str, object], request["details"]))
            return None
        if action == "acknowledged_match":
            return self.acknowledged_match(cast(str, request["barrier_id"]))
        if action == "release_match":
            self._phase_matches[cast(str, request["barrier_id"])].barrier.released.set()
            return None
        if action == "acknowledged":
            self.acknowledged(cast(str, request["operation_id"]), cast(str, request["phase"]))
            return None
        if action == "release":
            self.release(cast(str, request["operation_id"]), cast(str, request["phase"]))
            return None
        if action == "plan_model":
            return self.plan_model_calls(cast(str, request["session_id"]), cast(str, request["thread_id"]), cast(list[dict[str, object]], request["interactions"]))
        if action == "next_uuid":
            return self.next_uuid(cast(str, request["namespace"]), cast(int, request.get("offset", 0)))
        if action == "process":
            return self._process_controls[cast(str, request["name"])](cast(str, request["operation"]))
        if action == "events":
            with self._condition:
                return list(self.events)
        if action == "read_directory":
            root = self._trace_roots[cast(str, request["name"])]
            requested = root / cast(str, request["relative_path"])
            parent = requested.parent.resolve(strict=True)
            if not parent.is_relative_to(root):
                raise ValueError("只读目录路径不属于当前 pytest fixture")
            # 只允许已存在父目录下的叶目录未物化；断链、文件或损坏路径仍报错。
            if requested.name not in {entry.name for entry in parent.iterdir()}:
                return None
            path = requested.resolve(strict=True)
            if not path.is_relative_to(root) or not path.is_dir():
                raise ValueError("只读目录路径不属于当前 pytest fixture 的目录")
            return sorted(entry.name for entry in path.iterdir())
        if action == "read_sql":
            root = self._trace_roots[cast(str, request["name"])]
            path = (root / cast(str, request["relative_path"])).resolve(strict=True)
            if not path.is_relative_to(root) or not path.is_file():
                raise ValueError("只读 trace 路径不属于当前 pytest fixture")
            query = cast(str, request["query"])
            if not query.lstrip().upper().startswith(("SELECT ", "WITH ")):
                raise ValueError("只读 trace 只接受 SELECT")
            with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as connection:
                connection.execute("PRAGMA query_only=ON")
                connection.row_factory = sqlite3.Row
                return [dict(row) for row in connection.execute(query, cast(list[object], request.get("parameters", [])))]
        if action == "advance_residency":
            self.advance_residency(cast(float, request["seconds"]))
            return None
        if action == "advance_wait":
            self.advance_wait(cast(float, request["seconds"]), change_origin=bool(request.get("change_origin", False)))
            return None
        if action == "identifier_date":
            self.set_identifier_date(datetime.fromisoformat(cast(str, request["utc"])))
            return None
        if action == "phase":
            key = (cast(str, request["operation_id"]), cast(str, request["phase"]))
            with self._condition:
                event = {"operation_id": key[0], "phase": key[1], "pid": request["pid"], "at": time.monotonic()}
                if request.get("details") is not None:
                    event["details"] = request["details"]
                self.events.append(event)
                barrier = self._barriers.get(key)
                details = cast(dict[str, object], request.get("details") or {})
                matches = [match for match in self._phase_matches.values()
                           if match.phase == key[1] and not match.barrier.released.is_set()
                           and (match.barrier.event is None or match.barrier.event["operation_id"] == key[0])
                           and all(name in details and details[name] == value for name, value in match.details.items())]
                if len(matches) > 1 or (matches and barrier is not None):
                    raise RuntimeError(f"phase barrier 选择串扰: {key}")
                if matches:
                    barrier = matches[0].barrier
                    barrier.event = dict(event)
                if barrier is not None:
                    barrier.acknowledged.set()
            if barrier is not None and not barrier.released.wait(1200):
                raise TimeoutError(f"pytest 未释放 phase: {key}")
            return None
        with self._condition:
            if action == "identifier":
                namespace = cast(str, request["namespace"])
                ordinal = self._counters.get(namespace, 0)
                self._counters[namespace] = ordinal + 1
                epoch = request.get("epoch_ms")
                return deterministic_uuid7(namespace, ordinal, self._id_epoch_ms if epoch is None else cast(int, epoch))
            if action == "clock":
                return {"residency_seconds": self._residency_seconds, "wait_ns": self._wait_ns, "wait_utc": self._wait_utc.isoformat(), "origin": self._origin}
            if action == "deadline":
                deadline = cast(int, request["deadline_ns"])
                origin = request["origin"]
                while not self._stopped and origin == self._origin and self._wait_ns < deadline:
                    if disconnected is not None and disconnected():
                        return None
                    self._condition.wait(timeout=1)
                return None
        raise ValueError(f"未知外置控制操作: {action!r}")


def control_request(address: dict[str, object], payload: dict[str, object]) -> object:
    with socket.create_connection((cast(str, address["host"]), cast(int, address["port"])), timeout=1200 if payload.get("action") in {"phase", "process"} else 30) as connection:
        connection.sendall(json.dumps({**payload, "token": address["token"], "pid": os.getpid()}).encode() + b"\n")
        with connection.makefile("rb") as stream:
            result = json.loads(stream.readline())
    if "error" in result:
        raise RuntimeError(result["error"])
    return result["value"]
