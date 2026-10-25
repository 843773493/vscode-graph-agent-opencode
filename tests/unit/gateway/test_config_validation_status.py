from __future__ import annotations

import errno
import json
from dataclasses import asdict
from pathlib import Path

import httpx
import jsonschema
import pytest
from fastapi import FastAPI

from app.core.trace_middleware import TraceMiddleware
from app.gateway.auth import verify_gateway_token
from app.gateway.config import GatewayConfigReloadService, load_gateway_config
from app.gateway.control.gateway_state import GatewayStateStore
from app.gateway.routes.health_config import router
from app.services.infrastructure.config.state import ConfigConflictError
from configs.runtime import read_jsonc_object
from tests.harness.python.run_context import TestRunContext


@pytest.fixture
def validation_owner(tmp_path: Path):
    config_path = tmp_path / "gateway.jsonc"
    schema_path = tmp_path / "gateway_schema.jsonc"
    original = json.dumps({"config_version": 2, "workspaces": []})
    config_path.write_text(original, encoding="utf-8")
    schema_path.write_bytes(Path("configs/gateway_schema.jsonc").read_bytes())
    with GatewayStateStore(path=tmp_path / "gateway.sqlite") as store:
        config = load_gateway_config(
            config_path=config_path, schema_path=schema_path, state_store=store,
        )
        service = GatewayConfigReloadService(
            state_store=store, config=config, config_path=config_path,
            local_config_path=tmp_path / "gateway_local.jsonc", schema_path=schema_path,
        )
        service.initialize_active_snapshot()
        application = FastAPI()
        application.add_middleware(TraceMiddleware)
        application.include_router(router)
        application.state.gateway_config_reload = service
        application.dependency_overrides[verify_gateway_token] = lambda: "validation-test"
        yield application, service, store, config_path, schema_path, original


@pytest.mark.parametrize("source", ["config", "schema"])
@pytest.mark.parametrize("fields", ["filename", "filename2", "errno_only"])
async def test_native_source_message_does_not_enter_reload_status_or_persistent_state(
    validation_owner, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
    source: str, fields: str,
) -> None:
    application, service, store, config_path, schema_path, _ = validation_owner
    target = config_path if source == "config" else schema_path
    original_bytes = target.read_bytes()
    original_error = PermissionError(errno.EACCES, f"private-config-message: {target}")
    if fields == "filename":
        original_error.filename = str(target)
    elif fields == "filename2":
        original_error.filename2 = str(target)
    initial = service.config
    active = store.get_active_config_snapshot("gateway")
    source_keys = ("gateway_mutable_override", "gateway_local_mutable_override")

    def sources():
        # 既有秘密审计会更新时间；来源内容、归属和版本必须全部保持原值。
        return {key: {name: value for name, value in asdict(record).items() if name != "updated_at"}
                if (record := store.get_source_layer(key)) is not None else None for key in source_keys}

    before_sources = sources()
    read = Path.read_bytes

    def fail(path):
        if path == target:
            raise original_error
        return read(path)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "read_bytes", fail)
        with pytest.raises(PermissionError) as failure:
            await service.reload()
    assert failure.value.__cause__ is original_error
    assert failure.value.errno == errno.EACCES
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url="http://test") as client:
        failed = await client.get("/api/gateway/config/reload-status", headers={"X-Request-ID": "config-source-io"})
        assert failed.status_code == 200, failed.text
        body = failed.json()
        assert body["request_id"] == failed.headers["X-Request-ID"] == "config-source-io"
        status = body["data"]
        assert status["healthy"] is False and status["reason"] == "invalid_config"
        assert str(target) not in failed.text and "private-config-message" not in failed.text
        assert "operation=read" in status["last_error"] and "errno=13" in status["last_error"]
        assert "PermissionError" in status["last_error"]
        assert service.config is initial
        assert store.get_active_config_snapshot("gateway") == active
        assert store.get_pending_config_candidate(config_domain="gateway") is None
        assert sources() == before_sources and service.list_events() == ()
        assert target.read_bytes() == original_bytes
        assert any(record.exc_info and record.exc_info[1] is original_error for record in caplog.records)
        assert str(target) in caplog.text and "private-config-message" in caplog.text
        await service.reload()
        recovered = await client.get("/api/gateway/config/reload-status")
        assert recovered.status_code == 200, recovered.text
        assert recovered.json()["data"]["healthy"] is True
        assert recovered.json()["data"]["last_error"] is None
        assert service.config is initial and store.get_active_config_snapshot("gateway") == active
        assert sources() == before_sources and service.list_events() == ()
    store.close()
    with GatewayStateStore(path=store.path) as reopened:
        assert reopened.get_active_config_snapshot("gateway") == active
        assert reopened.get_pending_config_candidate(config_domain="gateway") is None
    context = TestRunContext.from_test_file(Path(__file__)).prepare()
    (context.artifacts_dir / "config-source-io.json").write_text(json.dumps({
        "failure": body, "recovered": recovered.json(),
    }, ensure_ascii=False, indent=2) + "\n")


