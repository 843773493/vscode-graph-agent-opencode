"""OpenSpec 3.2 observed source 链路的定向合同测试。

覆盖 StableSourceReader 读取协议(允许根/no-follow/普通文件/双读/上限/
严格 UTF-8/原始 byte hash)、失败保旧语义,以及
「CSM/middleware/skill_load 不得直接调用 reader」的静态隔离审计。
"""

from __future__ import annotations

import ast
import errno
import hashlib
import json
from dataclasses import asdict
from pathlib import Path

import pytest

from app.services.infrastructure.resource_platform.sources.observed_source import (
    MAX_STABLE_READ_BYTES,
    ObservedSourceDescriptor,
    ObservedSourceHandle,
    ObservedSourceRevision,
    SourceReconciler,
    StableSourceReader,
    StableSourceReadError,
)

PROJECT_ROOT = Path.cwd()


def _file_handle(
    path: Path, root: Path, source_id: str = "src-1"
) -> ObservedSourceHandle:
    return ObservedSourceHandle(
        descriptor=ObservedSourceDescriptor(
            source_id=source_id,
            source_kind="file",
            display_uri="boxteam://workspace/test/resources/skills/demo/SKILL.md",
            entry_identity="entry-demo",
        ),
        file_path=str(path),
        allowed_root=str(root),
    )


def test_stable_read_publishes_full_raw_byte_hash(tmp_path: Path) -> None:
    source = tmp_path / "SKILL.md"
    raw = "# Demo\n\u4f60\u597d \U0001f30d\n".encode("utf-8")
    source.write_bytes(raw)
    revision = StableSourceReader().read(_file_handle(source, tmp_path))
    assert revision.revision == "sha256:" + hashlib.sha256(raw).hexdigest()
    assert revision.revision_kind == "file_byte_hash"
    assert revision.byte_length == len(raw)
    assert revision.content == raw.decode("utf-8")
    assert revision.available is True


def test_handle_rejects_path_outside_allowed_root(tmp_path: Path) -> None:
    outside = tmp_path / "outside" / "SKILL.md"
    outside.parent.mkdir()
    outside.write_text("x", encoding="utf-8")
    with pytest.raises(StableSourceReadError) as info:
        _file_handle(outside, tmp_path / "skills")
    assert info.value.reason_code == "outside_allowed_root"
    assert "boxteam://workspace/test/resources/skills/demo/SKILL.md" in str(info.value)
    assert str(tmp_path) not in str(info.value)


def test_reader_rejects_symlink_without_following(tmp_path: Path) -> None:
    target = tmp_path / "real.md"
    target.write_text("secret\n", encoding="utf-8")
    link = tmp_path / "SKILL.md"
    link.symlink_to(target)
    with pytest.raises(StableSourceReadError) as info:
        StableSourceReader().read(_file_handle(link, tmp_path))
    assert info.value.reason_code == "symlink_rejected"


def test_reader_rejects_non_regular_file(tmp_path: Path) -> None:
    directory = tmp_path / "not-a-file"
    directory.mkdir()
    with pytest.raises(StableSourceReadError) as info:
        StableSourceReader().read(_file_handle(directory, tmp_path))
    assert info.value.reason_code == "not_regular_file"


def test_reader_enforces_fixed_byte_limit(tmp_path: Path) -> None:
    source = tmp_path / "big.md"
    source.write_bytes(b"a" * (MAX_STABLE_READ_BYTES + 1))
    with pytest.raises(StableSourceReadError) as info:
        StableSourceReader().read(_file_handle(source, tmp_path))
    assert info.value.reason_code == "oversize"


def test_reader_rejects_invalid_utf8(tmp_path: Path) -> None:
    source = tmp_path / "SKILL.md"
    source.write_bytes(b"\xff\xfe\x00")
    with pytest.raises(StableSourceReadError) as info:
        StableSourceReader().read(_file_handle(source, tmp_path))
    assert info.value.reason_code == "invalid_utf8"


def test_reader_reports_missing_file(tmp_path: Path) -> None:
    with pytest.raises(StableSourceReadError) as info:
        StableSourceReader().read(_file_handle(tmp_path / "gone.md", tmp_path))
    assert info.value.reason_code == "missing"


