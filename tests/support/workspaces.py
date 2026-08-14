from __future__ import annotations

import shutil
from pathlib import Path


def prepare_test_workspace(
    *,
    workspace_root: Path,
    template_root: Path,
    template_items: tuple[str, ...],
    shared_skill_root: Path | None = None,
) -> Path:
    """从只读模板创建全新的隔离测试工作区。"""

    if workspace_root.exists():
        shutil.rmtree(workspace_root)
    workspace_root.mkdir(parents=True, exist_ok=True)

    for relative_item in template_items:
        source = template_root / relative_item
        if not source.exists():
            raise FileNotFoundError(f"测试模板缺少必要文件: {source}")
        target = workspace_root / relative_item
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(source, target)
        else:
            shutil.copy2(source, target)
    if shared_skill_root is not None:
        if not shared_skill_root.is_dir():
            raise FileNotFoundError(f"共享 Skill 源目录不存在: {shared_skill_root}")
        target_skill_root = workspace_root / ".boxteam" / "skills"
        target_skill_root.mkdir(parents=True, exist_ok=True)
        for skill_source in sorted(shared_skill_root.iterdir()):
            if skill_source.is_dir():
                shutil.copytree(
                    skill_source,
                    target_skill_root / skill_source.name,
                    dirs_exist_ok=True,
                )
    return workspace_root


def prepare_default_test_workspace(
    *,
    workspace_root: Path,
    template_root: Path,
    shared_skill_root: Path | None = None,
) -> Path:
    """完整复制默认只读模板，创建文件级隔离工作区。"""

    template_items = tuple(item.name for item in template_root.iterdir())
    return prepare_test_workspace(
        workspace_root=workspace_root,
        template_root=template_root,
        template_items=template_items,
        shared_skill_root=shared_skill_root,
    )


def install_test_workspace_config(
    *,
    workspace_root: Path,
    config_path: Path,
    schema_path: Path,
) -> Path:
    """将正式测试配置与 schema 复制到隔离工作区。"""

    resolved_config_path = config_path.resolve()
    if not resolved_config_path.is_file():
        raise FileNotFoundError(f"测试配置不存在: {resolved_config_path}")
    resolved_schema_path = schema_path.resolve()
    if not resolved_schema_path.is_file():
        raise FileNotFoundError(f"测试配置 schema 不存在: {resolved_schema_path}")

    target_path = workspace_root / ".boxteam" / "workspace.jsonc"
    target_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(resolved_config_path, target_path)
    shutil.copy2(resolved_schema_path, target_path.parent / "workspace_schema.jsonc")
    return target_path