@pytest.mark.parametrize("stage", ["apply", "promotion", "rollback"])
@pytest.mark.parametrize("locator", ["/private/runtime/gateway.jsonc", r"C:\private\gateway.jsonc", r"\\server\private\gateway.jsonc"])
async def test_runtime_failure_keeps_native_diagnostics_out_of_config_state(
    validation_owner, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
    stage: str, locator: str,
) -> None:
    application, service, store, config_path, _, original = validation_owner
    active = store.get_active_config_snapshot("gateway")
    initial = service.config
    apply_error = OSError(13, "runtime source denied", locator)
    promotion_error = ConfigConflictError(f"runtime promotion failed: {locator}")
    rollback_error = RuntimeError(f"runtime rollback failed: {locator}")
    calls: list[str] = []

    async def apply(candidate, previous, fencing_token, fence_check):
        assert previous is initial
        assert candidate.revision != initial.revision
        assert fencing_token
        fence_check()
        calls.append("apply")
        if stage == "apply":
            raise apply_error

        async def rollback() -> None:
            calls.append("rollback")
            if stage == "rollback":
                raise rollback_error

        return rollback

    def reject_promotion(*args, **kwargs):
        raise promotion_error

    monkeypatch.setattr(service, "_on_runtime_config", apply)
    monkeypatch.setattr(store, "promote_active_config_snapshot", reject_promotion)
    payload = json.loads(original)
    payload["ui"] = {"theme": {"default_theme_id": "blue"}}
    config_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises((OSError, RuntimeError)) as failure:
        await service.reload()

    pending = store.get_pending_config_candidate(config_domain="gateway")
    assert pending is not None and pending.last_apply_id is not None
    journal = store.get_config_apply_journal(apply_id=pending.last_apply_id)
    assert journal is not None
    expected_state = "conflict" if stage == "promotion" else "recovery_required"
    assert pending.state == expected_state
    assert journal.state == ("compensated" if stage == "promotion" else "recovery_required")
    assert calls == (["apply"] if stage == "apply" else ["apply", "rollback"])
    assert service.config is initial
    assert store.get_active_config_snapshot("gateway") == active
    assert store.get_config_apply_claim(config_domain="gateway") is None
    assert len(service.list_events()) == 1

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://test",
    ) as client:
        responses = [await client.get(path, headers={"X-Request-ID": "runtime-failure"}) for path in (
            "/api/gateway/config/reload-status", "/api/gateway/config/events",
        )]
    for response in responses:
        assert response.status_code == 200, response.text
        assert response.headers["X-Request-ID"] == response.json()["request_id"] == "runtime-failure"
    assert responses[0].json()["data"]["healthy"] is False
    assert responses[0].json()["data"]["state"] == expected_state
    public_facts = json.dumps({
        "status": responses[0].json(), "events": responses[1].json(),
        "pending_error": pending.last_error, "journal": asdict(journal),
        "raised_error": str(failure.value),
    }, ensure_ascii=False, default=str)
    assert "private" not in public_facts
    # 冷重开同一事实库，确认安全正文来自 writer，而非响应层临时过滤。
    store.close()
    with GatewayStateStore(path=store.path) as reopened:
        assert reopened.get_pending_config_candidate(config_domain="gateway") == pending
        assert reopened.get_config_apply_journal(apply_id=pending.last_apply_id) == journal
    original_error = apply_error if stage == "apply" else promotion_error
    assert failure.value.__cause__ is original_error
    assert any(record.exc_info and record.exc_info[1] is original_error for record in caplog.records)
    assert str(original_error) in caplog.text
    if stage == "rollback":
        assert any(record.exc_info and record.exc_info[1] is rollback_error for record in caplog.records)
    assert "stage=" in (pending.last_error or "")
    assert type(original_error).__name__ in (pending.last_error or "")
    if stage == "apply":
        assert "errno=13" in (pending.last_error or "")


