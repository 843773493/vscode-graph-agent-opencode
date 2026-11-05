"""只依赖标准库和测试 wheel 的真实 stdio MCP 制品，回传实际环境事实。"""

import json
import sys
from importlib import import_module

locked_leaf = import_module("locked_leaf")


def main() -> None:
    for line in sys.stdin:
        request = json.loads(line)
        if "id" not in request:
            continue
        method = request["method"]
        if method == "initialize":
            result = {"protocolVersion": request["params"]["protocolVersion"], "capabilities": {"tools": {}},
                "serverInfo": {"name": "locked-artifact", "version": "1.0.0"}}
        elif method == "tools/list":
            result = {"tools": [{"name": "installation_probe", "description": "返回已安装制品的真实路径和锁定依赖",
                "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}}]}
        elif method == "tools/call":
            if request["params"]["name"] != "installation_probe":
                raise ValueError("测试制品没有此工具")
            result = {"content": [{"type": "text", "text": json.dumps({"prefix": sys.prefix,
                "executable": sys.executable, "dependency": locked_leaf.VALUE})}]}
        elif method == "ping":
            result = {}
        else:
            print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "error": {"code": -32601, "message": "unsupported test method"}}), flush=True)
            continue
        print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}), flush=True)


if __name__ == "__main__":
    main()
