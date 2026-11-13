"""保留当前完整源码与固定上游的 Workbench 独占构建快照。"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import tarfile
from pathlib import Path


def snapshot_workbench_sources(project_root: Path, runtime: Path) -> tuple[Path, dict[str, object]]:
    snapshot = runtime / "source"
    snapshot.mkdir(parents=True)
    archive = runtime / "source.tar"
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=project_root, text=True).strip()
    subprocess.run(["git", "archive", "--format=tar", "-o", str(archive), head],
                   cwd=project_root, check=True)
    with tarfile.open(archive) as files:
        files.extractall(snapshot, filter="data")
    archive.unlink()
    # 完整带入当前差异，避免测试与生产源码错配；后续观测修改只发生在该独占副本。
    difference = subprocess.check_output(["git", "diff", "--binary", head], cwd=project_root)
    subprocess.run(["git", "apply", "--whitespace=nowarn", "-"], input=difference,
                   cwd=snapshot, env={**os.environ, "GIT_CEILING_DIRECTORIES": str(snapshot.parent)}, check=True)
    # 未提交的新源码也属于当前完整实现，不能只快照已跟踪文件。
    untracked = subprocess.check_output(
        ["git", "ls-files", "--others", "--exclude-standard", "-z"], cwd=project_root,
    ).decode().split("\0")
    untracked_hashes = {}
    for name in filter(None, untracked):
        destination = snapshot / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(project_root / name, destination)
        untracked_hashes[name] = hashlib.sha256(destination.read_bytes()).hexdigest()
    for name in ("node_modules", "src/clients/web/node_modules", "src/clients/workbench/node_modules"):
        destination = snapshot / name
        if destination.is_dir():
            destination.rmdir()
        destination.symlink_to(project_root / name, target_is_directory=True)
    # 转换锚点依赖上游在此快照中的真实路径，不能将上游源码链接回另一工作树。
    upstream = project_root / "src/clients/workbench/upstream"
    upstream_snapshot = snapshot / "src/clients/workbench/upstream"
    upstream_snapshot.mkdir(exist_ok=True)
    subprocess.run(["git", "archive", "--format=tar", "-o", str(archive), "HEAD"], cwd=upstream, check=True)
    with tarfile.open(archive) as files:
        files.extractall(upstream_snapshot, filter="data")
    archive.unlink()
    upstream_git = subprocess.check_output(["git", "rev-parse", "--absolute-git-dir"], cwd=upstream, text=True).strip()
    (upstream_snapshot / ".git").write_text(f"gitdir: {upstream_git}\n", encoding="utf-8")
    return snapshot, {
        "head": head,
        "diff_sha256": hashlib.sha256(difference).hexdigest(),
        "untracked_sha256": untracked_hashes,
        "upstream_head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=upstream, text=True).strip(),
        "boundary": "完整 git archive 和当前 diff，上游也是固定 HEAD 的完整源码快照；仅依赖只读链接",
    }