@pytest.mark.parametrize(
    ("override", "location"),
    [
        ({"workspaces": "invalid"}, "workspaces"),
        ({"ui": {"theme": {"default_theme_id": 3}}}, "ui.theme.default_theme_id"),
    ],
)
async def test_invalid_reload_exposes_schema_reason_without_native_source_paths(
    validation_owner, override: dict[str, object], location: str,
) -> None:
    application, service, store, config_path, schema_path, original = validation_owner
    initial_config = service.config
    active = store.get_active_config_snapshot("gateway")
    payload = json.loads(original) | override
    config_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="配置验证失败") as failure:
        await service.reload()
    cause = failure.value.__cause__
    assert isinstance(cause, jsonschema.ValidationError)
    assert list(cause.absolute_path) == location.split(".")
    assert "stage=config" in str(failure.value)
    assert "rule=type" in str(failure.value)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://test",
    ) as client:
        response = await client.get(
            "/api/gateway/config/reload-status", headers={"X-Request-ID": "invalid-config"},
        )
        assert response.status_code == 200, response.text
        assert response.headers["X-Request-ID"] == response.json()["request_id"] == "invalid-config"
        status = response.json()["data"]
        assert status["healthy"] is False
        assert status["reason"] == "invalid_config"
        assert status["last_error"] == f"ValueError: {failure.value}"
        assert str(config_path) not in response.text
        assert str(schema_path) not in response.text
        assert str(config_path.parent) not in response.text
        assert service.config is initial_config
        assert store.get_active_config_snapshot("gateway") == active

        assert store.get_pending_config_candidate(config_domain="gateway") is None
        assert service.list_events() == ()
        # 修正源文件后走同一 reload，错误必须清除，原 active 不被坏候选覆盖。
        config_path.write_text(original, encoding="utf-8")
        await service.reload()
        recovered = await client.get("/api/gateway/config/reload-status")
        assert recovered.status_code == 200, recovered.text
        assert recovered.json()["data"]["healthy"] is True
        assert recovered.json()["data"]["last_error"] is None
        assert recovered.json()["data"]["reason"] is None
        assert store.get_active_config_snapshot("gateway") == active


@pytest.mark.parametrize("damage", ["value", "key", "schema", "policy", "ref_fragment", "ref_file"])
async def test_schema_failure_status_never_echoes_values_or_dynamic_keys(
    validation_owner, damage: str,
) -> None:
    application, service, store, config_path, schema_path, original = validation_owner
    schema_bytes = schema_path.read_bytes()
    initial_config = service.config
    active = store.get_active_config_snapshot("gateway")
    marker = str(config_path.parent / "secret-marker")
    payload = json.loads(original)
    if damage == "value":
        payload["workspaces"] = marker
    elif damage == "key":
        payload[marker] = "unexpected"
    else:
        schema = read_jsonc_object(schema_path)
        if damage == "schema":
            schema["type"] = marker
        elif damage.startswith("ref_"):
            schema["$ref"] = "#/$defs/secret-marker" if damage == "ref_fragment" else (config_path.parent / "secret-marker.json").as_uri()
        else:
            schema["x-boxteam-policy-manifest"] = [marker]
        schema_path.write_text(json.dumps(schema), encoding="utf-8")
    config_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="配置.*失败") as failure:
        await service.reload()
    assert failure.value.__cause__ is not None
    assert "secret-marker" in str(failure.value.__cause__)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://test",
    ) as client:
        response = await client.get("/api/gateway/config/reload-status")
        assert response.status_code == 200, response.text
        status = response.json()["data"]
        assert status["healthy"] is False
        assert status["reason"] == "invalid_config"
        assert status["last_error"] == f"ValueError: {failure.value}"
        assert "secret-marker" not in response.text
        assert str(config_path.parent) not in response.text
        assert service.config is initial_config
        assert store.get_active_config_snapshot("gateway") == active
        assert store.get_pending_config_candidate(config_domain="gateway") is None
        assert service.list_events() == ()
        schema_path.write_bytes(schema_bytes)
        config_path.write_text(original, encoding="utf-8")
        await service.reload()
        recovered = (await client.get("/api/gateway/config/reload-status")).json()["data"]
        assert recovered["healthy"] is True
        assert recovered["last_error"] is None
        assert store.get_active_config_snapshot("gateway") == active


