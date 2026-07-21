from __future__ import annotations

import json
import os
import tempfile
from importlib.resources import files
from pathlib import Path

from configs.defaults import build_boxteam_config


def atomic_write(path: Path, content: bytes, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise RuntimeError(f"拒绝覆盖符号链接: {path}")
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary_name)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    temporary_path.chmod(mode)
    os.replace(temporary_path, path)


def write_boxteam_config(
    path: Path,
    *,
    development_assets: bool,
    gateway_e2e_workspace_enabled: bool = False,
) -> None:
    payload = build_boxteam_config(
        development_assets=development_assets,
        gateway_e2e_workspace_enabled=gateway_e2e_workspace_enabled,
    )
    content = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    atomic_write(path.expanduser().resolve(), content, 0o600)


def resolve_config_schema_source(*, project_root: Path | None = None) -> Path:
    if project_root is not None:
        source = project_root.expanduser().resolve() / "configs" / "config.jsonc"
    else:
        source = Path(str(files("configs").joinpath("config.jsonc"))).resolve()
    if not source.is_file():
        raise FileNotFoundError(f"配置 schema 不存在: {source}")
    return source


def install_config_schema(
    *,
    config_path: Path,
    project_root: Path | None = None,
) -> Path:
    source = resolve_config_schema_source(project_root=project_root)
    target = config_path.expanduser().resolve().parent / "config.schema.jsonc"
    atomic_write(target, source.read_bytes(), 0o600)
    return target


def initialize_boxteam_config(
    config_path: Path,
    *,
    development_assets: bool,
    gateway_e2e_workspace_enabled: bool = False,
    project_root: Path | None = None,
    force: bool = False,
) -> bool:
    resolved_path = config_path.expanduser().resolve()
    created = force or not resolved_path.exists()
    if created:
        write_boxteam_config(
            resolved_path,
            development_assets=development_assets,
            gateway_e2e_workspace_enabled=gateway_e2e_workspace_enabled,
        )
    install_config_schema(project_root=project_root, config_path=resolved_path)
    return created
