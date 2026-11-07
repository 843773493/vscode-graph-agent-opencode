"""真实 stdio 测试制品只回传私有 env 摘要，并记录实际启动和调用。"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path


def main() -> None:
    digest = hashlib.sha256(os.environ["MCP_PRIVATE_TOKEN"].encode("utf-8")).hexdigest()
    receipts = Path(os.environ["MCP_ENV_RECEIPTS"])

    def record(kind: str, proof: str = "") -> None:
        with receipts.open("a", encoding="utf-8") as output:
            output.write(json.dumps({"kind": kind, "proof": proof, "secret_digest": digest}) + "\n")

    record("started")
    for line in sys.stdin:
        request = json.loads(line)
        if "id" not in request:
            continue
        method = request["method"]
        if method == "initialize":
            result = {"protocolVersion": request["params"]["protocolVersion"], "capabilities": {"tools": {}},
                "serverInfo": {"name": "private-env-artifact", "version": "1.0.0"}}
        elif method == "tools/list":
            result = {"tools": [{"name": "private_env_probe", "description": "验证实际进程获得私有 env，原值不进入回执",
                "inputSchema": {"type": "object", "properties": {"proof": {"type": "string"}, "include_inputs": {"type": "boolean"}},
                    "required": ["proof"], "additionalProperties": False}}]}
        elif method == "tools/call":
            if request["params"]["name"] != "private_env_probe":
                raise ValueError("测试制品没有此工具")
            proof = request["params"]["arguments"]["proof"]
            record("called", proof)
            observed: dict[str, object] = {"proof": proof, "secret_digest": digest, "prefix": sys.prefix}
            if request["params"]["arguments"].get("include_inputs"):
                observed.update({"arguments": sys.argv[1:], "environment": {key: os.environ.get(key)
                    for key in ("MCP_PLAIN_VALUE", "MCP_EMPTY_VALUE", "MCP_SKIPPED_VALUE")}})
            result = {"content": [{"type": "text", "text": json.dumps(observed)}]}
        elif method == "ping":
            result = {}
        else:
            print(json.dumps({"jsonrpc": "2.0", "id": request["id"],
                "error": {"code": -32601, "message": "unsupported test method"}}), flush=True)
            continue
        print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}), flush=True)


if __name__ == "__main__":
    main()
