from __future__ import annotations

import shutil
from pathlib import Path


def prepare_e2e_workspace(
    *,
    workspace_root: Path,
    template_root: Path,
    template_items: tuple[str, ...],
) -> Path:
    """从只读模板创建全新的隔离 E2E 工作区。"""

    if workspace_root.exists():
        shutil.rmtree(workspace_root)
    workspace_root.mkdir(parents=True, exist_ok=True)

    for relative_item in template_items:
        source = template_root / relative_item
        if not source.exists():
            raise FileNotFoundError(f"E2E 模板缺少必要文件: {source}")
        target = workspace_root / relative_item
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(source, target)
        else:
            shutil.copy2(source, target)
    return workspace_root


def prepare_default_e2e_workspace(*, workspace_root: Path, template_root: Path) -> Path:
    """完整复制默认只读模板，创建文件级隔离工作区。"""

    template_items = tuple(item.name for item in template_root.iterdir())
    return prepare_e2e_workspace(
        workspace_root=workspace_root,
        template_root=template_root,
        template_items=template_items,
    )


def install_e2e_workspace_config(
    *,
    workspace_root: Path,
    config_path: Path,
    schema_path: Path,
) -> Path:
    """将正式测试配置与 schema 复制到隔离工作区。"""

    resolved_config_path = config_path.resolve()
    if not resolved_config_path.is_file():
        raise FileNotFoundError(f"E2E 配置不存在: {resolved_config_path}")
    resolved_schema_path = schema_path.resolve()
    if not resolved_schema_path.is_file():
        raise FileNotFoundError(f"E2E 配置 schema 不存在: {resolved_schema_path}")

    target_path = workspace_root / ".boxteam" / "workspace.jsonc"
    target_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(resolved_config_path, target_path)
    shutil.copy2(resolved_schema_path, target_path.parent / "workspace_config.jsonc")
    return target_path
