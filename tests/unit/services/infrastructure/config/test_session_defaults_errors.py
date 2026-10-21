"""会话默认设置的原生 I/O 与损坏值留在原因链，公开错误只定位逻辑字段。"""

from __future__ import annotations

import errno
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services.infrastructure.config.session_defaults import SessionDefaults


@pytest.fixture
def defaults_case(tmp_path: Path):
    def validate_agent(value):
        if value not in {"default", "reviewer"}:
            raise ValueError(f"unknown agent: {value}")
        return value

    def resolve_provider(agent, value=None):
        validate_agent(agent)
        if value is None:
            return "primary"
        if value not in {"primary", "backup"}:
            raise ValueError(f"unknown provider: {value}")
        return value

    service = SessionDefaults(
        workspace_root=tmp_path, validate_agent_id=validate_agent,
        resolve_agent_provider_id=resolve_provider, default_agent_id_provider=lambda: "default",
    )
    service.set_workspace_default_agent("reviewer")
    service.set_workspace_default_provider("reviewer", "backup")
    path = service.workspace_session_defaults_path()
    return SimpleNamespace(service=service, path=path, root=tmp_path, original=path.read_bytes())


@pytest.mark.parametrize("stage", ["stat", "read", "mkdir", "write", "replace"])
@pytest.mark.parametrize("error_number", [errno.EACCES, errno.EIO])
def test_session_defaults_io_errors_preserve_type_errno_cause_and_original(
    defaults_case, monkeypatch: pytest.MonkeyPatch, stage: str, error_number: int,
):
    case = defaults_case
    method = {"stat": "exists", "read": "read_text", "mkdir": "mkdir", "write": "write_text", "replace": "replace"}[stage]
    temporary = case.path.with_suffix(".json.tmp")
    target = case.path.parent if stage == "mkdir" else (temporary if stage in {"write", "replace"} else case.path)
    actual = getattr(Path, method)
    fault = OSError(error_number, f"private-input.txt {case.root}", str(target), None, str(target))

    def fail(path, *args, **kwargs):
        if path == target:
            raise fault
        return actual(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, method, fail)
        with pytest.raises(type(fault)) as caught:
            if stage in {"stat", "read"}:
                case.service.get_workspace_default_agent_id()
            else:
                case.service.set_workspace_default_agent("default")
    assert caught.value.__cause__ is fault and caught.value.errno == error_number
    assert caught.value.filename is None and caught.value.filename2 is None
    message = str(caught.value)
    assert "artifact=session_defaults" in message
    assert f"stage={'read' if stage in {'stat', 'read'} else 'write'}" in message
    assert str(case.root) not in message and "private-input.txt" not in message
    assert case.path.read_bytes() == case.original
    # 未提交的临时正文沿用原策略，读路径不得将其当成当前默认值。
    assert temporary.exists() == (stage == "replace")
    assert case.service.get_workspace_default_agent_id() == "reviewer"
    case.service.set_workspace_default_agent("default")
    assert case.service.get_workspace_default_agent_id() == "default"
    assert not temporary.exists()


@pytest.mark.parametrize("failure", [
    "shape", "version", "json", "utf8", "agent_type", "agent_unknown",
    "provider_map", "provider_type", "provider_unknown",
])
def test_session_defaults_invalid_data_is_not_echoed_or_rewritten(defaults_case, failure):
    case = defaults_case
    private_input = str(case.root / "private-input.txt")
    payload = json.loads(case.original)
    if failure == "shape":
        payload = []
    elif failure == "version":
        payload["schema_version"] = private_input
    elif failure.startswith("agent"):
        payload["default_agent_id"] = {private_input: True} if failure == "agent_type" else private_input
    elif failure == "provider_map":
        payload["provider_by_agent"] = private_input
    elif failure.startswith("provider"):
        payload["provider_by_agent"]["reviewer"] = {private_input: True} if failure == "provider_type" else private_input
    corrupted = json.dumps(payload).encode()
    if failure == "json":
        corrupted = ('{\n "' + private_input + '":').encode()
    elif failure == "utf8":
        corrupted = b"\xff" + private_input.encode()
    case.path.write_bytes(corrupted)
    expected = {
        "shape": TypeError, "version": ValueError, "json": json.JSONDecodeError,
        "utf8": UnicodeDecodeError, "agent_type": TypeError, "agent_unknown": ValueError,
        "provider_map": TypeError, "provider_type": TypeError, "provider_unknown": ValueError,
    }[failure]
    with pytest.raises(expected) as caught:
        if failure.startswith("provider"):
            case.service.get_workspace_default_provider_id("reviewer")
        else:
            case.service.get_workspace_default_agent_id()
    message = str(caught.value)
    assert type(caught.value) is expected
    assert "artifact=session_defaults" in message
    assert str(case.root) not in message and "private-input.txt" not in message
    if failure == "json":
        assert isinstance(caught.value, json.JSONDecodeError)
        assert isinstance(caught.value.__cause__, json.JSONDecodeError)
        assert (caught.value.lineno, caught.value.colno, caught.value.pos) == (
            caught.value.__cause__.lineno, caught.value.__cause__.colno, caught.value.__cause__.pos,
        )
    elif failure == "utf8":
        assert isinstance(caught.value, UnicodeDecodeError)
        assert isinstance(caught.value.__cause__, UnicodeDecodeError)
        assert (caught.value.start, caught.value.end) == (0, 1)
    elif failure.endswith("unknown"):
        field = "default_agent_id" if failure == "agent_unknown" else "provider_by_agent"
        assert f"field={field}" in message
        assert type(caught.value.__cause__) is ValueError
        assert private_input in str(caught.value.__cause__)
    else:
        rule = {
            "shape": "必须是对象", "version": "版本非法", "agent_type": "Agent 必须是非空字符串",
            "provider_map": "provider 映射必须是对象", "provider_type": "provider 必须是非空字符串",
        }[failure]
        assert rule in message
    assert case.path.read_bytes() == corrupted
    assert not case.path.with_suffix(".json.tmp").exists()
    case.path.write_bytes(case.original)
    assert case.service.get_workspace_default_agent_id() == "reviewer"
    assert case.service.get_workspace_default_provider_id("reviewer") == "backup"


def test_session_defaults_missing_file_uses_static_defaults_without_creating_file(defaults_case):
    case = defaults_case
    case.path.unlink()
    assert case.service.get_workspace_default_agent_id() == "default"
    assert case.service.get_workspace_default_provider_id("reviewer") == "primary"
    assert not case.path.exists()
