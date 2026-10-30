from __future__ import annotations

import argparse
import json
from pathlib import Path

from google.protobuf.descriptor_pb2 import FileDescriptorSet
from google.protobuf.descriptor_pool import DescriptorPool

from app.protocol.openapi import SSE_JSON_DESCRIPTORS
from app.protocol.schema import assert_public_schema
from app.schemas.internal_v2.sse import SSE_EVENT_MODELS


def main() -> None:
    parser = argparse.ArgumentParser(description="生成 SSE 校验 schema，并由 proto 核对同形公开结构")
    parser.add_argument("--output-root", type=Path, default=Path.cwd())
    parser.add_argument("--descriptor-set", type=Path)
    args = parser.parse_args()
    project_root = Path.cwd()
    if not (project_root / "pyproject.toml").is_file():
        raise RuntimeError(f"必须从项目根目录运行: cwd={project_root}")
    pool = None
    if args.descriptor_set:
        pool = DescriptorPool()
        bundle = FileDescriptorSet.FromString(args.descriptor_set.read_bytes())
        # Buf build 输出按依赖顺序排列；缺少依赖时由 protobuf 明确拒绝。
        for file in bundle.file:
            pool.Add(file)
    schemas: dict[str, object] = {}
    for model in SSE_EVENT_MODELS.values():
        schema = model.model_json_schema(mode="serialization")
        descriptor = SSE_JSON_DESCRIPTORS.get(model.__name__)
        if descriptor is not None:
            if pool is not None:
                descriptor = pool.FindMessageTypeByName(descriptor.full_name)
            assert_public_schema(schema, descriptor)
            schema["x-protobuf-message"] = descriptor.full_name
        schema["$id"] = f"urn:boxteam:sse:{model.__name__}"
        schemas[model.__name__] = schema
    payload = {"generated": True, "schemas": schemas}
    target = (
        args.output_root
        / "src"
        / "clients"
        / "web"
        / "src"
        / "types"
        / "protocol_generated"
        / "sse_runtime_schemas.json"
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
