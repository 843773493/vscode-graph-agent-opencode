"""Gateway 原启动失败回报的持久化与诊断边界。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import pytest

from app.gateway.config import GatewayConfigReloadService, load_gateway_config, sources
from app.gateway.control.gateway_state import GatewayStateStore
from app.gateway.runtime_proof import _record_owned_gateway_startup_failure


@dataclass(frozen=True)
class PendingGateway:
    service: GatewayConfigReloadService
    state: GatewayStateStore
    candidate_ref: str
    config_path: Path
    schema_path: Path


@pytest.fixture
async def pending_gateway(tmp_path: Path) -> AsyncIterator[PendingGateway]:
    config = tmp_path / "config"
    config.mkdir()
    path = config / "gateway.jsonc"
    path.write_text('{"config_version":1,"workspaces":[]}', encoding="utf-8")
    schema = config / "gateway_schema.jsonc"
    schema.write_bytes(Path("configs/gateway_schema.jsonc").read_bytes())
    state = GatewayStateStore(path=tmp_path / "state" / "gateway" / "gateway.sqlite")
    try:
        initial = load_gateway_config(config_path=path, schema_path=schema, state_store=state)
        service = GatewayConfigReloadService(
            state_store=state, config=initial, config_path=path,
            local_config_path=config / "gateway_local.jsonc", schema_path=schema,
            gateway_id="gateway_startup_test",
        )
        service.initialize_active_snapshot()
        path.write_text(json.dumps({"config_version": 1, "workspaces": [], "runtime": {
            "gateway": {"process": {"health": {"poll_interval_seconds": 1}}},
        }}), encoding="utf-8")
        await service.reload()
        candidate_ref = service.status().candidate_ref
        assert candidate_ref is not None
        yield PendingGateway(service, state, candidate_ref, path, schema)
    finally:
        state.close()


def _record_failure(case: PendingGateway, error: BaseException, *, entry: str,
                    monkeypatch: pytest.MonkeyPatch) -> None:
    intent = case.state.get_gateway_restart_intent(candidate_ref=case.candidate_ref)
    assert intent is not None and intent.gateway_id is not None
    if entry == "early":
        monkeypatch.setenv("BOXTEAM_CONFIG_GENERATION", intent.target_generation)
        monkeypatch.setenv("BOXTEAM_CONFIG_FENCING_TOKEN", intent.fencing_token)
        assert _record_owned_gateway_startup_failure(
            gateway_state=case.state, gateway_id=intent.gateway_id,
            candidate_ref=case.candidate_ref, error=error,
        )
    else:
        case.service.record_pending_restart_failure(
            candidate_ref=case.candidate_ref, target_generation=intent.target_generation,
            fencing_token=intent.fencing_token, error=error,
        )


def _restart_facts(state: GatewayStateStore) -> dict[str, list[tuple[object, ...]]]:
    # 文件重新观察会更新 source 的读取时间；候选与提交事实则必须逐字段保持。
    tables = (
        "config_active_snapshot", "config_pending_candidate", "config_apply_claim",
        "config_apply_journal", "config_events", "config_revision_meta",
        "gateway_restart_intent", "config_source_journal", "config_source_owner",
    )
    with closing(state.connection()) as connection:
        return {table: [tuple(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY rowid")]
                for table in tables}


@pytest.mark.parametrize("entry", ["early", "service"])
@pytest.mark.parametrize("phase", ["pending", "applying"])
@pytest.mark.parametrize("location", ["/private/pending/config", r"C:\private\pending\config", r"\\server\share\pending"])
async def test_startup_failure_publishes_facts_in_one_original_transaction(
    pending_gateway: PendingGateway, monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture, entry: str, phase: str, location: str,
) -> None:
    case = pending_gateway
    active = case.state.get_active_config_snapshot("gateway")
    if phase == "applying":
        case.service.begin_pending_restart(candidate_ref=case.candidate_ref)
    error = OSError(13, f"原生失败: {location}", location)
    error.__cause__ = RuntimeError(f"原始原因: {location}")
    _record_failure(case, error, entry=entry, monkeypatch=monkeypatch)
    intent = case.state.get_gateway_restart_intent(candidate_ref=case.candidate_ref)
    pending = case.state.get_pending_config_candidate(config_domain="gateway")
    assert intent is not None and pending is not None
    assert intent.state == pending.state == "recovery_required"
    assert intent.last_error == pending.last_error
    assert pending.last_error == "Gateway pending generation 启动失败: stage=startup, error=PermissionError, errno=13"
    assert case.state.get_active_config_snapshot("gateway") == active
    event = case.service.list_events()[-1]
    assert event.error == pending.last_error and event.result == "recovery_required"
    if phase == "applying":
        assert pending.last_apply_id is not None
        journal = case.state.get_config_apply_journal(apply_id=pending.last_apply_id)
        assert journal is not None and journal.state == "recovery_required"
        assert journal.last_error == pending.last_error
    assert case.service.status().last_error == pending.last_error
    assert any(record.exc_info is not None and record.exc_info[1] is error for record in caplog.records)
    assert error.__cause__ is not None and location in str(error.__cause__)
    # 显式重试仍消费同一候选；真正的冷启动由独立进程验收覆盖。
    retried = case.service.retry_pending_restart(candidate_ref=case.candidate_ref)
    assert retried.state == "pending_restart" and retried.candidate_ref == case.candidate_ref
    next_intent = case.state.get_gateway_restart_intent(candidate_ref=case.candidate_ref)
    assert next_intent is not None and next_intent.fencing_token != intent.fencing_token


@pytest.mark.parametrize("error", [RuntimeError("/private/startup"), asyncio.CancelledError("/private/cancelled")])
async def test_startup_failure_settles_even_when_original_log_sink_fails(
    pending_gateway: PendingGateway, monkeypatch: pytest.MonkeyPatch, error: BaseException,
) -> None:
    def reject_log(*args: object, **kwargs: object) -> None:
        raise OSError(5, "日志原生位置 /private/gateway/log")
    monkeypatch.setattr(sources.logger, "error", reject_log)
    _record_failure(pending_gateway, error, entry="service", monkeypatch=monkeypatch)
    status = pending_gateway.service.status()
    assert status.state == "recovery_required"
    assert status.last_error is not None and "diagnostic_sink_error=OSError" in status.last_error
    assert "/private/" not in status.last_error
    assert any("诊断日志输出失败" in note for note in error.__notes__)


@pytest.mark.parametrize("mismatch", ["gateway", "generation", "token"])
async def test_unowned_early_failure_leaves_candidate_and_events_unchanged(
    pending_gateway: PendingGateway, monkeypatch: pytest.MonkeyPatch, mismatch: str,
) -> None:
    case = pending_gateway
    intent = case.state.get_gateway_restart_intent(candidate_ref=case.candidate_ref)
    assert intent is not None and intent.gateway_id is not None
    before = case.service.status()
    events = case.service.list_events()
    monkeypatch.setenv("BOXTEAM_CONFIG_GENERATION", "stale" if mismatch == "generation" else intent.target_generation)
    monkeypatch.setenv("BOXTEAM_CONFIG_FENCING_TOKEN", "stale" if mismatch == "token" else intent.fencing_token)
    assert not _record_owned_gateway_startup_failure(
        gateway_state=case.state, gateway_id="gateway_other" if mismatch == "gateway" else intent.gateway_id,
        candidate_ref=case.candidate_ref, error=RuntimeError("/private/stale"),
    )
    assert case.service.status() == before
    assert case.service.list_events() == events


async def test_startup_failure_rejects_preformatted_strings_without_publishing(
    pending_gateway: PendingGateway, monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = pending_gateway
    before = case.service.status()
    with pytest.raises(TypeError, match="必须提供原异常"):
        _record_failure(case, cast(BaseException, "/private/old-error-text"), entry="service", monkeypatch=monkeypatch)
    assert case.service.status() == before


@pytest.mark.parametrize("phase", ["pending_restart", "applying", "recovery_required", "discarded", "expired"])
@pytest.mark.parametrize("corrupt_payload", [False, True])
async def test_watcher_preserves_owned_restart_candidate_on_ordinary_recovery(
    pending_gateway: PendingGateway, phase: str, corrupt_payload: bool,
) -> None:
    case = pending_gateway
    intent = case.state.get_gateway_restart_intent(candidate_ref=case.candidate_ref)
    active = case.state.get_active_config_snapshot("gateway")
    assert intent is not None and active is not None
    if phase == "applying":
        case.service.begin_pending_restart(candidate_ref=case.candidate_ref)
    elif phase == "recovery_required":
        case.service.record_pending_restart_failure(
            candidate_ref=case.candidate_ref, target_generation=intent.target_generation,
            fencing_token=intent.fencing_token, error=RuntimeError("原候选启动失败"),
        )
    elif phase == "discarded":
        case.service.discard_pending_restart(
            candidate_ref=case.candidate_ref, expected_active_revision=active.active_revision,
            expected_active_digest=active.effective_digest,
        )
    # 故障只改变输入或时钟，不替产品生成候选、claim、失败事实和丢弃记录。
    with closing(case.state.connection()) as connection:
        if corrupt_payload:
            connection.execute(
                "UPDATE config_pending_candidate SET payload_json = ? WHERE candidate_id = ?",
                (json.dumps({"runtime": {"gateway": {"process": "invalid"}}}), intent.candidate_id),
            )
        if phase == "expired":
            connection.execute(
                "UPDATE gateway_restart_intent SET expires_at = ? WHERE candidate_ref = ?",
                ("1970-01-01T00:00:00+00:00", case.candidate_ref),
            )
        connection.commit()
    before = _restart_facts(case.state)

    config = load_gateway_config(
        config_path=case.config_path, schema_path=case.schema_path,
        state_store=case.state, startup=True,
    )
    assert config.revision == active.effective_digest
    recovered = GatewayConfigReloadService(
        state_store=case.state, config=config, config_path=case.config_path,
        local_config_path=case.config_path.with_name("gateway_local.jsonc"),
        schema_path=case.schema_path, gateway_id=intent.gateway_id,
    )
    try:
        # 真正启动 watcher，覆盖其订阅后的首次对账；随后重复 reload 仍不接纳旧候选。
        await recovered.start()
        await recovered.reload()
        await recovered.reload()
        status = recovered.status()
        assert status.state == ("pending_restart" if phase == "expired" else phase)
        assert status.active_revision == active.active_revision
        assert status.revision == active.effective_digest
        assert status.candidate_id == intent.candidate_id
        assert status.candidate_ref == (None if phase == "discarded" else case.candidate_ref)
        assert status.healthy is (phase not in {"recovery_required"})
        assert _restart_facts(case.state) == before
    finally:
        await recovered.stop()


async def test_changed_source_can_publish_new_candidate_after_restart_failure(
    pending_gateway: PendingGateway,
) -> None:
    case = pending_gateway
    intent = case.state.get_gateway_restart_intent(candidate_ref=case.candidate_ref)
    assert intent is not None
    case.service.record_pending_restart_failure(
        candidate_ref=case.candidate_ref, target_generation=intent.target_generation,
        fencing_token=intent.fencing_token, error=RuntimeError("原候选启动失败"),
    )
    failed = case.state.get_pending_config_candidate(config_domain="gateway", candidate_id=intent.candidate_id)
    failed_intent = case.state.get_gateway_restart_intent(candidate_ref=case.candidate_ref)
    document = json.loads(case.config_path.read_text(encoding="utf-8"))
    document["runtime"]["gateway"]["process"]["health"]["poll_interval_seconds"] = 2
    case.config_path.write_text(json.dumps(document), encoding="utf-8")
    await case.service.reload()
    status = case.service.status()
    assert status.state == "pending_restart"
    assert status.candidate_id != intent.candidate_id
    assert status.candidate_ref != case.candidate_ref
    assert case.state.get_pending_config_candidate(config_domain="gateway", candidate_id=intent.candidate_id) == failed
    assert case.state.get_gateway_restart_intent(candidate_ref=case.candidate_ref) == failed_intent
    assert [event.result for event in case.service.list_events()] == [
        "restart_required", "recovery_required", "restart_required",
    ]
