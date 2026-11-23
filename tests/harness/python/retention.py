"""按测试文件保留整次运行；共享证据跟随最后一个消费者回收。"""
from __future__ import annotations

import json
import shutil
import stat
from pathlib import Path

import portalocker

from app.core.atomic_fs import atomic_write_bytes

OWNER = "tests.harness.python.artifacts"
RECENT_RUNS = 2


def write_receipt(path: Path, value: dict) -> None:
    atomic_write_bytes(path, (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode())


def read_receipt(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("owner") != OWNER or value.get("schema_version") != 1:
        raise ValueError(f"测试运行收据的 owner 或格式版本无效: {path}")
    return value


class RunRetention:
    def __init__(self, project_root: Path, session_path: Path) -> None:
        self.root = project_root
        self.session_path = session_path
        self.lock_path = project_root / "out/tests/harness/python/artifacts/runtime/retention.lock"

    def lock(self) -> portalocker.Lock:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        return portalocker.Lock(self.lock_path, timeout=60)

    def start(self, run_root: Path, started_at: str) -> None:
        with self.lock():
            run_root.mkdir(parents=True, exist_ok=True)
            with (run_root / "run.json").open("x", encoding="utf-8") as stream:
                json.dump({"schema_version": 1, "owner": OWNER, "status": "running",
                           "started_at": started_at, "session": str(self.session_path.relative_to(self.root))}, stream)

    def finish(self, files: dict[Path, dict], session: dict, prune: bool) -> None:
        with self.lock():
            session.update(schema_version=1, owner=OWNER, status="complete" if prune else "incomplete",
                           file_runs=[str(path.relative_to(self.root)) for path in files], runtime_cleanup="running")
            write_receipt(self.session_path, session)
            for path, receipt in files.items():
                receipt.update(schema_version=1, owner=OWNER, session=str(self.session_path.relative_to(self.root)))
                write_receipt(path, receipt)
            if not prune:
                session["runtime_cleanup"] = "retained"
                write_receipt(self.session_path, session)
                return
            sessions = {self.session_path}
            for path in files:
                sessions.update(self.prune_file(path.parent.parent.parent))
            for path in sessions:
                self.collect_session(path)

    def prune_file(self, output_root: Path) -> set[Path]:
        runs = []
        # 只读各运行的小收据，不遍历工作区、日志、SQLite 或事件正文。
        for directory in (output_root / "runs").iterdir():
            path = directory / "run.json"
            if directory.is_dir() and path.is_file():
                data = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data, dict) and data.get("owner") == OWNER:
                    runs.append((path, read_receipt(path)))
        completed = sorted(((path, data) for path, data in runs if data["status"] in {"passed", "failed", "skipped"}),
                           key=lambda item: item[1]["finished_at"], reverse=True)
        latest_failed = next((path for path, data in completed if data["status"] == "failed"), None)
        keep = {path for path, _ in completed[:RECENT_RUNS]}
        if latest_failed is not None:
            keep.add(latest_failed)
        marker = output_root / "runtime/workspace-owner.json"
        active = Path(json.loads(marker.read_text(encoding="utf-8"))["run"]) if marker.exists() else None
        sessions = set()
        for path, data in completed:
            session_path = self.root / data["session"]
            sessions.add(session_path)
            session = read_receipt(session_path)
            pinned = (data["pinned"] or (active is not None and active.is_relative_to(path.parent))
                      or session["status"] != "complete")
            if path not in keep and not pinned:
                # 删除中断后保留剩余现场，不把它当成完整旧运行再次回收。
                data["status"] = "pruning"
                write_receipt(path, data)
                session["status"] = "incomplete"
                write_receipt(session_path, session)
                for artifact in data["performance"]:
                    self.remove(output_root / artifact)
                self.remove(path.parent)
                session["status"] = "complete"
                write_receipt(session_path, session)
                continue
            data["retain_runtime"] = bool(pinned or path == latest_failed)
            write_receipt(path, data)
        self.refresh_pointer(output_root, "latest.json", runs, "last_node")
        self.refresh_pointer(output_root, "latest-failed.json", runs, "last_failed_node")
        return sessions

    def refresh_pointer(self, output_root: Path, name: str, runs: list, field: str) -> None:
        pointer = output_root / name
        if pointer.exists() and (output_root / json.loads(pointer.read_text(encoding="utf-8"))["run"]).exists():
            return
        candidates = sorted((data for path, data in runs if path.exists() and data.get(field)),
                            key=lambda data: data.get("finished_at", data["started_at"]), reverse=True)
        if candidates:
            write_receipt(pointer, {"schema_version": 1, "run": candidates[0][field]})
        else:
            pointer.unlink(missing_ok=True)

    def collect_session(self, path: Path) -> None:
        if not path.exists():
            return
        session = read_receipt(path)
        if session["status"] != "complete":
            return
        # 清理失败或进程退出时，这份共享现场不再参与自动回收。
        session["status"] = "incomplete"
        write_receipt(path, session)
        live = {name: read_receipt(self.root / name) for name in session["file_runs"] if (self.root / name).exists()}
        if any(data["status"] not in {"passed", "failed", "skipped"} for data in live.values()):
            return
        required = {name for name, data in live.items() if data.get("retain_runtime", True)}
        resources = session["runtime_resources"]
        kept = {self.root / item["path"] for item in resources if
                required.intersection(item["files"]) or (item["shared"] and required)}
        for item in sorted(resources, key=lambda item: len(Path(item["path"]).parts), reverse=True):
            runtime = self.root / item["path"]
            if not any(runtime.is_relative_to(keep) or keep.is_relative_to(runtime) for keep in kept):
                self.remove(runtime)
        session["nodes"] = []
        for name, data in live.items():
            output_root = (self.root / name).parent.parent.parent
            for node in data["nodes"]:
                node_path = output_root / node
                value = read_receipt(node_path)
                value["runtime"] = "retained" if any(keep.is_relative_to(output_root) for keep in kept) else "removed"
                write_receipt(node_path, value)
                session["nodes"].append(str(node_path.relative_to(self.root)))
        session["file_runs"] = list(live)
        session["runtime_cleanup"] = "partial" if kept else "removed"
        if live:
            session["status"] = "complete"
            write_receipt(path, session)
        else:
            for artifact in session["shared_artifacts"]:
                self.remove(self.root / artifact)
            self.remove(path.parent)
            if path.parent.parent.exists() and not any(path.parent.parent.iterdir()):
                path.parent.parent.rmdir()

    def remove(self, path: Path) -> None:
        if not path.is_relative_to(self.root / "out/tests") or path == self.root / "out/tests":
            raise ValueError(f"拒绝清理测试产物范围外的路径: {path}")
        if self.lock_path.is_relative_to(path):
            raise ValueError(f"拒绝清理测试保留锁所在的目录: {path}")
        if path.exists():
            def remove_readonly(operation, entry, failure):
                if not isinstance(failure[1], PermissionError):
                    raise failure[1]
                # 归档附件目录保留只读属性，删除内容前需恢复父目录的写权限。
                parent = Path(entry).parent
                if not parent.is_relative_to(path):
                    raise failure[1]
                parent.chmod(parent.stat().st_mode | stat.S_IWUSR)
                operation(entry)

            shutil.rmtree(path, onerror=remove_readonly)
