#!/usr/bin/env python3
"""
Test environment setup script.
Clears playground directory and copies asset/workspace to playground/workspace.
Sets WORKSPACE_ROOT environment variable for testing.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

def setup_test_environment():
    # Get project root
    project_root = Path(__file__).parent.parent.resolve()
    
    # Define paths
    playground_dir = project_root / "playground"
    source_workspace = project_root / "asset" / "workspace"
    target_workspace = playground_dir / "workspace"
    
    # Clear playground directory completely
    if playground_dir.exists():
        shutil.rmtree(playground_dir)
    
    # Recreate playground directory
    playground_dir.mkdir(exist_ok=True, parents=True)
    
    # Copy entire workspace from assets
    if source_workspace.exists():
        shutil.copytree(source_workspace, target_workspace)
        print(f"Copied test workspace from {source_workspace} to {target_workspace}")
    else:
        print(f"⚠ Source workspace not found at {source_workspace}, creating empty workspace")
        target_workspace.mkdir(exist_ok=True, parents=True)
    
    # Set environment variable
    os.environ["WORKSPACE_ROOT"] = str(target_workspace)
    print(f"Set WORKSPACE_ROOT={target_workspace}")
    print(f"测试工作区路径: {target_workspace}")
    
    # Verify directories exist
    boxteam_dir = target_workspace / ".boxteam"
    for subdir in ["sessions", "logs", "artifacts", "cache"]:
        (boxteam_dir / subdir).mkdir(exist_ok=True, parents=True)
    
    print("\nTest environment setup complete!")
    print(f"   Workspace root: {target_workspace}")
    print(f"   All tests will run in isolated playground environment")
    return target_workspace


def ensure_local_bun() -> Path:
    """确保仓库内 bun 运行时可用。"""
    project_root = Path(__file__).parent.parent.resolve()
    bun_path = project_root / "tools" / "bun.exe"

    if bun_path.exists():
        print(f"Found local bun: {bun_path}")
        return bun_path

    print("Local bun not found, trying to install via corepack...")
    result = subprocess.run(
        ["corepack", "prepare", "bun@latest", "--activate"],
        cwd=project_root,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            "Failed to prepare bun via corepack. "
            f"stdout={result.stdout!r} stderr={result.stderr!r}"
        )

    print("bun prepared successfully via corepack")
    return bun_path


def sync_bun_dependencies() -> None:
    """同步前端依赖，配合 uv sync 的后处理使用。"""
    project_root = Path(__file__).parent.parent.resolve()
    bun_path = ensure_local_bun()
    webview_ui_dir = project_root / "src" / "webview-ui"

    if not webview_ui_dir.exists():
        raise FileNotFoundError(f"webview-ui 目录不存在: {webview_ui_dir}")

    result = subprocess.run(
        [str(bun_path), "install"],
        cwd=webview_ui_dir,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"bun install failed with exit code {result.returncode}")

    print("bun dependencies synchronized successfully")

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "sync-bun":
        sync_bun_dependencies()
    else:
        setup_test_environment()
