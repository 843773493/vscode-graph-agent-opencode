#!/usr/bin/env python3
"""
Test environment setup script.
Clears playground directory and copies asset/workspace to playground/workspace.
Sets WORKSPACE_ROOT environment variable for testing.
"""
import os
import shutil
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
    
    # Verify directories exist
    boxteam_dir = target_workspace / ".boxteam"
    for subdir in ["sessions", "logs", "artifacts", "cache"]:
        (boxteam_dir / subdir).mkdir(exist_ok=True, parents=True)
    
    print("\nTest environment setup complete!")
    print(f"   Workspace root: {target_workspace}")
    print(f"   All tests will run in isolated playground environment")

if __name__ == "__main__":
    setup_test_environment()
