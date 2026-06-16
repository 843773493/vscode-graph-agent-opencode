#!/usr/bin/env python3
"""解码 checkpoint JSONL 文件以便人工查看。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer


def _decode(serde: JsonPlusSerializer, record: object) -> object:
    if isinstance(record, dict) and "__serde_type__" in record:
        return serde.loads_typed((record["__serde_type__"], bytes.fromhex(record["__serde_payload__"])))
    return record


def _default_str(value: object) -> str:
    return str(value)


def main() -> None:
    if len(sys.argv) != 2:
        print(f"用法: {sys.argv[0]} <checkpoints.jsonl 路径>")
        sys.exit(1)

    path = Path(sys.argv[1])
    serde = JsonPlusSerializer()

    with path.open("r", encoding="utf-8") as f:
        for i, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            print(f"=== Record {i}: checkpoint_id={rec['checkpoint_id']} ===")
            print(
                "metadata:",
                json.dumps(_decode(serde, rec["metadata"]), ensure_ascii=False, default=_default_str, indent=2),
            )
            cp = _decode(serde, rec["checkpoint"])
            print("checkpoint keys:", list(cp.keys()))
            print("channel_versions:", cp.get("channel_versions"))
            print("updated_channels:", cp.get("updated_channels"))
            print("versions_seen:", cp.get("versions_seen"))
            print("ts:", cp.get("ts"))
            print("v:", cp.get("v"))
            print()


if __name__ == "__main__":
    main()
