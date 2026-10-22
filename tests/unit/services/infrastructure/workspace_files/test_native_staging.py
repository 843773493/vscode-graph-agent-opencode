"""真实原生 I/O 验证长暂存路径、重开证明、独占发布与清理。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.core.atomic_fs import native_io_path
from app.services.infrastructure.workspace_files.filesystem import (
    FileEffectUnverifiedError,
    captured_source,
    cleanup_steps,
    content_manifest,
    publish_step,
    stage_step,
    verify_published,
)


@pytest.fixture
def long_parent(tmp_path: Path) -> Path:
    parent = tmp_path
    while len(str(parent)) < 270:
        parent /= "native-" + "x" * 30
    native_io_path(parent).mkdir(parents=True)
    return parent


@pytest.mark.parametrize("kind", ["file", "directory"])
def test_long_staging_reopens_proof_publishes_without_overwrite_and_cleans(long_parent: Path, tmp_path: Path, kind: str) -> None:
    source = long_parent / "source"
    if kind == "directory":
        native_io_path(source / "子目录").mkdir(parents=True)
        native_io_path(source / "子目录/正文.txt").write_bytes("原始字节 Ω\n".encode())
    else:
        native_io_path(source).write_bytes("原始字节 Ω\n".encode())
    observed: list[Path] = []
    original = captured_source(source, validate=observed.append)
    assert observed[0] == source  # 扩展路径不污染逻辑归属检查。
    stage = long_parent / ".boxteam-file-op_0123456789abcdef0123456789abcdef-0"
    target = long_parent / "published"
    _, proof = stage_step(stage_root=stage, operation_id="original", request_hash="hash", source=source, kind=kind)
    proof_file = tmp_path / "proof.json"
    proof_file.write_text(json.dumps(proof), encoding="utf-8")
    del proof
    restored = json.loads(proof_file.read_text(encoding="utf-8"))
    assert verify_published(stage / "entry", restored)
    native_io_path(target).write_bytes(b"external writer")
    with pytest.raises(FileExistsError):
        publish_step(stage / "entry", target, kind)
    assert native_io_path(target).read_bytes() == b"external writer"
    assert verify_published(stage / "entry", restored)
    native_io_path(target).unlink()
    publish_step(stage / "entry", target, kind)
    assert verify_published(target, restored)
    assert content_manifest(target) == original["manifest"]
    assert not native_io_path(stage / "entry").exists()
    cleanup_steps([{"stage_root": str(stage)}], operation_id="original", request_hash="hash")
    assert not native_io_path(stage).exists()
    assert verify_published(target, restored)


def test_long_staging_rejects_wrong_owner(long_parent: Path) -> None:
    stage = long_parent / ".boxteam-file-original-0"
    stage_step(stage_root=stage, operation_id="original", request_hash="hash", source=None, kind="file")
    with pytest.raises(FileEffectUnverifiedError, match="owner"):
        stage_step(stage_root=stage, operation_id="another", request_hash="hash", source=None, kind="file")
    with pytest.raises(RuntimeError, match="owner"):
        cleanup_steps([{"stage_root": str(stage)}], operation_id="another", request_hash="hash")
    assert native_io_path(stage / "entry").is_file()
