"""用任务私有索引运行 Git，保留原始参数、输出与退出码。"""

from __future__ import annotations

import argparse
import ast
import os
import shutil
import subprocess
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--task-dir", type=Path, required=True)
    parser.add_argument("git_args", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.git_args
    if command[:1] == ["--"]:
        command = command[1:]
    if not command:
        parser.error("必须在 -- 后提供 Git 参数")
    if command[0].startswith("-"):
        parser.error("不支持 Git 全局选项前缀；请直接提供 Git 子命令")
    if not args.task_dir.is_absolute():
        parser.error("--task-dir 必须为绝对路径")
    root = args.repo_root.resolve()
    task_dir = args.task_dir.resolve()
    if not task_dir.is_relative_to(root / "out/tests/temp"):
        parser.error("--task-dir 必须位于该仓库的 out/tests/temp/ 内")

    git_dir = task_dir / "git"
    if git_dir.is_symlink():
        parser.error("--task-dir/git 不得是符号链接")
    if git_dir.exists() and not git_dir.is_dir():
        parser.error("--task-dir/git 必须是目录")
    git_dir.mkdir(parents=True, exist_ok=True)
    index = git_dir / "index"
    if index.is_symlink():
        parser.error("--task-dir/git/index 不得是符号链接")
    if index.exists() and not index.is_file():
        parser.error("--task-dir/git/index 必须是普通文件")
    env = {**os.environ, "GIT_INDEX_FILE": str(index), "GIT_OPTIONAL_LOCKS": "0"}
    # 只初始化新私有索引；后续 add/commit 共用它，不重置已有暂存。
    if not index.exists():
        initialized = subprocess.run(
            ["git", "read-tree", "HEAD"], cwd=root, env=env, check=False
        )
        if initialized.returncode:
            raise SystemExit(initialized.returncode)

    if command[0] == "commit":
        try:
            reject_commit_worktree_inputs(command[1:])
            validate_staged_python(root, env)
        except RuntimeError as error:
            raise SystemExit(f"提交前 Python blob 检查失败：{error}") from error

    os.chdir(root)
    os.execvpe("git", ["git", *command], env)


def reject_commit_worktree_inputs(arguments: list[str]) -> None:
    """拒绝从工作树或路径参数选择内容，只允许提交已核验的私有索引。"""
    blocked_options = {
        "--all",
        "--include",
        "--interactive",
        "--only",
        "--patch",
        "--pathspec-file-nul",
        "--pathspec-from-file",
    }
    long_value_options = {
        "--author",
        "--cleanup",
        "--date",
        "--file",
        "--fixup",
        "--message",
        "--reedit-message",
        "--reuse-message",
        "--squash",
        "--template",
        "--trailer",
    }
    short_value_options = {"C", "F", "c", "m", "S", "t"}
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        option = argument.split("=", 1)[0]
        if argument in {"--", "-"} or option in blocked_options:
            raise RuntimeError("commit 参数会选择工作树内容或路径，检查只保护已核验的私有索引")
        if argument.startswith("--"):
            if argument == option and option in long_value_options:
                index += 2
            else:
                index += 1
            continue
        if argument.startswith("-"):
            flags = argument[1:]
            while flags and flags[0] not in short_value_options:
                if flags[0] in "aiop":
                    raise RuntimeError("commit 参数会选择工作树内容或路径，检查只保护已核验的私有索引")
                flags = flags[1:]
            if flags and len(argument) == 2:
                index += 2
            else:
                index += 1
            continue
        raise RuntimeError("commit 路径参数会选择工作树内容，检查只保护已核验的私有索引")


def validate_staged_python(root: Path, env: dict[str, str]) -> None:
    """只检查私有索引中相对 HEAD 变动的 Python blob。"""
    changed = subprocess.run(
        [
            "git",
            "diff",
            "--cached",
            "--no-renames",
            "--name-only",
            "--diff-filter=ACMRTUXB",
            "-z",
            "HEAD",
            "--",
            "*.py",
        ],
        cwd=root,
        env=env,
        capture_output=True,
        check=False,
    )
    if changed.returncode:
        detail = changed.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(
            f"读取私有索引 Python 变更失败（Git 退出码 {changed.returncode}）：{detail}"
        )
    paths = changed.stdout.split(b"\0")
    paths = [os.fsdecode(path) for path in paths if path]
    if not paths:
        return

    ruff = shutil.which("ruff")
    if ruff is None:
        raise RuntimeError("变动 Python 文件需要 Ruff CLI 执行 F821/F823 检查，但 PATH 中找不到 ruff")

    for path in paths:
        staged = subprocess.run(
            ["git", "ls-files", "--stage", "-z", "--", f":(literal){path}"],
            cwd=root,
            env=env,
            capture_output=True,
            check=False,
        )
        if staged.returncode:
            detail = staged.stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(
                f"读取私有索引条目失败：{path}（Git 退出码 {staged.returncode}）：{detail}"
            )
        entries: list[tuple[bytes, bytes, bytes]] = []
        for record in staged.stdout.split(b"\0"):
            if not record:
                continue
            header, staged_path = record.split(b"\t", 1)
            mode, object_id, stage = header.split(b" ", 2)
            if staged_path == os.fsencode(path):
                entries.append((mode, object_id, stage))
        if len(entries) != 1 or entries[0][2] != b"0":
            raise RuntimeError(f"私有索引中的 Python 文件缺少唯一 stage 0 blob：{path}")
        mode, object_id, _ = entries[0]
        if mode not in {b"100644", b"100755"}:
            raise RuntimeError(f"私有索引中的 Python 文件不是普通文件：{path}（mode {mode.decode()}）")
        blob = subprocess.run(
            ["git", "cat-file", "blob", object_id.decode("ascii")],
            cwd=root,
            env=env,
            capture_output=True,
            check=False,
        )
        if blob.returncode:
            detail = blob.stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(
                f"读取私有索引 Python blob 失败：{path}（Git 退出码 {blob.returncode}）：{detail}"
            )

        try:
            ast.parse(blob.stdout, filename=path)
        except (SyntaxError, UnicodeError) as error:
            line = getattr(error, "lineno", None)
            location = f":{line}" if line is not None else ""
            detail = getattr(error, "msg", str(error))
            raise RuntimeError(f"{path}{location}: Python AST 解析失败：{detail}") from error

        try:
            lint = subprocess.run(
                [
                    ruff,
                    "check",
                    "--isolated",
                    "--target-version",
                    "py311",
                    "--select",
                    "F821,F823",
                    "--output-format",
                    "concise",
                    "--stdin-filename",
                    path,
                    "-",
                ],
                input=blob.stdout,
                capture_output=True,
                check=False,
            )
        except OSError as error:
            raise RuntimeError(f"运行 Ruff F821/F823 检查失败：{error}") from error
        if lint.returncode:
            detail = (lint.stdout + lint.stderr).decode("utf-8", errors="replace").strip()
            raise RuntimeError(
                f"{path}: Ruff F821/F823 检查退出码 {lint.returncode}：{detail or '无诊断输出'}"
            )


if __name__ == "__main__":
    main()
