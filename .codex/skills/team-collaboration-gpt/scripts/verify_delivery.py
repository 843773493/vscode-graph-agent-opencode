#!/usr/bin/env python3
"""从固定 Git 对象导出并核验可重建的代码交付。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


class DeliveryError(Exception):
    """交付边界或 Git 对象不满足验证条件。"""


@dataclass(frozen=True)
class GitObject:
    reference: str
    object_type: str
    object_sha: str
    tree_sha: str


@dataclass(frozen=True)
class FileState:
    mode: str
    object_type: str
    git_sha: str | None
    sha256: str | None = None

    def same_as(self, other: FileState | None) -> bool:
        return other is not None and (
            self.mode,
            self.object_type,
            self.git_sha,
        ) == (other.mode, other.object_type, other.git_sha)

    def manifest(self) -> dict[str, str | None]:
        return {
            "mode": self.mode,
            "object_type": self.object_type,
            "git_sha": self.git_sha,
            "sha256": self.sha256,
        }


@dataclass(frozen=True)
class Delta:
    status: str
    path: str
    old_state: FileState | None
    new_state: FileState | None

    def manifest(self) -> dict[str, object]:
        return {
            "status": self.status,
            "path": self.path,
            "old": self.old_state.manifest() if self.old_state else None,
            "new": self.new_state.manifest() if self.new_state else None,
        }


@dataclass(frozen=True)
class DeferredRule:
    declared: str
    path: str

    def matches(self, source_path: str) -> bool:
        return source_path == self.path


class Git:
    def __init__(self, repo: Path, index_file: Path, output_dir: Path) -> None:
        self.repo = repo
        self.output_dir = output_dir
        self.failure_sequence = 0
        self.env = os.environ.copy()
        self.env["GIT_INDEX_FILE"] = str(index_file)
        self.env["GIT_OPTIONAL_LOCKS"] = "0"

    def write_failure_log(
        self, operation: str, args: Sequence[str], exit_code: int, stderr: bytes
    ) -> Path:
        self.failure_sequence += 1
        log_path = self.output_dir / f"git-failure-{self.failure_sequence:03d}.log"
        while log_path.exists():
            self.failure_sequence += 1
            log_path = self.output_dir / f"git-failure-{self.failure_sequence:03d}.log"
        metadata = {
            "operation": operation,
            "argv": ["git", *args],
            "exit_code": exit_code,
        }
        log_path.write_bytes(
            json.dumps(metadata, ensure_ascii=False, sort_keys=True).encode("utf-8")
            + b"\n--- stderr (raw) ---\n"
            + stderr
        )
        return log_path

    def run(
        self,
        operation: str,
        args: Sequence[str],
        *,
        cwd: Path | None = None,
        input_data: bytes | None = None,
    ) -> bytes:
        completed = subprocess.run(
            ["git", *args],
            cwd=cwd or self.repo,
            env=self.env,
            input=input_data,
            capture_output=True,
            check=False,
        )
        if completed.returncode != 0:
            log_path = self.write_failure_log(
                operation, args, completed.returncode, completed.stderr
            )
            stderr_summary = next(
                (
                    re.sub(
                        r"[\x00-\x1f\x7f]",
                        " ",
                        line.decode("utf-8", errors="replace"),
                    ).strip()[:400]
                    for line in completed.stderr.splitlines()
                    if line.strip()
                ),
                "Git 未提供 stderr 说明",
            )
            raise DeliveryError(
                f"{operation}失败，Git 退出码 {completed.returncode}；"
                f"原因：{stderr_summary}；原始 stderr 日志：{log_path}"
            )
        return completed.stdout

    def text(
        self,
        operation: str,
        args: Sequence[str],
        *,
        cwd: Path | None = None,
    ) -> str:
        return self.run(operation, args, cwd=cwd).decode("ascii").strip()


SOURCE_ROOTS = (
    ".codex",
    "app",
    "configs",
    "docs",
    "examples",
    "migrations",
    "proto",
    "scripts",
    "src",
    "tests",
    "tools",
)
SOURCE_SUFFIXES = {
    ".c",
    ".cc",
    ".cpp",
    ".cjs",
    ".css",
    ".go",
    ".h",
    ".hpp",
    ".html",
    ".java",
    ".js",
    ".json",
    ".jsonc",
    ".jsx",
    ".kt",
    ".md",
    ".mjs",
    ".mts",
    ".proto",
    ".ps1",
    ".py",
    ".pyi",
    ".rs",
    ".scss",
    ".sh",
    ".sql",
    ".swift",
    ".toml",
    ".ts",
    ".tsx",
    ".vue",
    ".yaml",
    ".yml",
}
BINARY_SUFFIXES = {
    ".7z",
    ".avi",
    ".db",
    ".dll",
    ".exe",
    ".gif",
    ".gz",
    ".ico",
    ".jpeg",
    ".jpg",
    ".mp3",
    ".mp4",
    ".pdf",
    ".png",
    ".pyc",
    ".sqlite",
    ".tar",
    ".webm",
    ".woff",
    ".woff2",
    ".zip",
}
DATA_SUFFIXES = {".jsonl", ".log", ".ndjson"}
GENERATED_COMPONENTS = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "build",
    "coverage",
    "dist",
    "node_modules",
    "out",
    "target",
}
ROOT_SOURCE_NAMES = {
    ".editorconfig",
    ".gitignore",
    ".prettierignore",
    ".prettierrc",
    "Dockerfile",
    "Makefile",
    "AGENTS.md",
}
SECRET_NAMES = {
    ".env",
    ".npmrc",
    ".pypirc",
    "id_ed25519",
    "id_rsa",
}
SECRET_SUFFIXES = {".key", ".pem", ".p12", ".pfx", ".token"}


def is_secret_path(path: str) -> bool:
    name = PurePosixPath(path).name.lower()
    return (
        name in SECRET_NAMES
        or (name.startswith(".env.") and name not in {".env.example", ".env.sample"})
        or Path(name).suffix in SECRET_SUFFIXES
        or name.startswith("credentials.")
    )


def is_source_path(path: str) -> bool:
    normalized = PurePosixPath(path)
    if normalized.is_absolute() or ".." in normalized.parts:
        raise DeliveryError("Git 返回了非仓库相对源码路径")
    parts = normalized.parts
    if not parts or any(
        part in GENERATED_COMPONENTS or part == ".boxteam" for part in parts
    ):
        return False
    if is_secret_path(path):
        return False
    if parts[0] == "asset" or parts[0] == "reference_repo":
        return False
    suffix = normalized.suffix.lower()
    if suffix in BINARY_SUFFIXES or suffix in DATA_SUFFIXES:
        return False
    if suffix in SOURCE_SUFFIXES or normalized.name in ROOT_SOURCE_NAMES:
        return True
    return parts[0] in SOURCE_ROOTS and suffix == ""


def normalize_deferred(value: str) -> DeferredRule:
    if not value or value.startswith("/") or "\\" in value:
        raise DeliveryError(f"deferred-path 必须是精确仓库相对路径：{value!r}")
    if any(character in value for character in "*?[]{}"):
        raise DeliveryError("deferred-path 不接受 glob 或通配符")
    if value.endswith("/"):
        raise DeliveryError("deferred-path 必须是精确文件路径，不接受目录前缀")
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise DeliveryError(f"deferred-path 不是规范路径：{value!r}")
    return DeferredRule(value, value)


def resolve_git_object(git: Git, reference: str, label: str) -> GitObject:
    object_sha = git.text(
        f"解析{label}", ["rev-parse", "--verify", "--end-of-options", reference]
    )
    if not re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", object_sha):
        raise DeliveryError(f"{label} 未解析为完整 Git object SHA")
    object_type = git.text(f"读取{label} object type", ["cat-file", "-t", object_sha])
    if object_type not in {"commit", "tag", "tree"}:
        raise DeliveryError(f"{label} 必须指向 commit、tag 或 tree object")
    tree_sha = git.text(
        f"读取{label} tree SHA",
        ["rev-parse", "--verify", "--end-of-options", f"{object_sha}^{{tree}}"],
    )
    return GitObject(reference, object_type, object_sha, tree_sha)


def state_from_object(
    git: Git,
    mode: str,
    object_type: str,
    object_sha: str,
    *,
    include_hash: bool,
) -> FileState | None:
    if not object_sha or set(object_sha) == {"0"}:
        return None
    sha256: str | None = None
    if include_hash and object_type == "blob":
        content = git.run(
            "读取 blob 内容用于 SHA-256", ["cat-file", "blob", object_sha]
        )
        sha256 = hashlib.sha256(content).hexdigest()
    return FileState(mode, object_type, object_sha, sha256)


def read_delta(git: Git, baseline: GitObject, candidate: GitObject) -> list[Delta]:
    raw = git.run(
        "读取 base-to-target raw diff",
        [
            "diff-tree",
            "--no-commit-id",
            "--raw",
            "--no-renames",
            "--no-abbrev",
            "-r",
            "-z",
            baseline.tree_sha,
            candidate.tree_sha,
        ],
    )
    fields = raw.split(b"\0")
    deltas: list[Delta] = []
    index = 0
    while index < len(fields) and fields[index]:
        header = fields[index].decode("ascii")
        index += 1
        if not header.startswith(":") or index >= len(fields):
            raise DeliveryError("Git raw diff 格式不完整")
        metadata = header[1:].split()
        if len(metadata) != 5:
            raise DeliveryError("Git raw diff 元数据字段数不正确")
        old_mode, new_mode, old_sha, new_sha, status = metadata
        path = os.fsdecode(fields[index])
        index += 1
        if not path:
            raise DeliveryError("Git raw diff 返回了空路径")
        if is_secret_path(path):
            raise DeliveryError(
                "候选增量涉及密钥类路径；已拒绝导出且未读取或输出其内容"
            )
        old_type = "blob" if old_mode != "000000" else ""
        new_type = "blob" if new_mode != "000000" else ""
        if old_mode == "160000":
            old_type = "commit"
        if new_mode == "160000":
            new_type = "commit"
        old_state = state_from_object(
            git, old_mode, old_type, old_sha, include_hash=True
        )
        new_state = state_from_object(
            git, new_mode, new_type, new_sha, include_hash=True
        )
        deltas.append(Delta(status[0], path, old_state, new_state))
    return deltas


def read_tree(git: Git, tree_sha: str) -> dict[str, FileState]:
    raw = git.run("列出候选 tree", ["ls-tree", "-r", "-z", "--full-tree", tree_sha])
    entries: dict[str, FileState] = {}
    for field in raw.split(b"\0"):
        if not field:
            continue
        metadata, separator, path_bytes = field.partition(b"\t")
        if not separator:
            raise DeliveryError("候选 tree 项缺少路径分隔符")
        mode, object_type, object_sha = metadata.decode("ascii").split()
        path = os.fsdecode(path_bytes)
        if is_source_path(path):
            entries[path] = FileState(mode, object_type, object_sha)
    return entries


def author_source_paths(author: Path) -> set[str]:
    paths: set[str] = set()
    for current, directories, files in os.walk(author, topdown=True, followlinks=False):
        current_path = Path(current)
        relative_current = current_path.relative_to(author)
        symlink_directories = [
            name for name in directories if (current_path / name).is_symlink()
        ]
        directories[:] = [
            name
            for name in directories
            if name not in GENERATED_COMPONENTS
            and name != ".boxteam"
            and name not in symlink_directories
            and not is_secret_path((relative_current / name).as_posix())
            and not (
                relative_current == Path(".")
                and name in {"asset", "reference_repo"}
            )
        ]
        for name in (*files, *symlink_directories):
            relative = (relative_current / name).as_posix()
            if is_source_path(relative):
                paths.add(relative)
    return paths


def worktree_state(git: Git, author: Path, path: str) -> FileState | None:
    absolute_path = author.joinpath(*PurePosixPath(path).parts)
    try:
        metadata = absolute_path.lstat()
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(metadata.st_mode):
        target = os.fsencode(os.readlink(absolute_path))
        object_sha = (
            git.run(
                "计算作者工作树符号链接 blob",
                ["hash-object", "--stdin"],
                cwd=author,
                input_data=target,
            )
            .decode("ascii")
            .strip()
        )
        return FileState("120000", "blob", object_sha)
    if stat.S_ISREG(metadata.st_mode):
        mode = (
            "100755"
            if metadata.st_mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
            else "100644"
        )
        object_sha = git.text(
            "计算作者工作树文件 blob",
            ["hash-object", f"--path={path}", "--", path],
            cwd=author,
        )
        return FileState(mode, "blob", object_sha)
    if stat.S_ISDIR(metadata.st_mode):
        return FileState("040000", "tree", None)
    return FileState("000000", "unsupported", None)


def with_worktree_hash(state: FileState, author: Path, path: str) -> FileState:
    absolute_path = author.joinpath(*PurePosixPath(path).parts)
    if state.mode == "120000":
        content = os.fsencode(os.readlink(absolute_path))
    elif state.mode in {"100644", "100755"}:
        digest = hashlib.sha256()
        with absolute_path.open("rb") as file:
            for chunk in iter(lambda: file.read(1024 * 1024), b""):
                digest.update(chunk)
        return FileState(
            state.mode, state.object_type, state.git_sha, digest.hexdigest()
        )
    else:
        return state
    return FileState(
        state.mode,
        state.object_type,
        state.git_sha,
        hashlib.sha256(content).hexdigest(),
    )


def with_git_blob_hash(git: Git, state: FileState) -> FileState:
    if state.object_type != "blob" or state.git_sha is None:
        return state
    content = git.run("读取源码 blob 用于 SHA-256", ["cat-file", "blob", state.git_sha])
    return FileState(
        state.mode,
        state.object_type,
        state.git_sha,
        hashlib.sha256(content).hexdigest(),
    )


def normalize_absolute_dir(value: str, label: str, *, exists: bool) -> Path:
    path = Path(value)
    if not path.is_absolute():
        raise DeliveryError(f"{label} 必须是绝对路径")
    resolved = path.resolve(strict=exists)
    if exists and not resolved.is_dir():
        raise DeliveryError(f"{label} 必须是目录")
    return resolved


def validate_output_roots(
    repo: Path, output_dir: Path, index_dir: Path
) -> tuple[Path, Path]:
    temp_root = (repo / "out" / "tests" / "temp").resolve(strict=True)
    if index_dir.name != "git":
        raise DeliveryError("--index-dir 必须指向任务产物根下的 git/ 目录")
    task_root = index_dir.parent
    artifacts_root = task_root / "artifacts"
    if not task_root.is_relative_to(temp_root):
        raise DeliveryError("--index-dir 必须位于仓库 out/tests/temp/ 下")
    if not output_dir.is_relative_to(artifacts_root) or output_dir == artifacts_root:
        raise DeliveryError("--output-dir 必须是同一任务 artifacts/ 下的候选专属子目录")
    return task_root, artifacts_root


def normalize_rules(values: Sequence[str]) -> list[DeferredRule]:
    rules = [normalize_deferred(value) for value in values]
    declared = [rule.declared for rule in rules]
    if len(set(declared)) != len(declared):
        raise DeliveryError("deferred-path 不得重复")
    return rules


def rule_for_path(path: str, rules: Sequence[DeferredRule]) -> DeferredRule | None:
    matches = [rule for rule in rules if rule.matches(path)]
    if len(matches) > 1:
        raise DeliveryError(
            f"源码差异 {path} 同时匹配多个 deferred-path，声明过宽或重叠"
        )
    return matches[0] if matches else None


def verify_common_repository(git: Git, repo: Path, author: Path) -> None:
    repo_root = Path(
        git.text("核验 --repo", ["rev-parse", "--show-toplevel"], cwd=repo)
    ).resolve()
    author_root = Path(
        git.text("核验作者工作树", ["rev-parse", "--show-toplevel"], cwd=author)
    ).resolve()
    if repo_root != repo or author_root != author:
        raise DeliveryError("--repo 与 --author-worktree 必须是 Git 工作树根目录")
    repo_common = Path(
        git.text(
            "读取 --repo Git common dir",
            ["rev-parse", "--path-format=absolute", "--git-common-dir"],
            cwd=repo,
        )
    ).resolve()
    author_common = Path(
        git.text(
            "读取作者工作树 Git common dir",
            ["rev-parse", "--path-format=absolute", "--git-common-dir"],
            cwd=author,
        )
    ).resolve()
    if repo_common != author_common:
        raise DeliveryError(
            "--repo 与 --author-worktree 不属于同一 Git object database"
        )


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="从固定 Git base/target 导出补丁，严格重建候选并审计作者源码差异。"
    )
    parser.add_argument("--repo", required=True, help="Git 仓库根目录绝对路径")
    parser.add_argument(
        "--baseline", required=True, help="固定 baseline commit、tag 或 tree"
    )
    parser.add_argument(
        "--candidate", required=True, help="固定 candidate commit、tag 或 tree"
    )
    parser.add_argument(
        "--author-worktree", required=True, help="作者工作树根目录绝对路径"
    )
    parser.add_argument(
        "--output-dir", required=True, help="候选专属 artifacts 子目录绝对路径"
    )
    parser.add_argument(
        "--index-dir",
        required=True,
        help="同任务 out/tests/temp/<task>/git/ 目录绝对路径",
    )
    parser.add_argument(
        "--deferred-path",
        action="append",
        default=[],
        help="逐项声明候选之外的单个源码文件路径；必须精确匹配，不支持目录前缀或通配符",
    )
    return parser.parse_args(argv)


def run(args: argparse.Namespace) -> dict[str, object]:
    repo = normalize_absolute_dir(args.repo, "--repo", exists=True)
    author = normalize_absolute_dir(
        args.author_worktree, "--author-worktree", exists=True
    )
    output_dir = normalize_absolute_dir(args.output_dir, "--output-dir", exists=False)
    index_dir = normalize_absolute_dir(args.index_dir, "--index-dir", exists=True)
    validate_output_roots(repo, output_dir, index_dir)
    rules = normalize_rules(args.deferred_path)
    output_dir.mkdir(parents=True, exist_ok=True)

    descriptor, index_name = tempfile.mkstemp(
        prefix="verify-delivery-", suffix=".idx", dir=index_dir
    )
    os.close(descriptor)
    index_file = Path(index_name)
    index_file.unlink()
    try:
        git = Git(repo, index_file, output_dir)
        verify_common_repository(git, repo, author)
        baseline = resolve_git_object(git, args.baseline, "baseline")
        candidate = resolve_git_object(git, args.candidate, "candidate")
        deltas = read_delta(git, baseline, candidate)
        delta_by_path = {delta.path: delta for delta in deltas}
        candidate_source = read_tree(git, candidate.tree_sha)

        git.run("初始化私有 fresh index", ["read-tree", baseline.tree_sha])
        patch_bytes = git.run(
            "从 Git objects 导出 binary full-index patch",
            [
                "diff",
                "--binary",
                "--full-index",
                "--no-ext-diff",
                "--no-renames",
                "--src-prefix=a/",
                "--dst-prefix=b/",
                baseline.tree_sha,
                candidate.tree_sha,
                "--",
            ],
        )
        git.run(
            "strict cached apply check",
            ["apply", "--cached", "--check", "--binary", "-"],
            input_data=patch_bytes,
        )
        git.run(
            "strict cached apply",
            ["apply", "--cached", "--binary", "-"],
            input_data=patch_bytes,
        )
        rebuilt_tree = git.text("重建 candidate tree", ["write-tree"])
        if rebuilt_tree != candidate.tree_sha:
            raise DeliveryError(
                f"严格重建 tree 与 candidate 不一致：expected={candidate.tree_sha}, actual={rebuilt_tree}"
            )

        worktree_cache: dict[str, FileState | None] = {}

        def live_state(path: str) -> FileState | None:
            if path not in worktree_cache:
                worktree_cache[path] = worktree_state(git, author, path)
            return worktree_cache[path]

        delivered_mismatches: list[str] = []
        for path, delta in delta_by_path.items():
            current = live_state(path)
            if delta.new_state is None:
                if current is not None:
                    delivered_mismatches.append(path)
            elif not delta.new_state.same_as(current):
                delivered_mismatches.append(path)

        live_source_paths = author_source_paths(author)
        source_paths = set(candidate_source) | live_source_paths
        source_differences: list[dict[str, object]] = []
        matched_rules: set[str] = set()
        uncovered: list[str] = []
        for path in sorted(source_paths):
            expected = candidate_source.get(path)
            current = live_state(path)
            if expected is None and current is None:
                continue
            if expected is not None and expected.same_as(current):
                continue
            if path in delta_by_path:
                continue
            rule = rule_for_path(path, rules)
            if rule is None:
                uncovered.append(path)
            else:
                matched_rules.add(rule.declared)
            expected_hashed = with_git_blob_hash(git, expected) if expected else None
            current_hashed = (
                with_worktree_hash(current, author, path) if current else None
            )
            source_differences.append(
                {
                    "path": path,
                    "candidate": expected_hashed.manifest()
                    if expected_hashed
                    else None,
                    "author_worktree": current_hashed.manifest()
                    if current_hashed
                    else None,
                    "deferred_path": rule.declared if rule else None,
                }
            )

        unused_rules = sorted(
            rule.declared for rule in rules if rule.declared not in matched_rules
        )
        if delivered_mismatches or uncovered or unused_rules:
            problems: list[str] = []
            if delivered_mismatches:
                problems.append(
                    "交付路径的作者工作树内容或 mode 不等于 candidate："
                    + ", ".join(delivered_mismatches)
                )
            if uncovered:
                problems.append(
                    "候选外源码差异缺少逐路径 deferred-path：" + ", ".join(uncovered)
                )
            if unused_rules:
                problems.append(
                    "deferred-path 未对应实际源码差异：" + ", ".join(unused_rules)
                )
            raise DeliveryError("；".join(problems))

        patch_name = f"delivery-{candidate.tree_sha[:12]}.patch"
        manifest_name = f"delivery-{candidate.tree_sha[:12]}.manifest.json"
        patch_sha256 = hashlib.sha256(patch_bytes).hexdigest()
        manifest: dict[str, object] = {
            "schema": "codex.delivery.v1",
            "baseline": {
                "reference": baseline.reference,
                "object_type": baseline.object_type,
                "object_sha": baseline.object_sha,
                "tree_sha": baseline.tree_sha,
            },
            "candidate": {
                "reference": candidate.reference,
                "object_type": candidate.object_type,
                "object_sha": candidate.object_sha,
                "tree_sha": candidate.tree_sha,
            },
            "patch": {
                "path": patch_name,
                "sha256": patch_sha256,
                "bytes": len(patch_bytes),
            },
            "reconstruction": {
                "cached_apply_check": "passed",
                "cached_apply": "passed",
                "rebuilt_tree_sha": rebuilt_tree,
            },
            "delta": [delta.manifest() for delta in deltas],
            "author_worktree": str(author),
            "author_source_differences": source_differences,
            "deferred_paths": sorted(matched_rules),
        }
        manifest_bytes = (
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")
        patch_path = output_dir / patch_name
        manifest_path = output_dir / manifest_name
        if patch_path.exists() or manifest_path.exists():
            if (
                patch_path.exists()
                and manifest_path.exists()
                and patch_path.read_bytes() == patch_bytes
                and manifest_path.read_bytes() == manifest_bytes
            ):
                return {
                    "candidate_tree": candidate.tree_sha,
                    "patch": str(patch_path),
                    "manifest": str(manifest_path),
                    "delta_paths": len(deltas),
                    "author_source_differences": len(source_differences),
                    "reused_identical_outputs": True,
                }
            raise DeliveryError("候选产物路径已存在且内容不同；拒绝覆盖")
        patch_path.write_bytes(patch_bytes)
        manifest_path.write_bytes(manifest_bytes)
        return {
            "candidate_tree": candidate.tree_sha,
            "patch": str(patch_path),
            "manifest": str(manifest_path),
            "delta_paths": len(deltas),
            "author_source_differences": len(source_differences),
            "reused_identical_outputs": False,
        }
    finally:
        index_file.unlink(missing_ok=True)
        Path(f"{index_file}.lock").unlink(missing_ok=True)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        result = run(args)
    except DeliveryError as error:
        print(f"verify_delivery: {error}", file=sys.stderr)
        return 2
    except OSError as error:
        print(
            f"verify_delivery: 文件操作失败：{error.strerror or type(error).__name__}",
            file=sys.stderr,
        )
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
