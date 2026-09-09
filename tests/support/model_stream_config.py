from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Literal, cast

from configs.runtime import read_jsonc_object

ModelStreamMode = Literal["off", "record", "replay"]
_MODEL_STREAM_MODES = frozenset({"off", "record", "replay"})


def _object(value: object, *, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise TypeError(f"{label} 必须是 JSON object")
    return value


def prepare_e2e_model_stream_config(
    source_path: Path | str,
    *,
    output_root: Path | str,
    mode: str | None = None,
) -> Path:
    """为单个 E2E 文件生成带运行时覆盖的 model stream 配置。

    原始 JSONC 只提供场景和 transport 基线；生成的配置属于当前测试文件，
    录制产物固定写入该文件的正式 artifacts 目录，不自动复制到长期 fixture。
    """

    source = Path(source_path).expanduser().resolve()
    config = read_jsonc_object(source)
    model_stream = _object(config.get("model_stream"), label="model_stream")
    transport = _object(
        model_stream.get("transport"),
        label="model_stream.transport",
    )

    if mode is not None:
        if mode not in _MODEL_STREAM_MODES:
            raise ValueError(
                f"E2E model stream mode 不受支持: {mode!r}; "
                f"可选值: {sorted(_MODEL_STREAM_MODES)}"
            )
        transport["mode"] = cast(ModelStreamMode, mode)

    recording = _object(
        transport.get("recording", {}),
        label="model_stream.transport.recording",
    )
    output = Path(output_root).expanduser().resolve()
    recording["artifact_root"] = str(output / "artifacts" / "model-stream")
    transport["recording"] = recording

    raw_schema = config.get("$schema")
    if not isinstance(raw_schema, str) or not raw_schema:
        raise ValueError(f"model stream 配置缺少有效 $schema: {source}")
    schema_source = (source.parent / raw_schema).resolve()
    if not schema_source.is_file():
        raise FileNotFoundError(
            f"model stream 配置 schema 不存在: config={source} schema={schema_source}"
        )

    runtime_root = output / "runtime" / "model-stream-config"
    runtime_root.mkdir(parents=True, exist_ok=True)
    runtime_schema = runtime_root / "model_stream_schema.jsonc"
    runtime_config = runtime_root / "model_stream.jsonc"
    shutil.copyfile(schema_source, runtime_schema)
    config["$schema"] = runtime_schema.name
    runtime_config.write_text(
        json.dumps(config, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return runtime_config
