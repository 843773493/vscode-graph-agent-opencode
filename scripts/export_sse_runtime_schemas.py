from __future__ import annotations

import json
from pathlib import Path

from app.schemas.internal_v2.sse import SSE_EVENT_MODELS


def main() -> None:
    project_root = Path.cwd()
    if not (project_root / "pyproject.toml").is_file():
        raise RuntimeError(f"必须从项目根目录运行: cwd={project_root}")
    schemas: dict[str, object] = {}
    for model in SSE_EVENT_MODELS.values():
        schema = model.model_json_schema(mode="serialization")
        schema["$id"] = f"urn:boxteam:sse:{model.__name__}"
        schemas[model.__name__] = schema
    payload = {"generated": True, "schemas": schemas}
    target = (
        project_root
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
