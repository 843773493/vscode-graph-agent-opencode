from __future__ import annotations

import glob
import os
import shlex
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

_MAX_SSH_CONFIG_RESOLUTION_WORKERS = 8


@dataclass(frozen=True, slots=True)
class UserSshHost:
    alias: str
    hostname: str
    port: int
    username: str


def _split_config_line(raw_line: str) -> list[str]:
    lexer = shlex.shlex(raw_line, posix=True)
    lexer.whitespace_split = True
    lexer.commenters = "#"
    if os.name == "nt":
        # Windows Include 路径中的反斜杠是路径分隔符，不作为 shell 转义符消费。
        lexer.escape = ""
    return list(lexer)


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
        config_tokens = _split_config_line(raw_line)
        if not config_tokens:
            continue
        keyword = config_tokens[0].lower()
        if keyword == "include":
            for pattern in config_tokens[1:]:
                for included_path in _included_config_paths(pattern):
                    aliases.extend(_collect_host_aliases(included_path, visited))
            continue
        if keyword != "host":
            continue
        for alias in config_tokens[1:]:
            if alias.startswith("!") or any(character in alias for character in "*?"):
                continue
            aliases.append(alias)
    return aliases


def _effective_ssh_options(alias: str) -> dict[str, list[str]]:
    ssh_config_result = subprocess.run(
        ["ssh", "-G", alias],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if ssh_config_result.returncode != 0:
        detail = (
            ssh_config_result.stderr.strip()
            or ssh_config_result.stdout.strip()
            or "ssh -G 无输出"
        )
        raise RuntimeError(f"解析 SSH Host {alias} 失败: {detail}")
    options_by_name: dict[str, list[str]] = {}
    for option_line in ssh_config_result.stdout.splitlines():
        option_name, separator, option_value = option_line.partition(" ")
        if separator and option_value:
            options_by_name.setdefault(option_name.lower(), []).append(
                option_value.strip()
            )
    return options_by_name


def _resolve_effective_host(alias: str) -> UserSshHost:
    ssh_options = _effective_ssh_options(alias)
    hostname = (ssh_options.get("hostname") or [alias])[0]
    username_values = ssh_options.get("user")
    if not username_values or not username_values[0]:
        raise ValueError(f"SSH Host {alias} 无法解析 User")
    port_text = (ssh_options.get("port") or ["22"])[0]
    try:
        port = int(port_text)
    except ValueError as error:
        raise ValueError(f"SSH Host {alias} 的 Port 不是整数: {port_text}") from error
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
    """按用户配置顺序解析 Host，并有界并行读取 OpenSSH 生效配置。"""
    aliases = _collect_host_aliases(
        config_path or Path.home() / ".ssh" / "config",
        set(),
    )
    unique_aliases = list(dict.fromkeys(aliases))
    if len(unique_aliases) < 2:
        return [_resolve_effective_host(alias) for alias in unique_aliases]
    with ThreadPoolExecutor(
        max_workers=min(_MAX_SSH_CONFIG_RESOLUTION_WORKERS, len(unique_aliases))
    ) as executor:
        return list(executor.map(_resolve_effective_host, unique_aliases))
