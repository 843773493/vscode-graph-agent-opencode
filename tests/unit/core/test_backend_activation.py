from app.core import path_utils
from app.core.workspace_identity import load_or_create_workspace_id


def test_initialize_directories_only_creates_current_workspace_root(tmp_path, monkeypatch):
    user_workspace_root = tmp_path / "default-workspace"
    runtime_workspace_root = tmp_path / "remote-workspace"
    monkeypatch.setenv("BOXTEAM_USER_WORKSPACE_ROOT", str(user_workspace_root))
    monkeypatch.setenv("WORKSPACE_ROOT", str(runtime_workspace_root))
    runtime_workspace_root.mkdir(parents=True)
    workspace_id = load_or_create_workspace_id(runtime_workspace_root)

    path_utils.initialize_directories(workspace_id, runtime_workspace_root)

    assert (runtime_workspace_root / ".boxteam" / "sessions").is_dir()
    assert (
        runtime_workspace_root / ".boxteam" / "navigation" / "session-catalog.sqlite"
    ).is_file()
    assert not user_workspace_root.exists()


def test_initialize_directories_does_not_scan_or_mutate_legacy_storage(
    tmp_path,
    monkeypatch,
):
    workspace_root = tmp_path / "workspace"
    monkeypatch.setenv("WORKSPACE_ROOT", str(workspace_root))

    boxteam_root = workspace_root / ".boxteam"
    sessions_root = boxteam_root / "sessions"
    legacy_session = sessions_root / "legacy-session"
    legacy_manifest = legacy_session / "session.json"
    legacy_manifest.parent.mkdir(parents=True)
    legacy_manifest.write_text(
        '{"session_id":"ses_legacy000000000000000000000000"}\n',
        encoding="utf-8",
    )
    legacy_trace = legacy_session / "logs" / "traces" / "events.jsonl"
    legacy_trace.parent.mkdir(parents=True)
    legacy_trace.write_text(
        '{"timestamp":"2026-06-29T02:07:35.569434"}\n',
        encoding="utf-8",
    )
    legacy_checkpoint = boxteam_root / "checkpoints" / legacy_manifest.stem
    legacy_checkpoint.mkdir(parents=True)
    (legacy_checkpoint / "state.json").write_text("legacy", encoding="utf-8")
    legacy_trace_source = (
        boxteam_root
        / "logs"
        / "traces"
        / "trace_ses_legacy000000000000000000000000.jsonl"
    )
    legacy_trace_source.parent.mkdir(parents=True)
    legacy_trace_source.write_text("legacy trace\n", encoding="utf-8")

    tracked_files = {
        path: path.read_bytes()
        for path in (
            legacy_manifest,
            legacy_trace,
            legacy_checkpoint / "state.json",
            legacy_trace_source,
        )
    }

    workspace_id = load_or_create_workspace_id(workspace_root)
    path_utils.initialize_directories(workspace_id, workspace_root)

    for path, content in tracked_files.items():
        assert path.is_file()
        assert path.read_bytes() == content
    assert not (boxteam_root / "migrations").exists()
