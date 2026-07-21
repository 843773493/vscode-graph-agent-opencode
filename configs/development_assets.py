from __future__ import annotations

import os
from pathlib import Path

from configs.installer import atomic_write


SSH_KEY_NAME = "boxteam_gateway_e2e_ed25519"
SSH_KNOWN_HOSTS_NAME = "boxteam_gateway_e2e_known_hosts"
SSH_HOST_ALIAS = "boxteam-gateway-e2e"
SSH_BLOCK_BEGIN = "# BEGIN BOXTEAM GATEWAY E2E"
SSH_BLOCK_END = "# END BOXTEAM GATEWAY E2E"
DOCKER_TARGET_KNOWN_HOSTS = Path(
    "out/cross-platform-dev-targets/docker-debian/ssh/known_hosts"
)


def _replace_managed_ssh_block(existing: str, block: str) -> str:
    begin_count = existing.count(SSH_BLOCK_BEGIN)
    end_count = existing.count(SSH_BLOCK_END)
    if begin_count != end_count or begin_count > 1:
        raise RuntimeError(
            "~/.ssh/config 中的 BoxTeam 托管块损坏或重复，"
            f"begin={begin_count} end={end_count}"
        )
    if begin_count == 1:
        start = existing.index(SSH_BLOCK_BEGIN)
        end = existing.index(SSH_BLOCK_END, start) + len(SSH_BLOCK_END)
        updated = existing[:start] + block + existing[end:]
    else:
        separator = "" if not existing else ("\n" if existing.endswith("\n") else "\n\n")
        updated = existing + separator + block
    return updated.rstrip("\n") + "\n"


def install_development_ssh_assets(*, project_root: Path, home: Path) -> None:
    source_root = project_root / "asset" / "gateway_ssh"
    private_source = source_root / SSH_KEY_NAME
    public_source = source_root / f"{SSH_KEY_NAME}.pub"
    if not private_source.is_file() or not public_source.is_file():
        raise FileNotFoundError(f"Gateway E2E SSH 密钥不完整: {source_root}")

    ssh_root = home.expanduser().resolve() / ".ssh"
    ssh_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    ssh_root.chmod(0o700)
    private_target = ssh_root / SSH_KEY_NAME
    public_target = ssh_root / f"{SSH_KEY_NAME}.pub"
    atomic_write(private_target, private_source.read_bytes(), 0o600)
    atomic_write(public_target, public_source.read_bytes(), 0o644)
    # Docker 开发目标重建时由 provisioner 校验并更新专用 known-hosts。
    # 这里只同步 BoxTeam 自有文件，不触碰用户通用 ~/.ssh/known_hosts。
    target_known_hosts = project_root / DOCKER_TARGET_KNOWN_HOSTS
    known_hosts_contents = (
        target_known_hosts.read_bytes() if target_known_hosts.is_file() else b""
    )
    atomic_write(ssh_root / SSH_KNOWN_HOSTS_NAME, known_hosts_contents, 0o600)

    config_path = ssh_root / "config"
    if config_path.is_symlink():
        raise RuntimeError(f"拒绝修改符号链接 SSH config: {config_path}")
    existing = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
    ssh_user = os.environ.get("BOXTEAM_GATEWAY_E2E_SSH_USER", "boxteam").strip()
    if not ssh_user:
        raise ValueError("BOXTEAM_GATEWAY_E2E_SSH_USER 不能为空")
    block = "\n".join(
        (
            SSH_BLOCK_BEGIN,
            f"Host {SSH_HOST_ALIAS}",
            "  HostName 127.0.0.1",
            "  Port 22222",
            f"  User {ssh_user}",
            f"  IdentityFile ~/.ssh/{SSH_KEY_NAME}",
            "  IdentitiesOnly yes",
            f"  UserKnownHostsFile ~/.ssh/{SSH_KNOWN_HOSTS_NAME}",
            "  StrictHostKeyChecking yes",
            SSH_BLOCK_END,
        )
    )
    updated = _replace_managed_ssh_block(existing, block)
    if updated != existing:
        atomic_write(config_path, updated.encode("utf-8"), 0o600)
    else:
        config_path.chmod(0o600)
