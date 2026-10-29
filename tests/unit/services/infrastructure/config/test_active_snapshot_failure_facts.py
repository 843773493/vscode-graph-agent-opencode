"""两域 active 快照损坏时，恢复记录保留错误事实而不复制原生位置。"""

from __future__ import annotations

import json
from collections.abc import Callable
from contextlib import closing
from functools import partial
from pathlib import Path

import pytest

from app.gateway.control.gateway_state import GatewayStateStore
from app.services.infrastructure.workspace_state_store import WorkspaceStateStore
from configs.diagnostics import _read_sqlite_diagnostic

SnapshotStore = GatewayStateStore | WorkspaceStateStore


@pytest.fixture(params=["gateway", "workspace"])
def snapshot_store_factory(
    request: pytest.FixtureRequest, tmp_path: Path,
) -> tuple[str, Callable[[], SnapshotStore]]:
    if request.param == "gateway":
        return "gateway", partial(GatewayStateStore, path=tmp_path / "gateway.sqlite")
    return "workspace", partial(WorkspaceStateStore, workspace_root=tmp_path / "workspace")


def _row(store: SnapshotStore, domain: str) -> dict[str, object]:
    with closing(store.connection()) as connection:
        cursor = connection.execute(
            "SELECT * FROM config_active_snapshot WHERE config_domain = ?", (domain,),
        )
        row = cursor.fetchone()
        assert row is not None and cursor.description is not None
        return dict(zip((column[0] for column in cursor.description), row, strict=True))


def _replace_column(store: SnapshotStore, domain: str, column: str, value: object) -> None:
    assert column in {"layer_revisions_json", "promoted_at"}
    with closing(store.connection()) as connection:
        connection.execute(
            f"UPDATE config_active_snapshot SET {column} = ? WHERE config_domain = ?", (value, domain),
        )
        connection.commit()


def _initialize(store: SnapshotStore, domain: str) -> None:
    store.ensure_active_config_snapshot(
        config_domain=domain, payload={"config_version": 1}, source_baseline={},
        source_generation=3, layer_revisions={"user": 2}, layer_digests={"user": "source-digest"},
        effective_digest="active-digest", secret_bindings={}, schema_version=1,
        promoted_generation="generation-original",
    )


@pytest.mark.parametrize("native", ["/private/配置/active.jsonc", r"C:\私有\active.jsonc", r"\\server\私有\active.jsonc"])
@pytest.mark.parametrize("column", ["layer_revisions_json", "promoted_at"])
def test_snapshot_decode_failure_retains_original_record_and_cold_recovery_state(
    snapshot_store_factory: tuple[str, Callable[[], SnapshotStore]],
    caplog: pytest.LogCaptureFixture,
    native: str,
    column: str,
) -> None:
    domain, factory = snapshot_store_factory
    with factory() as store:
        _initialize(store, domain)
        original = _row(store, domain)
        corrupted = json.dumps({"user": native}) if column == "layer_revisions_json" else native
        _replace_column(store, domain, column, corrupted)
        damaged = _row(store, domain)

        with pytest.raises(ValueError) as captured:
            store.get_active_config_snapshot(domain)
        failure = captured.value
        failed = _row(store, domain)
        detail = str(failed["last_error"])
        assert native not in detail and str(failure) not in detail
        assert f"config_domain={domain}" in detail
        assert "stage=config-active-decode" in detail and "error=ValueError" in detail
        assert failed == damaged | {"state": "recovery_required", "last_error": detail}
        assert repr(native) in str(failure)
        assert any(record.exc_info and record.exc_info[1] is failure for record in caplog.records)
        assert str(failure) in caplog.text

    # 冷重开仍拒绝原损坏记录；不能扫描配置、重建 active 或分配新 revision。
    with factory() as store:
        with pytest.raises(ValueError):
            store.get_active_config_snapshot(domain)
        assert _row(store, domain) == failed
        _replace_column(store, domain, column, original[column])
        restored = store.get_active_config_snapshot(domain)
        assert restored is not None
        assert restored.state == "recovery_required" and restored.last_error == detail
        assert restored.active_revision == original["active_revision"]
        assert restored.promoted_generation == "generation-original"
        assert restored.effective_digest == "active-digest"
        _initialize(store, domain)
        assert _row(store, domain) == original | {"state": "recovery_required", "last_error": detail}

        # 离线诊断消费同一已提交恢复记录，不把它误报为 active 或回显失败原文。
        diagnostic = _read_sqlite_diagnostic(path=store.path)
        active = diagnostic["active"]
        assert isinstance(active, list) and len(active) == 1
        assert active[0]["state"] == "recovery_required"
        assert active[0]["last_error"] == detail
        assert native not in json.dumps(diagnostic, ensure_ascii=False)


def test_snapshot_diagnostic_sink_failure_cannot_prevent_recovery_record(
    snapshot_store_factory: tuple[str, Callable[[], SnapshotStore]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    domain, factory = snapshot_store_factory
    sink_error = OSError(5, "诊断写入失败", "/private/诊断/log.txt")

    def fail_log(*_args: object, **_kwargs: object) -> None:
        raise sink_error

    monkeypatch.setattr("app.core.failures.logger.error", fail_log)
    with factory() as store:
        _initialize(store, domain)
        native = "/private/配置/invalid-revision"
        _replace_column(store, domain, "layer_revisions_json", json.dumps({"user": native}))
        with pytest.raises(ValueError) as captured:
            store.get_active_config_snapshot(domain)
        detail = str(_row(store, domain)["last_error"])
        assert "diagnostic_sink_error=OSError" in detail
        assert native not in detail and str(sink_error) not in detail
        assert _row(store, domain)["state"] == "recovery_required"
        assert native in str(captured.value)
        assert any(str(sink_error) in note for note in captured.value.__notes__)
