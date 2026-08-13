from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from hashlib import sha1
from pathlib import Path
from typing import ClassVar, Literal, Self

TestResultStatus = Literal[
    "passed",
    "failed",
    "skipped",
    "UNMET_PREREQUISITE",
]


@dataclass(slots=True)
class TestRunContext:
    __test__: ClassVar[bool] = False

    project_root: Path
    test_file: Path
    output_root: Path
    workspace_root: Path
    artifacts_dir: Path
    boxteam_home: Path
    _cleanups: list[tuple[str, Callable[[], None]]] = field(default_factory=list)
    _closed: bool = False

    @classmethod
    def from_test_file(
        cls,
        test_file: Path,
        *,
        project_root: Path | None = None,
    ) -> TestRunContext:
        root = (project_root or Path.cwd()).resolve()
        resolved_test_file = test_file.resolve()
        tests_root = root / "tests"
        try:
            relative_test_file = resolved_test_file.relative_to(tests_root)
        except ValueError as exc:
            raise ValueError(
                f"测试文件必须位于 {tests_root} 内，实际为 {resolved_test_file}"
            ) from exc
        relative_output = relative_test_file.with_suffix("")
        output_root = root / "out" / "tests" / relative_output
        return cls(
            project_root=root,
            test_file=resolved_test_file,
            output_root=output_root,
            workspace_root=output_root / "workspace",
            artifacts_dir=output_root / "artifacts",
            boxteam_home=output_root / "boxteam-home",
        )

    def prepare(self) -> TestRunContext:
        self.workspace_root.mkdir(parents=True, exist_ok=True)
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
        self.boxteam_home.mkdir(parents=True, exist_ok=True)
        return self

    def runtime_root_for_node(self, node_id: str) -> Path:
        """返回同一测试文件内按 pytest 节点隔离的运行目录。"""
        if not node_id.strip():
            raise ValueError("pytest 节点 ID 不能为空")
        run_identity = (
            os.getenv("BOXTEAM_TEST_RUN_ID")
            or os.getenv("PYTEST_XDIST_TESTRUNUID")
            or f"pid-{os.getpid()}"
        )
        run_key = sha1(run_identity.encode("utf-8")).hexdigest()[:12]
        node_key = sha1(node_id.encode("utf-8")).hexdigest()[:12]
        return self.output_root / "runtime" / f"run-{run_key}" / f"node-{node_key}"

    def boxteam_home_for_node(self, node_id: str) -> Path:
        """返回同一测试文件内按 pytest 节点隔离的全局配置目录。"""
        return self.runtime_root_for_node(node_id) / "boxteam-home"

    def add_cleanup(self, label: str, cleanup: Callable[[], None]) -> None:
        if self._closed:
            raise RuntimeError("测试运行上下文已经关闭，不能再注册清理动作")
        self._cleanups.append((label, cleanup))

    def write_result(
        self,
        status: TestResultStatus,
        details: dict[str, object] | None = None,
    ) -> dict[str, object]:
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
        result: dict[str, object] = {
            "schema_version": 1,
            "test_id": self.test_file.relative_to(
                self.project_root / "tests"
            ).with_suffix("").as_posix(),
            "status": status,
            "recorded_at": datetime.now(UTC).isoformat(),
            "details": details or {},
        }
        (self.artifacts_dir / "result.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return result

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        failures: list[BaseException] = []
        for label, cleanup in reversed(self._cleanups):
            try:
                cleanup()
            except BaseException as exc:  # noqa: BLE001
                failures.append(RuntimeError(f"清理 {label} 失败: {exc}"))
        if failures:
            raise ExceptionGroup("测试资源清理失败", failures)

    def __enter__(self) -> Self:
        return self.prepare()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object,
    ) -> None:
        del exc_type, exc, traceback
        self.close()
