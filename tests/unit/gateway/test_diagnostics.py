from pathlib import Path

from app.gateway.diagnostics import _LogCandidate, _scope_log_candidates


def _candidate(
    log_id: str,
    *,
    source: str,
    workspace_id: str | None = None,
) -> _LogCandidate:
    return _LogCandidate(
        log_id=log_id,
        source=source,
        service="test",
        label=log_id,
        path=Path(f"/tmp/{log_id.replace(':', '-')}.log"),
        workspace_id=workspace_id,
    )


def test_workspace_scope_keeps_launcher_and_selected_workspace_logs() -> None:
    gateway_candidates = [
        _candidate("gateway:launcher", source="gateway"),
        _candidate("gateway:file:local-backend-8010.log", source="gateway"),
    ]
    workspace_candidates = [
        _candidate("workspace:home:workspace_api", source="workspace", workspace_id="home"),
        _candidate("workspace:other:workspace_api", source="workspace", workspace_id="other"),
    ]

    scoped = _scope_log_candidates(gateway_candidates, workspace_candidates, "home")

    assert [candidate.log_id for candidate in scoped] == [
        "gateway:launcher",
        "workspace:home:workspace_api",
    ]


def test_empty_workspace_scope_keeps_all_log_sources() -> None:
    gateway_candidates = [_candidate("gateway:launcher", source="gateway")]
    workspace_candidates = [_candidate("workspace:home:workspace_api", source="workspace", workspace_id="home")]

    scoped = _scope_log_candidates(gateway_candidates, workspace_candidates, None)

    assert scoped == [*gateway_candidates, *workspace_candidates]