@pytest.mark.parametrize(
    "failure",
    ["missing", "invalid_utf8", "symlink_rejected", "not_regular_file", "oversize"],
)
def test_failed_observation_references_uri_and_retains_previous_revision(
    tmp_path: Path,
    failure: str,
) -> None:
    source = tmp_path / "SKILL.md"
    source.write_text("accepted\n", encoding="utf-8")
    handle = _file_handle(source, tmp_path)
    reconciler = SourceReconciler(handles={"src-1": handle})
    accepted = reconciler.reconcile("src-1")
    source.unlink()
    if failure == "invalid_utf8":
        source.write_bytes(b"\xff")
    elif failure == "symlink_rejected":
        target = tmp_path / "private-target.md"
        target.write_text("not accepted\n", encoding="utf-8")
        source.symlink_to(target)
    elif failure == "not_regular_file":
        source.mkdir()
    elif failure == "oversize":
        source.write_bytes(b"x" * (MAX_STABLE_READ_BYTES + 1))

    failed = reconciler.reconcile("src-1")
    assert not failed.available
    assert failed.error_code == failure
    assert failed.revision == failed.retained_revision == accepted.revision
    assert failed.content == accepted.content
    assert handle.descriptor.display_uri in failed.error
    assert str(tmp_path) not in json.dumps(asdict(failed), ensure_ascii=False)


@pytest.mark.parametrize("stage", ["open", "read"])
def test_source_io_failure_preserves_reason_without_publishing_locator(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
) -> None:
    source = tmp_path / "SKILL.md"
    source.write_text("accepted\n", encoding="utf-8")
    handle = _file_handle(source, tmp_path)
    reconciler = SourceReconciler(handles={"src-1": handle})
    accepted = reconciler.reconcile("src-1")

    def fail_io(*_args):
        raise PermissionError(errno.EACCES, "private OS diagnostic", str(source))

    with monkeypatch.context() as patch:
        patch.setattr(f"os.{stage}", fail_io)
        failed = reconciler.reconcile("src-1")
    assert not failed.available and failed.error_code == "read_failed"
    assert failed.revision == failed.retained_revision == accepted.revision
    assert failed.content == accepted.content
    assert "PermissionError" in failed.error and f"errno={errno.EACCES}" in failed.error
    assert handle.descriptor.display_uri in failed.error
    assert str(tmp_path) not in json.dumps(asdict(failed), ensure_ascii=False)
    assert reconciler.reconcile("src-1").available


