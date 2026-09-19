"""OpenSpec 3.2/3.3 observed source 链路的定向合同测试。

覆盖 StableSourceReader 读取协议(允许根/no-follow/普通文件/双读/上限/
严格 UTF-8/原始 byte hash)、失败保旧语义、版本 token 源、observed/
pending/committed 状态合并,以及「CSM/middleware/skill_load 不得直接调用
reader」的静态隔离审计。
"""

from __future__ import annotations

import ast
import hashlib
from pathlib import Path

import pytest

from app.services.infrastructure.resource_platform.sources.observed_source import (
    MAX_STABLE_READ_BYTES,
    ObservedSourceDescriptor,
    ObservedSourceHandle,
    SourceReconciler,
    StableSourceReader,
    StableSourceReadError,
)

PROJECT_ROOT = Path.cwd()


def _file_handle(path: Path, root: Path, source_id: str = "src-1") -> ObservedSourceHandle:
    return ObservedSourceHandle(
        descriptor=ObservedSourceDescriptor(
            source_id=source_id,
            source_kind="file",
            display_uri="boxteam://workspace/skill/demo",
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
    calls = {"count": 0}

    def fake_pread(fd: int, size: int, offset: int) -> bytes:
        if offset != 0:
            return b""
        data = payload[calls["count"] % 2]
        calls["count"] += 1
        return data

    monkeypatch.setattr("os.pread", fake_pread)
    unavailable = reconciler.reconcile("src-1")
    assert unavailable.available is False
    assert unavailable.error_code == "unstable_read"
    # 失败保旧:revision/content 保持上一份 valid 事实,并显式保留引用。
    assert unavailable.revision == first.revision
    assert unavailable.content == first.content
    assert unavailable.retained_revision == first.revision
    monkeypatch.undo()
    source.write_text("v4\n", encoding="utf-8")
    recovered = reconciler.reconcile("src-1")
    assert recovered.available is True
    assert recovered.revision == "sha256:" + hashlib.sha256(b"v4\n").hexdigest()
    assert recovered.retained_revision is None


def test_token_source_uses_version_token_without_file_double_read() -> None:
    state = {"token": "gateway-gen-1:rev-a", "content": "gateway snapshot v1"}
    handle = ObservedSourceHandle(
        descriptor=ObservedSourceDescriptor(
            source_id="gw-agents",
            source_kind="gateway_snapshot",
            display_uri="boxteam://gateway/agents",
            entry_identity="entry-gw-agents",
        ),
        version_token_reader=lambda: (state["token"], state["content"]),
    )
    reconciler = SourceReconciler(handles={"gw-agents": handle})
    first = reconciler.reconcile("gw-agents")
    assert first.revision == "gateway-gen-1:rev-a"
    assert first.revision_kind == "version_token"
    assert first.byte_length == len(state["content"].encode("utf-8"))
    state["token"] = "gateway-gen-1:rev-b"
    state["content"] = "gateway snapshot v2"
    second = reconciler.reconcile("gw-agents")
    assert second.revision == "gateway-gen-1:rev-b"
    # 无变化的重复 reconcile 不产生新事实。
    again = reconciler.reconcile("gw-agents")
    assert again.revision == second.revision


def test_token_source_error_retains_previous_revision(tmp_path: Path) -> None:
    state = {"token": "mem-rev-1", "content": "memory v1", "fail": False}
    handle = ObservedSourceHandle(
        descriptor=ObservedSourceDescriptor(
            source_id="mem-team",
            source_kind="memory_state",
            display_uri="boxteam://memory/team",
            entry_identity="entry-mem-team",
        ),
        version_token_reader=lambda: (
            (_ for _ in ()).throw(StableSourceReadError("missing", "snapshot disconnected"))
            if state["fail"]
            else (state["token"], state["content"])
        ),
    )
    reconciler = SourceReconciler(handles={"mem-team": handle})
    first = reconciler.reconcile("mem-team")
    assert first.available is True
    state["fail"] = True
    unavailable = reconciler.reconcile("mem-team")
    assert unavailable.available is False
    assert unavailable.error_code == "missing"
    assert unavailable.retained_revision == "mem-rev-1"
    assert unavailable.content == "memory v1"


def test_revision_states_merge_uncommitted_changes(tmp_path: Path) -> None:
    source = tmp_path / "SKILL.md"
    source.write_text("r1\n", encoding="utf-8")
    reconciler = SourceReconciler(handles={"src-1": _file_handle(source, tmp_path)})
    first = reconciler.reconcile("src-1")
    reconciler.mark_committed("src-1", first.revision)
    assert reconciler.revision_states("src-1") == (first.revision, None, first.revision)

    source.write_text("r2\n", encoding="utf-8")
    reconciler.reconcile("src-1")
    observed, pending, committed = reconciler.revision_states("src-1")
    assert pending == observed != committed

    # 多个未提交变化合并:committed 基准不变,pending 只指向最新 observed。
    source.write_text("r3\n", encoding="utf-8")
    reconciler.reconcile("src-1")
    observed_latest, pending_latest, committed_same = reconciler.revision_states("src-1")
    assert pending_latest == observed_latest
    assert committed_same == committed
    assert observed_latest == "sha256:" + hashlib.sha256(b"r3\n").hexdigest()

    reconciler.mark_committed("src-1", observed_latest)
    assert reconciler.revision_states("src-1")[1] is None


def test_mark_committed_rejects_unobserved_revision(tmp_path: Path) -> None:
    source = tmp_path / "SKILL.md"
    source.write_text("r1\n", encoding="utf-8")
    reconciler = SourceReconciler(handles={"src-1": _file_handle(source, tmp_path)})
    reconciler.reconcile("src-1")
    with pytest.raises(ValueError, match="已观察事实"):
        reconciler.mark_committed("src-1", "sha256:never-observed")


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
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.id in forbidden_names:
                violations.append(f"{rel}:{node.lineno}:{node.id}")
            if isinstance(node, ast.Attribute) and node.attr in forbidden_names:
                violations.append(f"{rel}:{node.lineno}:{node.attr}")
    assert violations == [], ("reader 被域外直接引用:", violations)
