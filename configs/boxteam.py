from __future__ import annotations

from configs.cli import (
    DEVELOPMENT_ASSETS_ENV,
    GATEWAY_E2E_WORKSPACE_ENV,
    main,
)
from configs.defaults import build_boxteam_config
from configs.development_assets import (
    SSH_BLOCK_BEGIN,
    SSH_BLOCK_END,
    SSH_HOST_ALIAS,
    SSH_KEY_NAME,
    SSH_KNOWN_HOSTS_NAME,
    install_development_ssh_assets,
)
from configs.installer import (
    initialize_boxteam_config,
    install_config_schema,
    write_boxteam_config,
)

__all__ = [
    "DEVELOPMENT_ASSETS_ENV",
    "GATEWAY_E2E_WORKSPACE_ENV",
    "SSH_BLOCK_BEGIN",
    "SSH_BLOCK_END",
    "SSH_HOST_ALIAS",
    "SSH_KEY_NAME",
    "SSH_KNOWN_HOSTS_NAME",
    "build_boxteam_config",
    "initialize_boxteam_config",
    "install_config_schema",
    "install_development_ssh_assets",
    "main",
    "write_boxteam_config",
]


if __name__ == "__main__":
    main()
