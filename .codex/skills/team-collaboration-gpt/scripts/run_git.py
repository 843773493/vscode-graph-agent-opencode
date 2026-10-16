"""用任务私有索引运行 Git，保留原始参数、输出与退出码。"""

from __future__ import annotations

import argparse
import os
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
    os.chdir(root)
    os.execvpe("git", ["git", *command], env)


if __name__ == "__main__":
    main()
