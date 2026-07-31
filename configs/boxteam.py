from __future__ import annotations

from configs.cli import GATEWAY_DEVELOPMENT_ASSETS_ENV, main
from configs.gateway_development_assets import (
    SSH_BLOCK_BEGIN,
    SSH_BLOCK_END,
    SSH_HOST_ALIAS,
    SSH_KEY_NAME,
    SSH_KNOWN_HOSTS_NAME,
    install_gateway_development_assets,
)
from configs.installer import (
    ConfigurationInstallation,
    install_source_development_configuration,
    install_user_configuration,
)

__all__ = [
    "GATEWAY_DEVELOPMENT_ASSETS_ENV",
    "SSH_BLOCK_BEGIN",
    "SSH_BLOCK_END",
    "SSH_HOST_ALIAS",
    "SSH_KEY_NAME",
    "SSH_KNOWN_HOSTS_NAME",
    "ConfigurationInstallation",
    "install_gateway_development_assets",
    "install_source_development_configuration",
    "install_user_configuration",
    "main",
]


if __name__ == "__main__":
    main()
