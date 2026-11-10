"""不同运行时复用同一日志文件身份合同。"""

from __future__ import annotations

from pathlib import Path

from app.runtime.log_file_identity import (
    runtime_log_file_identity,
    runtime_log_path_identity,
)


def test_descriptor_and_path_share_platform_file_identity(tmp_path: Path) -> None:
    path = tmp_path / "runtime.log"
    path.write_text("runtime log", encoding="utf-8")

    with path.open("rb") as stream:
        descriptor_identity = runtime_log_file_identity(stream.fileno())

    assert runtime_log_path_identity(path) == descriptor_identity
