from __future__ import annotations

import json
import shutil
from functools import lru_cache
from pathlib import Path
from stat import S_IRUSR, S_ISDIR, S_ISLNK, S_IWUSR, S_IXUSR

import commentjson

from app.core.atomic_fs import native_io_path


@lru_cache(maxsize=16)
def render_test_workspace_config(source_text: str) -> str:
    """按完整源文本复用不可变配置正文；各测试仍写入自己的目录。"""
    payload = commentjson.loads(source_text)
    payload["$schema"] = "./workspace_schema.jsonc"
    payload["config_version"] = 1
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"


def _make_test_tree_writable(path: Path) -> None:
    """只放开待删除副本的 owner 权限，不跟随指向模板或其它目录的链接。"""
    mode = path.lstat().st_mode
    if S_ISLNK(mode):
        return
    writable = mode | S_IWUSR | (S_IRUSR | S_IXUSR if S_ISDIR(mode) else 0)
    if writable != mode:
        path.chmod(writable)
    if S_ISDIR(mode):
        for child in path.iterdir():
            _make_test_tree_writable(child)


def prepare_test_workspace(
    *,
    workspace_root: Path,
    template_root: Path,
    template_items: tuple[str, ...] | None,
    shared_skill_root: Path | None = None,
) -> Path:
    """从只读模板创建隔离工作区；None复制完整模板，空tuple创建空目录。"""

    if workspace_root.exists():
        _make_test_tree_writable(native_io_path(workspace_root))
        shutil.rmtree(native_io_path(workspace_root))

    if template_items is None:
        workspace_root.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(template_root, workspace_root)
    else:
        workspace_root.mkdir(parents=True, exist_ok=True)
        for relative_template_path in template_items:
            source = template_root / relative_template_path
            try:
                source_mode = source.stat().st_mode
            except FileNotFoundError as error:
                raise FileNotFoundError(f"测试模板缺少必要文件: {source}") from error
            target = workspace_root / relative_template_path
            target.parent.mkdir(parents=True, exist_ok=True)
            if S_ISDIR(source_mode):
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
    """完整复制只读模板，创建文件级隔离工作区。"""

    return prepare_test_workspace(
        workspace_root=workspace_root,
        template_root=template_root,
        template_items=None,
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
