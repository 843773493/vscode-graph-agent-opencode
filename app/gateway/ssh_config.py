from __future__ import annotations

import glob
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class UserSshHost:
    alias: str
    hostname: str
    port: int
    username: str


def _included_config_paths(raw_pattern: str) -> list[Path]:
    expanded = Path(raw_pattern).expanduser()
    if not expanded.is_absolute():
        expanded = Path.home() / ".ssh" / expanded
    return [Path(path).resolve() for path in sorted(glob.glob(str(expanded)))]


def _collect_host_aliases(config_path: Path, visited: set[Path]) -> list[str]:
    resolved_path = config_path.expanduser().resolve()
    if resolved_path in visited or not resolved_path.is_file():
        return []
    visited.add(resolved_path)
    aliases: list[str] = []
    for raw_line in resolved_path.read_text(encoding="utf-8").splitlines():
        tokens = shlex.split(raw_line, comments=True)
        if not tokens:
            continue
        keyword = tokens[0].lower()
        if keyword == "include":
            for pattern in tokens[1:]:
                for included_path in _included_config_paths(pattern):
                    aliases.extend(_collect_host_aliases(included_path, visited))
            continue
        if keyword != "host":
            continue
        for alias in tokens[1:]:
            if alias.startswith("!") or any(character in alias for character in "*?"):
                continue
            aliases.append(alias)
    return aliases


def _effective_ssh_options(alias: str) -> dict[str, list[str]]:
    result = subprocess.run(
        ["ssh", "-G", alias],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "ssh -G 无输出"
        raise RuntimeError(f"解析 SSH Host {alias} 失败: {detail}")
    options: dict[str, list[str]] = {}
    for line in result.stdout.splitlines():
        key, separator, value = line.partition(" ")
        if separator and value:
            options.setdefault(key.lower(), []).append(value.strip())
    return options


def _resolve_effective_host(alias: str) -> UserSshHost:
    options = _effective_ssh_options(alias)
    hostname = (options.get("hostname") or [alias])[0]
    username_values = options.get("user")
    if not username_values or not username_values[0]:
        raise ValueError(f"SSH Host {alias} 无法解析 User")
    port_text = (options.get("port") or ["22"])[0]
    try:
        port = int(port_text)
    except ValueError as error:
        raise ValueError(
            f"SSH Host {alias} 的 Port 不是整数: {port_text}"
        ) from error
    if port < 1 or port > 65535:
        raise ValueError(f"SSH Host {alias} 的 Port 超出范围: {port}")
    return UserSshHost(
        alias=alias,
        hostname=hostname,
        port=port,
        username=username_values[0],
    )


def resolve_user_ssh_host(
    alias: str,
    config_path: Path | None = None,
) -> UserSshHost:
    normalized_alias = alias.strip()
    if not normalized_alias:
        raise ValueError("SSH Host 别名不能为空")
    configured_aliases = set(
        _collect_host_aliases(
            config_path or Path.home() / ".ssh" / "config",
            set(),
        )
    )
    if normalized_alias not in configured_aliases:
        raise ValueError(f"用户 SSH config 中不存在 Host: {normalized_alias}")
    return _resolve_effective_host(normalized_alias)


def list_user_ssh_hosts(config_path: Path | None = None) -> list[UserSshHost]:
    aliases = _collect_host_aliases(
        config_path or Path.home() / ".ssh" / "config",
        set(),
    )
    unique_aliases = list(dict.fromkeys(aliases))
    return [_resolve_effective_host(alias) for alias in unique_aliases]