def test_open_symlink_race_keeps_symlink_reason(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "SKILL.md"
    source.write_text("accepted\n", encoding="utf-8")
    handle = _file_handle(source, tmp_path)

    def fail_open(*_args):
        raise OSError(errno.ELOOP, "private symlink diagnostic", str(source))

    monkeypatch.setattr("os.open", fail_open)
    with pytest.raises(StableSourceReadError) as caught:
        StableSourceReader().read(handle)
    assert caught.value.reason_code == "symlink_rejected"
    assert handle.descriptor.display_uri in str(caught.value)
    assert str(tmp_path) not in str(caught.value)


def test_unstable_read_exhausts_attempts_and_reconciler_retains_previous(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "SKILL.md"
    source.write_text("v1\n", encoding="utf-8")
    reconciler = SourceReconciler(handles={"src-1": _file_handle(source, tmp_path)})
    first = reconciler.reconcile("src-1")
    assert first.available is True

    payload = [b"v2\n", b"v3\n"]
    read_state = {"count": 0, "at_eof": False}

    def fake_read(_fd: int, _size: int) -> bytes:
        if read_state["at_eof"]:
            read_state["at_eof"] = False
            return b""
        data = payload[read_state["count"] % len(payload)]
        read_state["count"] += 1
        read_state["at_eof"] = True
        return data

    monkeypatch.setattr("os.read", fake_read)
    unavailable = reconciler.reconcile("src-1")
    assert unavailable.available is False
    assert unavailable.error_code == "unstable_read"
    assert (
        "boxteam://workspace/test/resources/skills/demo/SKILL.md" in unavailable.error
    )
    assert str(tmp_path) not in unavailable.error
    # 失败保旧:revision/content 保持上一份 valid 事实,并显式保留引用。
    assert unavailable.revision == first.revision
    assert unavailable.revision_kind == "file_byte_hash"
    assert unavailable.content == first.content
    assert unavailable.retained_revision == first.revision
    repeated = reconciler.reconcile("src-1")
    assert not repeated.available
    assert repeated.retained_revision == first.revision
    assert repeated.content == first.content
    monkeypatch.undo()
    source.write_text("v4\n", encoding="utf-8")
    recovered = reconciler.reconcile("src-1")
    assert recovered.available is True
    assert recovered.revision == "sha256:" + hashlib.sha256(b"v4\n").hexdigest()
    assert recovered.retained_revision is None


def test_reclaimed_kinds_and_reason_codes_are_rejected() -> None:
    """闭集校验必须显式拒绝 gateway_snapshot/version_token 等已回收取值。"""
    with pytest.raises(ValueError, match="未知 ObservedSourceDescriptor.source_kind"):
        ObservedSourceDescriptor(
            source_id="gw-agents",
            source_kind="gateway_snapshot",
            display_uri="boxteam://gateway/gateway_test/resources/agent-spec/root/AGENTS.md",
            entry_identity="entry-gw-agents",
        )
    with pytest.raises(ValueError, match="未知 ObservedSourceRevision.revision_kind"):
        ObservedSourceRevision(
            source_id="src-1",
            revision="token",
            revision_kind="version_token",
            content="",
            byte_length=0,
        )
    with pytest.raises(ValueError, match="未知 StableSourceReadError reason_code"):
        StableSourceReadError("invalid_version_token", "已回收的 reason code")


def test_reconcile_unknown_source_and_bounded_reconcile_all(tmp_path: Path) -> None:
    source = tmp_path / "SKILL.md"
    source.write_text("v\n", encoding="utf-8")
    reconciler = SourceReconciler(handles={"src-1": _file_handle(source, tmp_path)})
    with pytest.raises(KeyError):
        reconciler.reconcile("src-unknown")
    revisions = reconciler.reconcile_all()
    assert [item.source_id for item in revisions] == ["src-1"]
    with pytest.raises(KeyError):
        reconciler.observed_revision("src-unknown")


def test_notify_fires_only_on_observed_change(tmp_path: Path) -> None:
    source = tmp_path / "SKILL.md"
    source.write_text("v1\n", encoding="utf-8")
    notified: list[str] = []
    reconciler = SourceReconciler(
        handles={"src-1": _file_handle(source, tmp_path)},
        notify=lambda revision: notified.append(revision.revision),
    )
    reconciler.reconcile("src-1")
    reconciler.reconcile("src-1")
    assert len(notified) == 1
    source.write_text("v2\n", encoding="utf-8")
    reconciler.reconcile("src-1")
    assert len(notified) == 2


def test_reader_and_reconciler_are_not_imported_outside_sources() -> None:
    """CSM/middleware/skill_load/model-call preparation 不得直接调用 reader。"""
    allowed = {
        "app/services/infrastructure/resource_platform/sources/observed_source.py",
        "app/services/infrastructure/resource_platform/sources/workspace_file_resources.py",
        "app/services/infrastructure/resource_platform/registry/context_source_reactor.py",
    }
    forbidden_names = {
        "StableSourceReader",
        "SourceReconciler",
        "ObservedSourceHandle",
        "StableSourceReadError",
    }
    violations: list[str] = []
    for path in sorted((PROJECT_ROOT / "app").rglob("*.py")):
        rel = path.relative_to(PROJECT_ROOT).as_posix()
        if rel in allowed:
            continue
        source = path.read_text(encoding="utf-8")
        if not any(name in source for name in forbidden_names):
            continue
        tree = ast.parse(source, filename=rel)
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.id in forbidden_names:
                violations.append(f"{rel}:{node.lineno}:{node.id}")
            if isinstance(node, ast.Attribute) and node.attr in forbidden_names:
                violations.append(f"{rel}:{node.lineno}:{node.attr}")
    assert violations == [], ("reader 被域外直接引用:", violations)
