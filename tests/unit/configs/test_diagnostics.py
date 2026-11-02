"""离线配置诊断的资源引用、只读与失败边界。"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from app.core.distribution_identity import load_distribution_id
from app.core.resource_uri import parse_resource_uri
from app.core.workspace_identity import load_or_create_workspace_id
from app.gateway.control.gateway_state import GatewayStateStore
from configs import diagnostics
from configs.cli import main as config_cli_main


def _strings(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [
            text
            for key, child in value.items()
            for text in (*_strings(key), *_strings(child))
        ]
    if isinstance(value, list):
        return [text for child in value for text in _strings(child)]
    return []


@pytest.fixture
def diagnostic_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    boxteam_home = tmp_path / "boxteam-home"
    monkeypatch.setenv("BOXTEAM_HOME", str(boxteam_home))
    monkeypatch.delenv("BOXTEAM_GATEWAY_ROOT", raising=False)
    return boxteam_home


@pytest.fixture
def invoke_diagnostic(
    tmp_path: Path,
    diagnostic_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> Callable[[str], tuple[dict, str]]:
    def invoke(action: str = "doctor") -> tuple[dict, str]:
        monkeypatch.setattr(
            "sys.argv",
            [
                "boxteam",
                action,
                "--home",
                str(tmp_path),
                "--workspace",
                str(tmp_path / "workspace"),
            ],
        )
        config_cli_main()
        output = capsys.readouterr()
        return json.loads(output.out), output.err

    return invoke


@pytest.mark.parametrize("action", ["doctor", "diagnose"])
def test_cli_diagnostic_reports_actual_files_and_uris_read_only(
    tmp_path: Path,
    diagnostic_home: Path,
    invoke_diagnostic: Callable,
    action: str,
) -> None:
    config_root = diagnostic_home / "config"
    config_root.mkdir(parents=True)
    for domain in ("gateway", "workspace"):
        for suffix in ("", "_schema"):
            source = Path("configs") / f"{domain}{suffix or '_inline'}.jsonc"
            shutil.copyfile(source, config_root / f"{domain}{suffix}.jsonc")
        (config_root / f"{domain}_local.jsonc").write_text("{}", encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace_id = load_or_create_workspace_id(workspace)
    (workspace / ".boxteam" / "workspace.jsonc").write_text("{}", encoding="utf-8")
    state = GatewayStateStore(
        path=diagnostic_home / "state" / "gateway" / "gateway.sqlite"
    )
    state.sync_config_source(
        config_key="gateway_mutable_override",
        uri=None,
        config_version=1,
        presence="present",
        payload={"config_version": 1, "secret": "diagnostic-payload-must-not-escape"},
        layer_digest="a" * 64,
    )
    state.close()

    def fingerprint() -> dict[Path, tuple[bytes, int]]:
        return {
            path: (path.read_bytes(), path.stat().st_mtime_ns)
            for root in (diagnostic_home, workspace)
            for path in root.rglob("*")
            if path.is_file()
        }

    before = fingerprint()
    output, stderr = invoke_diagnostic(action)
    assert not stderr
    assert "config_root" not in output
    assert "root" not in output["workspace"]
    for domain in ("gateway", "workspace"):
        jsonc = output[domain]["jsonc"]
        assert jsonc["effective"]["valid"] is True
        assert (
            jsonc["schema_uri"]
            == f"boxteam://user/local/resources/config/{domain}_schema"
        )
        assert jsonc["schema_address_error"] is None
        expected = [
            ("inline", load_distribution_id(), f"{domain}_inline"),
            ("user", "local", domain),
            ("user", "local", f"{domain}_local"),
        ]
        if domain == "workspace":
            expected.append(("workspace", workspace_id, "workspace"))
        assert len(jsonc["sources"]) == len(expected)
        for source, (scope, scope_id, name) in zip(
            jsonc["sources"], expected, strict=True
        ):
            parsed = parse_resource_uri(source["uri"])
            assert (
                parsed.scope,
                parsed.scope_id,
                parsed.kind,
                parsed.tail_segments,
            ) == (scope, scope_id, "config", (name,))
            assert source["presence"] == "present"
            assert source["address_error"] is None
            expected_path = (
                Path.cwd() / "configs" / f"{domain}_inline.jsonc" if scope == "inline"
                else workspace / ".boxteam" / "workspace.jsonc" if scope == "workspace"
                else config_root / f"{name}.jsonc"
            )
            assert source["path"] == str(expected_path.resolve())
        assert jsonc["schema_path"] == str((config_root / f"{domain}_schema.jsonc").resolve())
        database = (diagnostic_home / "state" / "gateway" / "gateway.sqlite" if domain == "gateway"
                    else workspace / ".boxteam" / "state" / "workspace.sqlite")
        assert output[domain]["sqlite"]["path"] == str(database.resolve())
    assert output["gateway"]["sqlite"]["available"] is True
    assert output["action"] == "diagnose"
    assert output["read_only"] is True
    assert output["migration"]["executed"] is False
    assert output["migration"]["backup_created"] is False
    assert output["gateway"]["sqlite"]["sources"][0]["layer_digest"] == "a" * 64
    assert "diagnostic-payload-must-not-escape" not in json.dumps(output)
    assert "payload_json" not in json.dumps(output)
    assert output["workspace"]["sqlite"]["available"] is False
    assert output["workspace"]["source_owner"]["available"] is False
    after = fingerprint()
    assert {path: after[path] for path in before} == before
    # WAL 模式只读连接允许 SQLite 建立锁 sidecar；不能新增业务库或修改正文。
    added = set(after) - set(before)
    database = diagnostic_home / "state" / "gateway" / "gateway.sqlite"
    assert added <= {Path(f"{database}-wal"), Path(f"{database}-shm")}
    assert all(not after[path][0] for path in added if path.name.endswith("-wal"))
    assert output["workspace"]["source_owner"]["path"] == str((config_root / "workspace-source.sqlite").resolve())


@pytest.mark.parametrize("missing", [True, False])
def test_cli_diagnostic_reports_unavailable_identity_without_creating_or_replacing_it(
    tmp_path: Path,
    diagnostic_home: Path,
    invoke_diagnostic: Callable,
    monkeypatch: pytest.MonkeyPatch,
    missing: bool,
) -> None:
    workspace = tmp_path / "workspace"
    if missing:
        monkeypatch.delenv("BOXTEAM_RUNTIME_MANIFEST")
    else:
        manifest = tmp_path / "manifest.json"
        manifest.write_text(
            json.dumps({"distribution": str(tmp_path), "version": "1.0.0"}),
            encoding="utf-8",
        )
        monkeypatch.setenv("BOXTEAM_RUNTIME_MANIFEST", str(manifest))
        (workspace / ".boxteam").mkdir(parents=True)
        (workspace / ".boxteam" / "workspace-identity.json").write_text(
            json.dumps({"workspace_id": str(tmp_path)}), encoding="utf-8"
        )
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    output, stderr = invoke_diagnostic("doctor")
    for domain in ("gateway", "workspace"):
        jsonc = output[domain]["jsonc"]
        assert jsonc["sources"][0]["uri"] is None
        assert "stage=identity" in jsonc["sources"][0]["address_error"]
        assert jsonc["effective"]["valid"] is True
        assert output[domain]["sqlite"]["available"] is False
    assert output["workspace"]["jsonc"]["sources"][-1]["uri"] is None
    assert (
        "stage=identity" in output["workspace"]["jsonc"]["sources"][-1]["address_error"]
    )
    assert not diagnostic_home.exists()
    assert {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == before
    assert output["workspace"]["jsonc"]["sources"][-1]["path"] == str((workspace / ".boxteam/workspace.jsonc").resolve())
    assert "stage=diagnostic_log" in stderr
    assert ("error=FileNotFoundError" if missing else "error=ValueError") in stderr


@pytest.mark.parametrize(
    "location",
    [
        "/private/diagnostic/source",
        r"C:\private\diagnostic\source",
        r"\\server\share\diagnostic",
    ],
)
@pytest.mark.parametrize("failure", ["jsonc", "version", "schema", "sqlite"])
def test_cli_diagnostic_failures_do_not_echo_native_input_and_can_retry(
    diagnostic_home: Path,
    invoke_diagnostic: Callable,
    location: str,
    failure: str,
) -> None:
    config_root = diagnostic_home / "config"
    config_root.mkdir(parents=True)
    path = config_root / "gateway.jsonc"
    if failure == "jsonc":
        path.write_text('{"config_version": ' + location, encoding="utf-8")
    elif failure == "version":
        path.write_text(json.dumps({"config_version": location}), encoding="utf-8")
    elif failure == "schema":
        path = config_root / "gateway_schema.jsonc"
        path.write_text(json.dumps({"type": location}), encoding="utf-8")
    else:
        path = diagnostic_home / "state" / "gateway" / "gateway.sqlite"
        path.parent.mkdir(parents=True)
        path.write_bytes(location.encode() * 20)
    original = path.read_bytes()
    output, stderr = invoke_diagnostic("diagnose")
    target = (
        output["gateway"]["sqlite"]
        if failure == "sqlite"
        else output["gateway"]["jsonc"]["effective"]
    )
    assert "stage=" in target["error"]
    assert all(location not in value for value in _strings(output))
    assert location not in stderr
    assert path.read_bytes() == original
    if failure == "sqlite":
        path.unlink()
    elif failure == "schema":
        shutil.copyfile(Path("configs/gateway_schema.jsonc"), path)
    else:
        path.write_text("{}", encoding="utf-8")
    retried, _ = invoke_diagnostic("diagnose")
    assert retried["gateway"]["jsonc"]["effective"]["valid"] is True
    assert "error" not in retried["gateway"]["sqlite"]


@pytest.mark.parametrize("action", ["doctor", "diagnose"])
@pytest.mark.parametrize("initialized", [True, False])
def test_real_cli_process_reports_actual_locations_without_initializing_target(
    tmp_path: Path,
    diagnostic_home: Path,
    action: str,
    initialized: bool,
) -> None:
    environment = dict(os.environ)
    if initialized:
        load_or_create_workspace_id(tmp_path / "workspace")
    else:
        environment.pop("BOXTEAM_RUNTIME_MANIFEST", None)
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "configs.boxteam",
            action,
            "--home",
            str(tmp_path),
            "--workspace",
            str(tmp_path / "workspace"),
        ],
        env=environment,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    report = json.loads(completed.stdout)
    assert report["read_only"] is True
    assert report["gateway"]["jsonc"]["effective"]["valid"] is True
    assert report["gateway"]["jsonc"]["sources"][0]["path"] == str((Path.cwd() / "configs/gateway_inline.jsonc").resolve())
    assert report["workspace"]["jsonc"]["sources"][-1]["path"] == str((tmp_path / "workspace/.boxteam/workspace.jsonc").resolve())
    assert completed.stderr == (
        "" if initialized else "ERROR: logger=app.core.failures, stage=diagnostic_log, error=FileNotFoundError, errno=2\n"
    )
    assert not diagnostic_home.exists()


def test_diagnostic_source_metadata_and_effective_state_share_one_snapshot(
    diagnostic_home: Path,
    invoke_diagnostic: Callable,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_root = diagnostic_home / "config"
    config_root.mkdir(parents=True)
    source = config_root / "gateway.jsonc"
    source.write_text("{}", encoding="utf-8")
    original_read = diagnostics.read_stable_config_file
    snapshots = []

    def read_once(path: Path):
        snapshot = original_read(path)
        if path == source:
            snapshots.append(snapshot)
            source.write_text(
                '{"config_version":"/changed/after/snapshot"}', encoding="utf-8"
            )
        return snapshot

    monkeypatch.setattr(diagnostics, "read_stable_config_file", read_once)
    output, _ = invoke_diagnostic("doctor")
    assert len(snapshots) == 1
    jsonc = output["gateway"]["jsonc"]
    assert jsonc["sources"][1]["digest"] == hashlib.sha256(b"{}").hexdigest()
    assert jsonc["effective"]["valid"] is True
    retried, _ = invoke_diagnostic("doctor")
    assert retried["gateway"]["jsonc"]["effective"]["valid"] is False


def test_diagnostic_native_read_failure_keeps_errno_and_does_not_report_absence(
    diagnostic_home: Path, invoke_diagnostic: Callable, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = diagnostic_home / "config" / "gateway.jsonc"
    source.parent.mkdir(parents=True)
    source.write_text("{}", encoding="utf-8")
    original_read = Path.read_bytes

    def rejected_read(path: Path) -> bytes:
        if path == source:
            raise PermissionError(13, f"原始错误指向 {source}", str(source))
        return original_read(path)

    monkeypatch.setattr(Path, "read_bytes", rejected_read)
    output, stderr = invoke_diagnostic("doctor")
    failed = output["gateway"]["jsonc"]["sources"][1]
    assert failed["presence"] is None
    assert "stage=read, error=PermissionError, errno=13" in failed["parse_error"]
    assert failed["path"] == str(source.resolve())
    assert str(source) not in stderr
    assert original_read(source) == b"{}"
    monkeypatch.setattr(Path, "read_bytes", original_read)
    retried, _ = invoke_diagnostic("doctor")
    assert retried["gateway"]["jsonc"]["effective"]["valid"] is True


def test_diagnostic_reads_committed_wal_without_changing_persistent_bytes(
    diagnostic_home: Path,
    invoke_diagnostic: Callable,
) -> None:
    database = diagnostic_home / "state" / "gateway" / "gateway.sqlite"
    state = GatewayStateStore(path=database)
    reader = sqlite3.connect(database)
    try:
        reader.execute("SELECT COUNT(*) FROM config_source_layers").fetchone()
        state.sync_config_source(
            config_key="gateway_mutable_override",
            uri=None,
            config_version=1,
            presence="present",
            payload={"config_version": 1},
            layer_digest="b" * 64,
        )
        wal = Path(f"{database}-wal")
        before = {path: path.read_bytes() for path in (database, wal)}
        assert before[wal]
        output, _ = invoke_diagnostic("doctor")
        assert output["gateway"]["sqlite"]["sources"][0]["layer_digest"] == "b" * 64
        assert {path: path.read_bytes() for path in before} == before
    finally:
        reader.close()
        state.close()


@pytest.mark.parametrize("action", ["doctor", "diagnose"])
def test_real_cli_missing_distribution_asset_reports_safe_failure(
    tmp_path: Path,
    diagnostic_home: Path,
    action: str,
) -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "configs.boxteam",
            action,
            "--project-root",
            str(tmp_path / "missing-source"),
        ],
        env=dict(os.environ),
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert completed.returncode == 1
    assert (
        "stage=select_resources, error=FileNotFoundError"
        in json.loads(completed.stdout)["error"]
    )
    assert str(tmp_path) not in completed.stdout + completed.stderr
    assert "Traceback" not in completed.stderr
    assert not diagnostic_home.exists()


@pytest.mark.parametrize("action", ["doctor", "diagnose"])
def test_real_cli_preserves_rejection_of_unrelated_migration_options(
    diagnostic_home: Path,
    action: str,
) -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "configs.boxteam", action, "--preview"],
        env=dict(os.environ),
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert completed.returncode == 2
    assert "只用于 migrate-runtime-config" in completed.stderr
    assert not diagnostic_home.exists()


@pytest.mark.skipif(sys.platform == "win32", reason="本例验证 POSIX 符号链接环")
def test_diagnostic_reports_source_path_even_when_symlink_target_cannot_resolve(
    diagnostic_home: Path, invoke_diagnostic: Callable,
) -> None:
    source = diagnostic_home / "config/gateway.jsonc"
    source.parent.mkdir(parents=True)
    source.symlink_to(source.name)
    output, _ = invoke_diagnostic("doctor")
    failed = output["gateway"]["jsonc"]["sources"][1]
    assert failed["path"] == str(source.absolute())
    assert failed["parse_error"] == "stage=read, error=RuntimeError"
    assert source.is_symlink() and source.readlink() == Path(source.name)
    source.unlink()
    source.write_text("{}", encoding="utf-8")
    retried, _ = invoke_diagnostic("doctor")
    assert retried["gateway"]["jsonc"]["effective"]["valid"] is True
    assert retried["gateway"]["jsonc"]["sources"][1]["path"] == failed["path"]