@pytest.mark.parametrize(("damage", "exception", "reason"), [
    ("config_root", TypeError, "配置文件根节点必须是对象"),
    ("schema_root", TypeError, "配置文件根节点必须是对象"),
    ("schema_missing", FileNotFoundError, "配置文件不存在"),
    ("schema_directory", IsADirectoryError, "配置文件读取失败"),
])
async def test_source_read_failure_has_no_native_path_and_preserves_active_state(
    validation_owner, damage: str, exception: type[Exception], reason: str,
) -> None:
    application, service, store, config_path, schema_path, original = validation_owner
    initial_config = service.config
    active = store.get_active_config_snapshot("gateway")
    schema_bytes = schema_path.read_bytes()
    if damage == "config_root":
        config_path.write_text("[]", encoding="utf-8")
    elif damage == "schema_root":
        schema_path.write_text("[]", encoding="utf-8")
    else:
        schema_path.unlink()
        if damage == "schema_directory":
            schema_path.mkdir()

    with pytest.raises(exception, match=reason):
        await service.reload()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://test",
    ) as client:
        response = await client.get("/api/gateway/config/reload-status")
        assert response.status_code == 200, response.text
        status = response.json()["data"]
        assert status["healthy"] is False
        assert status["reason"] == "invalid_config"
        assert reason in status["last_error"]
        assert str(config_path.parent) not in response.text
        assert service.config is initial_config
        assert store.get_active_config_snapshot("gateway") == active
        assert store.get_pending_config_candidate(config_domain="gateway") is None
        assert service.list_events() == ()
        if damage == "schema_directory":
            schema_path.rmdir()
        schema_path.write_bytes(schema_bytes)
        config_path.write_text(original, encoding="utf-8")
        await service.reload()
        recovered = (await client.get("/api/gateway/config/reload-status")).json()["data"]
        assert recovered["healthy"] is True
        assert recovered["last_error"] is None
        assert store.get_active_config_snapshot("gateway") == active


@pytest.mark.parametrize("source", ["config", "schema"])
@pytest.mark.parametrize("damage", ["character", "token", "utf8"])
async def test_parse_failure_status_does_not_echo_document_or_advance_sources(
    validation_owner, source: str, damage: str,
) -> None:
    application, service, store, config_path, schema_path, _ = validation_owner
    target = config_path if source == "config" else schema_path
    original = target.read_bytes()
    initial_config = service.config
    active = store.get_active_config_snapshot("gateway")
    source_keys = ("gateway_mutable_override", "gateway_local_mutable_override")

    def source_state() -> dict[str, object]:
        # 既有秘密迁移检查会刷新审计时间；其余全部持久字段必须保持原值。
        return {
            key: {name: value for name, value in asdict(record).items() if name != "updated_at"}
            if (record := store.get_source_layer(key)) is not None else None
            for key in source_keys
        }

    sources = source_state()
    prefix = json.dumps({"private": "secret-marker", "native": str(target)})[:-1]
    suffix = {"character": b', "broken": @}', "token": b' "another"}', "utf8": b"\xff"}[damage]
    target.write_bytes(prefix.encode("utf-8") + suffix)

    with pytest.raises(ValueError, match="配置 .*失败") as failure:
        await service.reload()
    assert failure.value.__cause__ is not None
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://test",
    ) as client:
        response = await client.get(
            "/api/gateway/config/reload-status", headers={"X-Request-ID": "parse-failure"},
        )
        assert response.status_code == 200, response.text
        assert response.headers["X-Request-ID"] == response.json()["request_id"] == "parse-failure"
        status = response.json()["data"]
        assert status["healthy"] is False
        assert status["reason"] == "invalid_config"
        assert status["last_error"] == f"ValueError: {failure.value}"
        assert "secret-marker" not in response.text
        assert str(config_path.parent) not in response.text
        assert "another" not in response.text
        assert service.config is initial_config
        assert store.get_active_config_snapshot("gateway") == active
        assert store.get_pending_config_candidate(config_domain="gateway") is None
        assert source_state() == sources
        assert service.list_events() == ()

        target.write_bytes(original)
        await service.reload()
        recovered = (await client.get("/api/gateway/config/reload-status")).json()["data"]
        assert recovered["healthy"] is True
        assert recovered["last_error"] is None
        assert store.get_active_config_snapshot("gateway") == active
        assert source_state() == sources
