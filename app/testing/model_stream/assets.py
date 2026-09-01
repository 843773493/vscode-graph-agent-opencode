from __future__ import annotations

import copy
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

import commentjson

from .errors import ModelStreamAssetError

FrameKind = Literal["data", "done"]
FrameEncoding = Literal["json", "text"]
AssetSource = Literal["handwritten", "recorded"]
ProtocolId = Literal[
    "openai_chat_sse",
    "openai_responses_sse",
    "anthropic_messages_sse",
]
CassetteProtocol = ProtocolId | Literal["mixed"]

_SCENARIO_ID = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


@dataclass(frozen=True, slots=True)
class StreamFrame:
    kind: FrameKind
    encoding: FrameEncoding
    payload: object
    event: str | None = None

    def to_wire_bytes(self) -> bytes:
        if self.encoding == "json":
            data = json.dumps(
                self.payload,
                ensure_ascii=False,
                separators=(",", ":"),
            )
        else:
            if not isinstance(self.payload, str):
                raise ModelStreamAssetError(
                    "text data frame payload 必须是字符串"
                )
            data = self.payload
        lines = []
        if self.event is not None:
            lines.append(f"event: {self.event}")
        lines.extend(f"data: {line}" for line in data.split("\n"))
        return ("\n".join(lines) + "\n\n").encode()


@dataclass(frozen=True, slots=True)
class RequestSpec:
    method: str
    url: str
    match: dict[str, object]


@dataclass(frozen=True, slots=True)
class ResponseSpec:
    status: int
    headers: dict[str, str]
    frames: tuple[StreamFrame, ...]


@dataclass(frozen=True, slots=True)
class ReplaySpec:
    sequence_id: str
    step: int


@dataclass(frozen=True, slots=True)
class Interaction:
    request: RequestSpec
    response: ResponseSpec
    protocol: ProtocolId
    replay: ReplaySpec | None
    index: int


@dataclass(frozen=True, slots=True)
class ModelStreamCassette:
    schema_version: int
    kind: Literal["model_stream_cassette"]
    metadata: dict[str, object]
    interactions: tuple[Interaction, ...]
    path: Path | None
    raw: dict[str, object]


@dataclass(frozen=True, slots=True)
class StreamScenario:
    scenario_id: str
    asset_path: Path
    cassette: ModelStreamCassette
    business_assertion: str | None
    raw: dict[str, object]


