from __future__ import annotations

import argparse
import json

from app.core.path_utils import get_gateway_root
from app.gateway.credentials import FederationCredentialStore


def issue_federation_token(
    *,
    connection_id: str,
    peer_gateway_id: str,
) -> dict[str, str]:
    credential = FederationCredentialStore(
        storage_path=get_gateway_root() / "credentials" / "federation.json"
    ).issue(
        connection_id=connection_id,
        peer_gateway_id=peer_gateway_id,
    )
    return {
        "connection_id": credential.connection_id,
        "peer_gateway_id": credential.peer_gateway_id,
        "token": credential.token,
        "expires_at": credential.expires_at.isoformat(),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--connection-id", required=True)
    parser.add_argument("--peer-gateway-id", required=True)
    parser.add_argument("--json", action="store_true")
    arguments = parser.parse_args()
    print(
        json.dumps(
            issue_federation_token(
                connection_id=arguments.connection_id,
                peer_gateway_id=arguments.peer_gateway_id,
            )
        )
    )


if __name__ == "__main__":
    main()
