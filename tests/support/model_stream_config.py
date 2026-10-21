from __future__ import annotations

import json
import shutil
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

from configs.runtime import read_jsonc_object
from tests.support.e2e_control.harness import E2ETestControlHarness

ModelStreamMode = Literal["off", "record", "replay"]
_MODEL_STREAM_MODES = frozenset({"off", "record", "replay"})


@dataclass(frozen=True, slots=True)
class ModelStreamReplay:
    control: E2ETestControlHarness
    config_path: Path
    artifacts: Path

    @property
    def environment(self) -> dict[str, str]:
        return {**self.control.environment, "BOXTEAM_TEST_MODEL_STREAM_CONFIG": str(self.config_path)}


@contextmanager
def model_stream_replay(
    output_root: Path, *, runtime_root: Path, business_assertion: str,
) -> Iterator[ModelStreamReplay]:
    """管理业务测试共用的精确回放资产、配置与控制端生命周期。"""
    fixture_root = runtime_root / "model-stream-fixture"
    (fixture_root / "scenarios").mkdir(parents=True, exist_ok=True)
    cassette = json.loads((Path.cwd() / "tests/fixtures/model_stream/handwritten/openai_chat/basic-text.json").read_text(encoding="utf-8"))
    # loader 要求非空资产；占位身份不能匹配产品 owner，不提供兜底响应。
    cassette["interactions"][0]["identity"] = {
        "session_id": "ses_00000000400070008000000000000000",
        "thread_id": "thr_00000000400070008000000000000000",
        "model_call_id": "00000000-4000-7000-8000-000000000000",
    }
    cassette_path = fixture_root / "calls.json"
    cassette_path.write_text(json.dumps(cassette, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (fixture_root / "scenarios/basic-chat-tool-loop.jsonc").write_text(json.dumps({
        "scenario_id": "basic-chat-tool-loop", "asset": "calls.json",
        "business_assertion": business_assertion,
    }) + "\n", encoding="utf-8")
    control = E2ETestControlHarness(runtime_root / "test-control")
    try:
        control.bind_cassette(cassette_path)
        config = prepare_e2e_model_stream_config(
            Path.cwd() / "configs/tests/model_stream/model_stream_web_basic_chat_tool_loop.jsonc",
            output_root=output_root, exact_fixture_root=fixture_root,
        )
        control.start()
        yield ModelStreamReplay(control, config, output_root / "artifacts/model-stream")
    finally:
        control.close()


def _object(value: object, *, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise TypeError(f"{label} 必须是 JSON object")
    return value


def prepare_e2e_model_stream_config(
    source_path: Path | str,
    *,
    output_root: Path | str,
    mode: str | None = None,
    exact_fixture_root: Path | None = None,
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
    if exact_fixture_root is not None:
        if mode not in (None, "replay"):
            raise ValueError("精确产品 E2E 只能使用 replay，禁止触发真实 Provider")
        transport["mode"] = "replay"
        transport["fixture_root"] = str(exact_fixture_root)
        transport["replay"] = {"policy": "call_identity"}

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
