"""只把请求派生的名称校验归为输入拒绝，内部配置及后续响应损坏仍抛原错。"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock

import pytest
from pydantic import ValidationError

from app.core import failures
from app.schemas.internal_v2.node_debug import (
    NodeDebugConfigurationCreateRequest,
    NodeDebugConfigurationDTO,
    NodeDebugConfigurationUpdateRequest,
)
from app.services.infrastructure.node_debug.configuration.configuration_factory import (
    NodeDebugConfigurationFactory,
)
from app.services.infrastructure.node_debug.configuration.configuration_registry import (
    NodeDebugConfigurationRegistry,
)
from app.services.infrastructure.node_debug.session.session_store import (
    NodeDebugSessionStore,
)
from tests.support.node_debug_dependencies import TEST_THREAD_ID, node_debug_file_paths

CONFIGURATION_ID = "dbgcfg_" + "1" * 32
SESSION_ID = "ses_00000000400070008000000000000001"
NATIVE_PATHS = ["/private/debug-secret", r"C:\x\debug-secret", r"\\target\x\debug-secret"]


@pytest.fixture
def configuration_factory(tmp_path: Path) -> NodeDebugConfigurationFactory:
    root = tmp_path / "workspace"
    root.mkdir()
    return NodeDebugConfigurationFactory(file_paths=node_debug_file_paths(root))


def _configuration(
    factory: NodeDebugConfigurationFactory, *, name: str = "入口调试",
    configuration_id: str = CONFIGURATION_ID,
) -> NodeDebugConfigurationDTO:
    return factory.configuration_from_request(
        configuration_id=configuration_id, name=name, script_path=None,
        working_directory="", launch_profile_name=None, args=[], breakpoints=[],
    )


@pytest.mark.parametrize("value", [*(native * 40 for native in NATIVE_PATHS), " "])
def test_deferred_name_rejection_is_safe_business_input(
    configuration_factory: NodeDebugConfigurationFactory, value: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    with pytest.raises(ValueError) as captured:
        _configuration(configuration_factory, name=value)
    failure = captured.value
    assert not isinstance(failure, ValidationError)
    assert "stage=node-debug-configuration-name, error=ValidationError" in str(failure)
    assert "debug-secret" not in str(failure) and "input_value" not in str(failure)
    assert isinstance(failure.__cause__, ValidationError)
    assert failure.__cause__.errors()[0]["input"] == value.strip()
    assert sum(bool(record.exc_info and record.exc_info[1] is failure.__cause__) for record in caplog.records) == 1
    assert _configuration(configuration_factory, name="  入口调试  ").name == "入口调试"


@pytest.mark.parametrize("native", NATIVE_PATHS)
def test_internal_identity_validation_stays_internal(
    configuration_factory: NodeDebugConfigurationFactory, native: str,
) -> None:
    with pytest.raises(ValidationError) as captured:
        _configuration(configuration_factory, configuration_id=native)
    assert captured.value.errors()[0]["input"] == native


@pytest.mark.parametrize("native", NATIVE_PATHS)
def test_downstream_validation_is_not_reclassified_as_naming_input(
    configuration_factory: NodeDebugConfigurationFactory, native: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ValidationError) as original:
        _configuration(configuration_factory, configuration_id=native)
    reject = Mock(side_effect=original.value)
    monkeypatch.setattr(configuration_factory, "validate_configuration", reject)
    with pytest.raises(ValidationError) as captured:
        _configuration(configuration_factory)
    assert captured.value is original.value
    reject.assert_called_once()


@pytest.mark.parametrize("entrypoint", ["start", "breakpoint"])
def test_implicit_name_rejection_precedes_publication_and_can_retry(
    configuration_factory: NodeDebugConfigurationFactory, tmp_path: Path, entrypoint: str,
) -> None:
    source = tmp_path / "workspace" / ("x" * 90 + ".mjs")
    source.write_text("export {};\n", encoding="utf-8")
    store = Mock(spec=NodeDebugSessionStore)
    store.list_configurations.return_value = []
    store.read_manifest.return_value = None
    store.write_configuration.side_effect = lambda session_id, configuration, thread_id: configuration
    registry = NodeDebugConfigurationRegistry(store=store, configuration_factory=configuration_factory)

    def select(path: str) -> None:
        if entrypoint == "start":
            registry.select_for_start(
                session_id=SESSION_ID, thread_id=TEST_THREAD_ID,
                configuration_id=None, path=path, working_directory=None,
                launch_profile_name=None, args=[],
            )
        else:
            registry.ensure_configuration_for_breakpoint(SESSION_ID, TEST_THREAD_ID, path=path)

    with pytest.raises(ValueError) as captured:
        select(source.name)
    assert not isinstance(captured.value, ValidationError)
    store.write_configuration.assert_not_called()
    assert registry.list(SESSION_ID, TEST_THREAD_ID) == []
    assert registry.active_id(SESSION_ID, TEST_THREAD_ID) is None
    source.rename(source.with_name("entry.mjs"))
    select("entry.mjs")
    store.write_configuration.assert_called_once()
    configurations = registry.list(SESSION_ID, TEST_THREAD_ID)
    assert len(configurations) == 1 and configurations[0].name == "调试 entry.mjs"
    assert registry.active_id(SESSION_ID, TEST_THREAD_ID) == configurations[0].configuration_id


@pytest.mark.parametrize("operation", ["create", "update"])
def test_whitespace_name_keeps_existing_business_rejection_without_write(
    configuration_factory: NodeDebugConfigurationFactory, operation: str,
) -> None:
    store = Mock(spec=NodeDebugSessionStore)
    store.list_configurations.return_value = []
    store.read_manifest.return_value = None
    store.write_configuration.side_effect = lambda session_id, configuration, thread_id: configuration
    registry = NodeDebugConfigurationRegistry(store=store, configuration_factory=configuration_factory)
    registry.ensure_loaded(SESSION_ID, TEST_THREAD_ID)
    if operation == "update":
        registry.put(SESSION_ID, _configuration(configuration_factory), TEST_THREAD_ID)
    before = registry.list(SESSION_ID, TEST_THREAD_ID)
    store.reset_mock()
    with pytest.raises(ValueError, match="^调试方案名称不能为空$") as captured:
        if operation == "create":
            registry.create(NodeDebugConfigurationCreateRequest(
                session_id=SESSION_ID, thread_id=TEST_THREAD_ID, name=" ",
            ))
        else:
            registry.update(CONFIGURATION_ID, NodeDebugConfigurationUpdateRequest(
                session_id=SESSION_ID, thread_id=TEST_THREAD_ID, name=" ",
            ))
    assert not isinstance(captured.value, ValidationError)
    store.write_configuration.assert_not_called()
    assert registry.list(SESSION_ID, TEST_THREAD_ID) == before


def test_diagnostic_sink_failure_does_not_replace_naming_rejection(
    configuration_factory: NodeDebugConfigurationFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_errors: list[ValidationError] = []
    sink_error = OSError(5, "日志不可写", "/private/debug-secret/service.log")

    def fail_log(*args: object, **kwargs: object) -> None:
        error = kwargs["exc_info"]
        assert isinstance(error, ValidationError)
        original_errors.append(error)
        raise sink_error

    monkeypatch.setattr(failures.logger, "error", fail_log)
    with pytest.raises(ValueError) as captured:
        _configuration(configuration_factory, name=NATIVE_PATHS[0] * 40)
    assert "diagnostic_sink_error=OSError" in str(captured.value)
    assert "debug-secret" not in str(captured.value)
    assert len(original_errors) == 1 and captured.value.__cause__ is original_errors[0]
    assert str(sink_error) in "\n".join(original_errors[0].__notes__)
