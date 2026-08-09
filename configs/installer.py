from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Literal

import commentjson
import jsonschema

ConfigProfile = Literal["default", "development"]
CONFIG_DOMAINS = ("gateway", "workspace")


@dataclass(frozen=True, slots=True)
class ConfigurationInstallation:
    config_paths: tuple[Path, ...]
    schema_paths: tuple[Path, ...]
    created_config_paths: tuple[Path, ...]
    env_path: Path | None = None


def atomic_write(path: Path, content: bytes, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise RuntimeError(f"拒绝覆盖符号链接: {path}")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        temporary_path.chmod(mode)
        os.replace(temporary_path, path)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def resolve_config_resource_source(
    resource_name: str,
    *,
    project_root: Path | None = None,
) -> Path:
    if Path(resource_name).name != resource_name:
        raise ValueError(f"配置资源名不能包含目录: {resource_name}")
    if project_root is not None:
        source = project_root.expanduser().resolve() / "configs" / resource_name
    else:
        source = Path(str(files("configs").joinpath(resource_name))).resolve()
    if not source.is_file():
        raise FileNotFoundError(f"配置资源不存在: {source}")
    return source


def _read_jsonc_object(path: Path) -> dict[str, object]:
    parsed = commentjson.loads(path.read_text(encoding="utf-8"))
    if not isinstance(parsed, dict):
        raise TypeError(f"JSONC 根节点必须是对象: {path}")
    return parsed


def validate_config_resource_pair(config_source: Path, schema_source: Path) -> None:
    schema = _read_jsonc_object(schema_source)
    jsonschema.Draft202012Validator.check_schema(schema)
    jsonschema.validate(_read_jsonc_object(config_source), schema)


def _profile_config_resource(domain: str, profile: ConfigProfile) -> str:
    suffix = "_dev" if profile == "development" else "_inline"
    return f"{domain}{suffix}.jsonc"


def install_user_configuration(
    *,
    config_root: Path,
    profile: ConfigProfile,
    project_root: Path | None = None,
    force: bool = False,
) -> ConfigurationInstallation:
    """从静态资源安装双配置；普通模式只补缺失配置。"""
    resolved_root = config_root.expanduser().resolve()
    sources: list[tuple[Path, Path, Path, Path]] = []
    for domain in CONFIG_DOMAINS:
        config_source = resolve_config_resource_source(
            _profile_config_resource(domain, profile),
            project_root=project_root,
        )
        schema_source = resolve_config_resource_source(
            f"{domain}_schema.jsonc",
            project_root=project_root,
        )
        validate_config_resource_pair(config_source, schema_source)
        sources.append(
            (
                config_source,
                schema_source,
                resolved_root / f"{domain}.jsonc",
                resolved_root / f"{domain}_schema.jsonc",
            )
        )

    config_paths: list[Path] = []
    schema_paths: list[Path] = []
    created_config_paths: list[Path] = []
    for config_source, schema_source, config_target, schema_target in sources:
        config_paths.append(config_target)
        schema_paths.append(schema_target)
        if force or not config_target.exists():
            atomic_write(config_target, config_source.read_bytes(), 0o600)
            created_config_paths.append(config_target)
        atomic_write(schema_target, schema_source.read_bytes(), 0o600)

    return ConfigurationInstallation(
        config_paths=tuple(config_paths),
        schema_paths=tuple(schema_paths),
        created_config_paths=tuple(created_config_paths),
    )


def install_source_development_configuration(
    *,
    project_root: Path,
    config_root: Path,
) -> ConfigurationInstallation:
    """把源码开发资源安装到 BOXTEAM_HOME，运行期不再读取源码文件。"""
    resolved_project_root = project_root.expanduser().resolve()
    source_env_path = resolved_project_root / ".env"
    if not source_env_path.is_file():
        raise FileNotFoundError(f"源码环境配置不存在: {source_env_path}")
    installation = install_user_configuration(
        config_root=config_root,
        profile="development",
        project_root=resolved_project_root,
        force=True,
    )
    env_path = config_root.expanduser().resolve() / ".env"
    atomic_write(env_path, source_env_path.read_bytes(), 0o600)
    return ConfigurationInstallation(
        config_paths=installation.config_paths,
        schema_paths=installation.schema_paths,
        created_config_paths=installation.created_config_paths,
        env_path=env_path,
    )
