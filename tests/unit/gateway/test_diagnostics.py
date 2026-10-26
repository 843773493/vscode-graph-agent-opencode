from pathlib import Path

import pytest

from app.core.logging_contract import load_runtime_log_contract
from app.gateway.diagnostics import (
    _candidate_gateway_logs,
    _launcher_log_path,
    _LogCandidate,
    _process_log_path,
    _scope_log_candidates,
)
from app.schemas.gateway.diagnostics import GatewayDiagnosticLogSource


def _log_candidate(
    log_id: str,
    *,
    source: GatewayDiagnosticLogSource,
    workspace_id: str | None = None,
) -> _LogCandidate:
    return _LogCandidate(
        log_id=log_id,
        source=source,
        service="test",
        label=log_id,
        log_path=Path("candidate-logs") / f"{log_id.replace(':', '-')}.log",
        workspace_id=workspace_id,
    )


def test_workspace_scope_keeps_launcher_and_selected_workspace_logs() -> None:
    gateway_candidates = [
        _log_candidate("gateway:launcher", source="gateway"),
        _log_candidate("gateway:file:local-backend-8010.log", source="gateway"),
    ]
    workspace_candidates = [
        _log_candidate("workspace:home:workspace_api", source="workspace", workspace_id="home"),
        _log_candidate("workspace:other:workspace_api", source="workspace", workspace_id="other"),
    ]

    scoped = _scope_log_candidates(gateway_candidates, workspace_candidates, "home")

    assert [candidate.log_id for candidate in scoped] == [
        "gateway:launcher",
        "workspace:home:workspace_api",
    ]


def test_empty_workspace_scope_keeps_all_log_sources() -> None:
    gateway_candidates = [_log_candidate("gateway:launcher", source="gateway")]
    workspace_candidates = [_log_candidate("workspace:home:workspace_api", source="workspace", workspace_id="home")]

    scoped = _scope_log_candidates(gateway_candidates, workspace_candidates, None)

    assert scoped == [*gateway_candidates, *workspace_candidates]


def test_diagnostics_finds_role_logs_and_launcher_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BOXTEAM_HOME", str(tmp_path))
    override = tmp_path / "显式路径/launcher.log"
    monkeypatch.setenv("BOXTEAM_SERVICE_LOG_PATH", str(override))
    contract = load_runtime_log_contract()
    root = contract.root_for(tmp_path)
    paths = [contract.path_for(root, "gateway"), contract.path_for(root, "backend", 12345), contract.path_for(root, "ssh-port-forward", "pf_test")]
    for log_path in paths:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("original diagnostics\n")
    assert _process_log_path("workspace_api", 12345) == paths[1]
    assert _launcher_log_path() == override
    candidates = _candidate_gateway_logs({paths[1].name})
    assert {candidate.log_path for candidate in candidates} == {override, paths[0], paths[2]}
