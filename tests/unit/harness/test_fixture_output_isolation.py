from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from app.core.distribution_identity import load_distribution_id
from tests import conftest as root_fixtures
from tests.e2e.system.gateway import conftest as docker_fixtures
from tests.harness.python.run_context import TestRunContext
from tests.unit.conftest import _workspace_scope


@pytest.mark.parametrize("difference", ["node", "run", "worker"])
def test_unit_workspace_setup_does_not_delete_another_active_workspace(
    monkeypatch: pytest.MonkeyPatch, difference: str,
) -> None:
    monkeypatch.setenv("BOXTEAM_TEST_RUN_ID", "first")
    monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw0")

    def request(node: str) -> pytest.FixtureRequest:
        return cast(pytest.FixtureRequest, SimpleNamespace(node=SimpleNamespace(
            path=Path(__file__), nodeid=f"{__file__}::{node}",
        )))

    scope = contextmanager(_workspace_scope)
    with scope(request("first"), template_items=()) as (_, first):
        marker = first / "active.txt"
        marker.write_bytes(b"still in use")
        if difference == "run":
            monkeypatch.setenv("BOXTEAM_TEST_RUN_ID", "second")
        elif difference == "worker":
            monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw1")
        with scope(request("second" if difference == "node" else "first"), template_items=()) as (_, second):
            assert second != first
            assert not (second / marker.name).exists()
            assert marker.read_bytes() == b"still in use"


def test_runtime_manifests_are_private_even_with_the_same_run_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BOXTEAM_TEST_RUN_ID", "shared-parent-run")
    manifest_scope = contextmanager(root_fixtures.runtime_manifest_path._get_wrapped_function())
    with manifest_scope(None) as first:
        original = first.read_bytes()
        with manifest_scope(None) as second:
            assert first != second
            assert first.read_bytes() == original
            assert load_distribution_id(first) == load_distribution_id(second)
        assert first.read_bytes() == original


def test_docker_setup_resets_workspace_but_preserves_previous_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    template = tmp_path / "tests/fixtures/workspaces/default_test_workspace"
    template.mkdir(parents=True)
    (template / "fixture.txt").write_bytes(b"fixture")
    test_file = tmp_path / "tests/e2e/system/gateway/test_case.py"
    request = cast(pytest.FixtureRequest, SimpleNamespace(node=SimpleNamespace(fspath=test_file)))
    context = TestRunContext.from_test_file(test_file)
    historical = context.output_root / "artifacts/old.log"
    historical.parent.mkdir(parents=True)
    historical.write_bytes(b"previous evidence")
    context.workspace_root.mkdir()
    (context.workspace_root / "stale.txt").write_bytes(b"old workspace")
    prepare = docker_fixtures.docker_e2e_paths._get_wrapped_function()

    first = prepare(request)
    evidence = first.artifacts / "current.log"
    evidence.write_bytes(b"first run")
    monkeypatch.setenv("BOXTEAM_TEST_RUN_ID", "second-docker-run")
    second = prepare(request)

    assert first.artifacts != second.artifacts
    assert historical.read_bytes() == b"previous evidence"
    assert evidence.read_bytes() == b"first run"
    assert not (second.local_workspace / "stale.txt").exists()
    assert (second.local_workspace / "fixture.txt").read_bytes() == b"fixture"
    assert first.remote_workspace == second.remote_workspace


def test_collection_marks_only_the_items_in_each_layer(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(request.config, "getoption", lambda *_args, **_kwargs: False)
    items = []
    layers = {"unit": "unit", "contracts": "contract", "integration": "integration", "e2e": "e2e"}
    for layer in layers:
        module = pytest.Module.from_parent(request.session, path=Path.cwd() / f"tests/{layer}/test_probe.py")
        for name in ("first", "second"):
            item = pytest.Function.from_parent(module, name=name, callobj=lambda: None)
            item.add_marker("live_model")
            items.append(item)

    root_fixtures.pytest_collection_modifyitems(request.config, items)

    groups = {}
    for item in items:
        layer = item.path.relative_to(Path.cwd() / "tests").parts[0]
        assert {mark.name for mark in item.iter_markers()} & set(layers.values()) == {layers[layer]}
        assert item.get_closest_marker("live_model") is not None
        group = list(item.iter_markers("xdist_group"))
        if layer in {"integration", "e2e"}:
            assert len(group) == 1
            group_name = group[0].kwargs["name"]
            assert groups.setdefault(layer, group_name) == group_name
            assert group_name.startswith(f"{layer}_file_")
        else:
            assert not group


def test_keep_docker_runtime_rejects_multiple_collected_items(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(request.config, "getoption", lambda *_args, **_kwargs: True)
    with pytest.raises(pytest.UsageError, match="只能配合一个"):
        root_fixtures.pytest_collection_modifyitems(request.config, [request.node, request.node])