def _object(value: object, *, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ModelStreamAssetError(f"{label} 必须是 JSON object")
    return value


def _required_str(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ModelStreamAssetError(f"{label} 必须是非空字符串")
    return value


def _frame(value: object, *, label: str) -> StreamFrame:
    raw = _object(value, label=label)
    raw_kind = _required_str(raw.get("kind"), label=f"{label}.kind")
    raw_encoding = _required_str(raw.get("encoding"), label=f"{label}.encoding")
    if raw_kind not in {"data", "done"}:
        raise ModelStreamAssetError(f"{label}.kind 不受支持: {raw_kind!r}")
    if raw_encoding not in {"json", "text"}:
        raise ModelStreamAssetError(
            f"{label}.encoding 不受支持: {raw_encoding!r}"
        )
    payload = raw.get("payload")
    if raw_encoding == "json" and not isinstance(payload, dict):
        raise ModelStreamAssetError(
            f"{label} 的 JSON frame payload 必须是 object"
        )
    elif raw_encoding == "text" and not isinstance(payload, str):
        raise ModelStreamAssetError(
            f"{label} 的 text frame payload 必须是字符串"
        )
    event = raw.get("event")
    if event is not None and (not isinstance(event, str) or not event):
        raise ModelStreamAssetError(
            f"{label}.event 必须是非空字符串或省略"
        )
    return StreamFrame(
        kind=cast(FrameKind, raw_kind),
        encoding=cast(FrameEncoding, raw_encoding),
        payload=copy.deepcopy(payload),
        event=event,
    )


def _interaction(
    value: object,
    *,
    index: int,
    default_protocol: ProtocolId | None,
) -> Interaction:
    label = f"interactions[{index}]"
    raw = _object(value, label=label)
    request = _object(raw.get("request"), label=f"{label}.request")
    method = _required_str(request.get("method"), label=f"{label}.request.method")
    url = _required_str(request.get("url"), label=f"{label}.request.url")
    match = _object(request.get("match"), label=f"{label}.request.match")
    response = _object(raw.get("response"), label=f"{label}.response")
    status = response.get("status")
    if not isinstance(status, int) or isinstance(status, bool) or not 100 <= status <= 599:
        raise ModelStreamAssetError(
            f"{label}.response.status 必须是 100 到 599 的整数"
        )
    raw_headers = _object(response.get("headers", {}), label=f"{label}.response.headers")
    headers: dict[str, str] = {}
    for header_name, header_value in raw_headers.items():
        if not isinstance(header_name, str) or not isinstance(header_value, str):
            raise ModelStreamAssetError(
                f"{label}.response.headers 必须是字符串到字符串的对象"
            )
        headers[header_name] = header_value
    raw_frames = response.get("frames")
    if not isinstance(raw_frames, list) or not raw_frames:
        raise ModelStreamAssetError(f"{label}.response.frames 必须是非空数组")
    frames = tuple(
        _frame(frame, label=f"{label}.response.frames[{frame_index}]")
        for frame_index, frame in enumerate(raw_frames)
    )
    done_indexes = [frame_index for frame_index, frame in enumerate(frames) if frame.kind == "done"]
    if done_indexes != [len(frames) - 1]:
        raise ModelStreamAssetError(
            f"{label}.response.frames 必须只在末尾包含一个 done frame"
        )

    replay: ReplaySpec | None = None
    raw_replay = raw.get("replay")
    if raw_replay is not None:
        replay_object = _object(raw_replay, label=f"{label}.replay")
        sequence_id = _required_str(
            replay_object.get("sequence_id"),
            label=f"{label}.replay.sequence_id",
        )
        step = replay_object.get("step")
        if not isinstance(step, int) or isinstance(step, bool) or step < 0:
            raise ModelStreamAssetError(
                f"{label}.replay.step 必须是非负整数"
            )
        replay = ReplaySpec(sequence_id=sequence_id, step=step)

    raw_protocol = raw.get("protocol", default_protocol)
    if not isinstance(raw_protocol, str) or not raw_protocol:
        raise ModelStreamAssetError(
            f"{label}.protocol 必须是非空 provider stream protocol"
        )
    from .protocols import get_protocol_codec

    protocol = get_protocol_codec(raw_protocol).protocol_id
    return Interaction(
        request=RequestSpec(method=method.upper(), url=url, match=copy.deepcopy(match)),
        response=ResponseSpec(status=status, headers=headers, frames=frames),
        protocol=protocol,
        replay=replay,
        index=index,
    )


def load_cassette(path: Path | str) -> ModelStreamCassette:
    resolved_path = Path(path).expanduser().resolve()
    if not resolved_path.is_file():
        raise FileNotFoundError(f"模型 stream asset 不存在: {resolved_path}")
    try:
        parsed: object = json.loads(resolved_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ModelStreamAssetError(
            f"模型 stream asset 不是合法 JSON: path={resolved_path}"
        ) from error
    raw = _object(parsed, label=str(resolved_path))
    schema_version = raw.get("schema_version")
    if schema_version != 1:
        raise ModelStreamAssetError(
            f"模型 stream asset schema_version 必须为 1: path={resolved_path}"
        )
    if raw.get("kind") != "model_stream_cassette":
        raise ModelStreamAssetError(
            f"模型 stream asset kind 无效: path={resolved_path} kind={raw.get('kind')!r}"
        )
    metadata = _object(raw.get("metadata"), label=f"{resolved_path}.metadata")
    source = metadata.get("source")
    if source not in {"handwritten", "recorded"}:
        raise ModelStreamAssetError(
            f"{resolved_path}.metadata.source 必须是 handwritten 或 recorded"
        )
    protocol = metadata.get("protocol")
    if not isinstance(protocol, str):
        raise ModelStreamAssetError(
            f"{resolved_path}.metadata.protocol 必须是非空字符串"
        )
    from .protocols import get_protocol_codec

    default_protocol = None if protocol == "mixed" else get_protocol_codec(protocol).protocol_id
    _required_str(metadata.get("asset_id"), label=f"{resolved_path}.metadata.asset_id")
    raw_interactions = raw.get("interactions")
    if not isinstance(raw_interactions, list) or not raw_interactions:
        raise ModelStreamAssetError(
            f"模型 stream asset interactions 必须是非空数组: path={resolved_path}"
        )
    interactions = tuple(
        _interaction(
            interaction,
            index=index,
            default_protocol=default_protocol,
        )
        for index, interaction in enumerate(raw_interactions)
    )
    _validate_sequence_steps(interactions, resolved_path)
    for interaction in interactions:
        get_protocol_codec(interaction.protocol).validate_stream(
            interaction.response.frames,
            label=(
                f"{resolved_path}.interactions[{interaction.index}]"
                f".response protocol={interaction.protocol!r}"
            ),
        )
    return ModelStreamCassette(
        schema_version=1,
        kind="model_stream_cassette",
        metadata=copy.deepcopy(metadata),
        interactions=interactions,
        path=resolved_path,
        raw=copy.deepcopy(raw),
    )


def _validate_sequence_steps(
    interactions: Sequence[Interaction],
    path: Path,
) -> None:
    grouped: dict[str, list[int]] = {}
    for interaction in interactions:
        if interaction.replay is None:
            continue
        grouped.setdefault(interaction.replay.sequence_id, []).append(
            interaction.replay.step
        )
    for sequence_id, steps in grouped.items():
        ordered = sorted(steps)
        expected = list(range(len(ordered)))
        if ordered != expected:
            raise ModelStreamAssetError(
                f"asset sequence steps 必须从 0 连续递增: path={path} "
                f"sequence_id={sequence_id!r} steps={ordered!r}"
            )


def _safe_relative_path(root: Path, raw_asset_path: str, *, label: str) -> Path:
    candidate = (root / Path(raw_asset_path)).resolve()
    if Path(raw_asset_path).is_absolute() or not candidate.is_relative_to(root):
        raise ModelStreamAssetError(f"{label} 不得跳出 fixture_root: {raw_asset_path!r}")
    return candidate


def load_scenario(fixture_root: Path | str, scenario_id: str) -> StreamScenario:
    root = Path(fixture_root).expanduser().resolve()
    if not _SCENARIO_ID.fullmatch(scenario_id):
        raise ModelStreamAssetError(f"scenario_id 不是 kebab-case: {scenario_id!r}")
    scenario_path = root / "scenarios" / f"{scenario_id}.jsonc"
    if not scenario_path.is_file():
        raise FileNotFoundError(f"scenario manifest 不存在: {scenario_path}")
    try:
        parsed: object = commentjson.loads(scenario_path.read_text(encoding="utf-8"))
    except ValueError as error:
        raise ModelStreamAssetError(
            f"scenario manifest 不是合法 JSONC: path={scenario_path}"
        ) from error
    raw = _object(parsed, label=str(scenario_path))
    if raw.get("scenario_id") != scenario_id:
        raise ModelStreamAssetError(
            f"scenario manifest id 不一致: path={scenario_path} expected={scenario_id!r}"
        )
    raw_asset = _required_str(raw.get("asset"), label=f"{scenario_path}.asset")
    asset_path = _safe_relative_path(root, raw_asset, label=f"{scenario_path}.asset")
    cassette = load_cassette(asset_path)
    business_assertion = raw.get("business_assertion")
    if business_assertion is not None and not isinstance(business_assertion, str):
        raise ModelStreamAssetError(
            f"{scenario_path}.business_assertion 必须是字符串"
        )
    for forbidden in ("mode", "transport", "replay_policy", "matching_policy"):
        if forbidden in raw:
            raise ModelStreamAssetError(
                f"scenario manifest 不得重复声明 transport 配置: path={scenario_path} field={forbidden}"
            )
    return StreamScenario(
        scenario_id=scenario_id,
        asset_path=asset_path,
        cassette=cassette,
        business_assertion=business_assertion,
        raw=copy.deepcopy(raw),
    )


def data_frame(
    payload: Mapping[str, object] | str,
    *,
    event: str | None = None,
) -> StreamFrame:
    if isinstance(payload, str):
        return StreamFrame(
            kind="data",
            encoding="text",
            payload=payload,
            event=event,
        )
    return StreamFrame(
        kind="data",
        encoding="json",
        payload=copy.deepcopy(dict(payload)),
        event=event,
    )


def done_frame(
    *,
    event: str | None = None,
    payload: object = "[DONE]",
    encoding: FrameEncoding = "text",
) -> StreamFrame:
    return StreamFrame(
        kind="done",
        encoding=encoding,
        payload=payload,
        event=event,
    )


def build_cassette(
    *,
    asset_id: str,
    url: str,
    model: str,
    frames: Sequence[StreamFrame],
    source: AssetSource = "handwritten",
    provider: str = "openai_compatible",
    protocol: ProtocolId = "openai_chat_sse",
    status: int = 200,
    headers: Mapping[str, str] | None = None,
    match: Mapping[str, object] | None = None,
    replay: ReplaySpec | None = None,
) -> ModelStreamCassette:
    if not _SCENARIO_ID.fullmatch(asset_id):
        raise ModelStreamAssetError(f"asset_id 不是 kebab-case: {asset_id!r}")
    response_headers = {
        "content-type": "text/event-stream",
        **dict(headers or {}),
    }
    interaction_raw: dict[str, object] = {
        "request": {
            "method": "POST",
            "url": url,
            "match": {"model": model, "stream": True, **dict(match or {})},
        },
        "response": {
            "status": status,
            "headers": response_headers,
            "frames": [
                {
                    "kind": frame.kind,
                    "encoding": frame.encoding,
                    "payload": copy.deepcopy(frame.payload),
                    **(
                        {"event": frame.event}
                        if frame.event is not None
                        else {}
                    ),
                }
                for frame in frames
            ],
        },
    }
    if replay is not None:
        interaction_raw["replay"] = {
            "sequence_id": replay.sequence_id,
            "step": replay.step,
        }
    raw: dict[str, object] = {
        "schema_version": 1,
        "kind": "model_stream_cassette",
        "metadata": {
            "source": source,
            "asset_id": asset_id,
            "protocol": protocol,
            "provider": provider,
            "model": model,
        },
        "interactions": [interaction_raw],
    }
    return load_cassette_from_object(raw)


def load_cassette_from_object(raw: dict[str, object]) -> ModelStreamCassette:
    """从测试 builder 生成的 object 加载 cassette，避免绕过同一套校验。"""
    if raw.get("schema_version") != 1 or raw.get("kind") != "model_stream_cassette":
        raise ModelStreamAssetError("内存 cassette 缺少合法 schema_version 或 kind")
    metadata = _object(raw.get("metadata"), label="cassette.metadata")
    if metadata.get("source") not in {"handwritten", "recorded"}:
        raise ModelStreamAssetError("内存 cassette metadata.source 无效")
    protocol = metadata.get("protocol")
    if not isinstance(protocol, str):
        raise ModelStreamAssetError("内存 cassette metadata.protocol 必须是字符串")
    from .protocols import get_protocol_codec

    default_protocol = None if protocol == "mixed" else get_protocol_codec(protocol).protocol_id
    _required_str(metadata.get("asset_id"), label="cassette.metadata.asset_id")
    interactions = raw.get("interactions")
    if not isinstance(interactions, list):
        raise ModelStreamAssetError("cassette.interactions 必须是数组")
    parsed_interactions = tuple(
        _interaction(
            interaction,
            index=index,
            default_protocol=default_protocol,
        )
        for index, interaction in enumerate(interactions)
    )
    _validate_sequence_steps(parsed_interactions, Path("<memory>"))
    for interaction in parsed_interactions:
        get_protocol_codec(interaction.protocol).validate_stream(
            interaction.response.frames,
            label=(
                f"cassette.interactions[{interaction.index}].response "
                f"protocol={interaction.protocol!r}"
            ),
        )
    return ModelStreamCassette(
        schema_version=1,
        kind="model_stream_cassette",
        metadata=copy.deepcopy(metadata),
        interactions=parsed_interactions,
        path=None,
        raw=copy.deepcopy(raw),
    )
