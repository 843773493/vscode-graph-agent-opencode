"""多个真实 Workspace Backend 对共享用户配置来源的独立观察与追赶。"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import commentjson
import httpx
import pytest

from tests.harness.python.run_context import TestRunContext
from tests.support.ports import TestFilePorts, integration_ports_for_file
from tests.support.processes.workspace import (
    BackendProcess,
    close_backend_process,
    start_backend_process,
)
from tests.support.workspaces import (
    install_test_workspace_config,
    prepare_default_test_workspace,
)

_GATEWAY_ID = "gateway_" + "7" * 32
_USER_SOURCE_KEY = "workspace_mutable_override"
_SOURCE_URL = f"/api/v1/config/sources/{_USER_SOURCE_KEY}"
_USER_CONFIG_RELATIVE_PATH = Path("config/workspace.jsonc")
_TEST_COMMAND = (
    "timeout 420 bash -c 'ulimit -d 4194304; exec uv run --no-sync pytest "
    "-x -vv --tb=short tests/integration/backend/test_workspace_shared_source_edit.py'"
)


@dataclass(frozen=True, slots=True)
class SharedSourceEnvironment:
    context: TestRunContext
    workspaces: dict[str, Path]
    home: Path
    other_home: Path
    port_block: TestFilePorts


@dataclass(frozen=True, slots=True)
class BackendClient:
    process: BackendProcess
    client: httpx.Client


@pytest.fixture
def shared_source_environment(request: pytest.FixtureRequest) -> Iterator[SharedSourceEnvironment]:
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    config_path = Path.cwd() / "configs/tests/workspace/default.jsonc"
    schema_path = Path.cwd() / "configs/workspace_schema.jsonc"
    workspaces: dict[str, Path] = {}
    for name in ("a-shadowed", "b-live", "c-stopped", "d-other-home"):
        root = context.workspace_root if name == "a-shadowed" else context.workspace_root / name
        prepare_default_test_workspace(
            workspace_root=root,
            template_root=Path.cwd() / "tests/fixtures/workspaces/default_test_workspace",
            fresh_identity=True,
        )
        install_test_workspace_config(
            workspace_root=root,
            config_path=config_path,
            schema_path=schema_path,
        )
        if name == "a-shadowed":
            workspace_config = root / ".boxteam/workspace.jsonc"
            payload = commentjson.loads(workspace_config.read_text(encoding="utf-8"))
            cast(dict[str, object], payload.setdefault("ui", {}))["auto_summarize"] = True
            workspace_config.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        workspaces[name] = root
    runtime_root = context.runtime_root_for_node(request.node.nodeid)
    yield SharedSourceEnvironment(
        context=context,
        workspaces=workspaces,
        home=runtime_root / "shared-boxteam-home",
        other_home=runtime_root / "other-boxteam-home",
        port_block=integration_ports_for_file(Path(request.node.path)),
    )


@contextmanager
def _backend(
    *,
    workspace: Path,
    home: Path,
    port: int,
    name: str,
    artifacts_dir: Path,
) -> Iterator[BackendClient]:
    process = start_backend_process(
        workspace_root=str(workspace),
        port=port,
        log_name=f"shared-source-{name}",
        boxteam_home=home,
        refresh_user_configuration=False,
        artifacts_dir=artifacts_dir,
    )
    try:
        with httpx.Client(
            base_url=f"http://127.0.0.1:{process.port}",
            headers={
                "X-Local-Token": "local-dev-token",
                "X-BoxTeam-Workspace-Id": process.workspace_id,
                "X-BoxTeam-Gateway-Id": _GATEWAY_ID,
            },
            timeout=30,
        ) as client:
            yield BackendClient(process, client)
    finally:
        close_backend_process(process)


def _data(response: httpx.Response) -> dict[str, object]:
    assert response.status_code == 200, response.text
    payload = response.json()
    assert response.headers["X-Request-ID"] == payload["request_id"]
    return cast(dict[str, object], payload["data"])


def _source_document(client: httpx.Client) -> dict[str, object]:
    return _data(client.get(_SOURCE_URL))


def _source_edit_request(
    document: dict[str, object], *, jsonc: str, idempotency_key: str,
) -> dict[str, object]:
    return {
        "gateway_id": document["gateway_id"],
        "workspace_id": document["workspace_id"],
        "source_key": document["source_key"],
        "jsonc": jsonc,
        "base_file_digest": document["file_digest"],
        "base_layer_revision": document["layer_revision"],
        "base_layer_digest": document["layer_digest"],
        "expected_active_revision": document["active_revision"],
        "expected_active_digest": document["active_digest"],
        "idempotency_key": idempotency_key,
    }


def _fanout(client: httpx.Client, idempotency_key: str) -> dict[str, object]:
    document = _data(client.get(_SOURCE_URL, params={"idempotency_key": idempotency_key}))
    return cast(dict[str, object], document["fanout"])


def _wait_for_fanout(
    client: httpx.Client,
    idempotency_key: str | None,
    expected: dict[str, str],
    *,
    timeout_seconds: float = 30,
) -> dict[str, object]:
    deadline = time.monotonic() + timeout_seconds
    while True:
        result = (
            _fanout(client, idempotency_key)
            if idempotency_key is not None
            else cast(dict[str, object], _source_document(client)["fanout"])
        )
        records = {
            str(record["workspace_id"]): cast(dict[str, object], record)
            for record in cast(list[dict[str, object]], result["workspaces"])
        }
        if all(
            workspace_id in records and records[workspace_id]["status"] == status
            for workspace_id, status in expected.items()
        ):
            return result
        assert time.monotonic() < deadline, result
        time.sleep(0.1)


def test_shared_user_source_is_observed_applied_and_caught_up_by_independent_backends(
    shared_source_environment: SharedSourceEnvironment,
) -> None:
    context = shared_source_environment.context
    workspaces = shared_source_environment.workspaces
    ports = shared_source_environment.port_block
    shared_home = shared_source_environment.home
    other_home = shared_source_environment.other_home
    artifacts = context.artifacts_dir
    evidence: dict[str, object] = {}
    idempotency_key = "shared-user-source-generation-2"
    with ExitStack() as active:
        a = active.enter_context(_backend(
            workspace=workspaces["a-shadowed"], home=shared_home,
            port=ports.port(0), name="a-shadowed", artifacts_dir=artifacts,
        ))
        b = active.enter_context(_backend(
            workspace=workspaces["b-live"], home=shared_home,
            port=ports.port(1), name="b-live", artifacts_dir=artifacts,
        ))
        d = active.enter_context(_backend(
            workspace=workspaces["d-other-home"], home=other_home,
            port=ports.port(3), name="d-other-home", artifacts_dir=artifacts,
        ))
        a_before = _source_document(a.client)
        b_before = _source_document(b.client)
        d_before = _source_document(d.client)
        with ExitStack() as stopped_consumer:
            c = stopped_consumer.enter_context(_backend(
                workspace=workspaces["c-stopped"], home=shared_home,
                port=ports.port(2), name="c-stopped", artifacts_dir=artifacts,
            ))
            c_before = _source_document(c.client)
            c_workspace_id = c.process.workspace_id
        assert a_before["source_key"] == b_before["source_key"] == c_before["source_key"] == _USER_SOURCE_KEY
        assert a_before["file_digest"] == b_before["file_digest"] == c_before["file_digest"]
        assert d_before["file_digest"] == a_before["file_digest"]
        initial_workspace = cast(dict[str, object], a_before["effective"])
        initial_ui = cast(dict[str, object], initial_workspace["ui"])
        assert initial_ui["auto_summarize"] is True
        assert cast(dict[str, object], cast(dict[str, object], b_before["effective"])["ui"])[
            "auto_summarize"
        ] is True

        original_user_text = str(a_before["jsonc"])
        user_payload = commentjson.loads(original_user_text)
        user_ui = cast(dict[str, object], user_payload.setdefault("ui", {}))
        user_ui["auto_summarize"] = False
        edited_text = json.dumps(user_payload, ensure_ascii=False, indent=2) + "\n"
        edit = _source_edit_request(
            a_before, jsonc=edited_text, idempotency_key=idempotency_key,
        )
        saved = _data(a.client.put(_SOURCE_URL, json=edit))
        assert cast(dict[str, object], saved["edit"])["source_saved"] is True
        assert cast(dict[str, object], saved["edit"])["layer_imported"] is True

        # 先等 B 自己的 watcher 完成本代应用，再读取它发布的 effective 快照。
        summary = _wait_for_fanout(
            a.client,
            idempotency_key,
            {a.process.workspace_id: "applied", b.process.workspace_id: "applied"},
        )
        a_effective = cast(dict[str, object], _source_document(a.client)["effective"])
        assert cast(dict[str, object], a_effective["ui"])["auto_summarize"] is True
        b_effective = cast(dict[str, object], _source_document(b.client)["effective"])
        assert cast(dict[str, object], b_effective["ui"])["auto_summarize"] is False
        b_source = _source_document(b.client)
        assert b_source["source_generation"] == saved["source_generation"]

        # fanout 仅汇总被观察到的 Workspace；停止的 C 仍可见为 unobserved，不能算成功。
        assert summary["coverage"] == "observed_only"
        assert summary["result"] == "fanout_partial"
        observed = {
            str(record["workspace_id"]): cast(dict[str, object], record)
            for record in cast(list[dict[str, object]], summary["workspaces"])
        }
        assert observed[a.process.workspace_id]["result"] == "unchanged"
        assert observed[b.process.workspace_id]["result"] == "applied"
        c_record = observed[c_workspace_id]
        assert c_record["status"] == "unobserved"

        # 确认另一 Home 未收到该更新，且有效配置与初始快照一致。
        d_after = _source_document(d.client)
        assert d_after["file_digest"] == d_before["file_digest"]
        assert d_after["source_generation"] == d_before["source_generation"]
        assert d_after["effective"] == d_before["effective"]

        evidence["initial"] = {
            "a": {"workspace_id": a.process.workspace_id, "source_generation": a_before["source_generation"]},
            "b": {"workspace_id": b.process.workspace_id, "source_generation": b_before["source_generation"]},
            "c": {"workspace_id": c_workspace_id, "source_generation": c_before["source_generation"]},
            "d": {"workspace_id": d.process.workspace_id, "source_generation": d_before["source_generation"]},
        }
        evidence["generation"] = saved["source_generation"]
        evidence["a"] = {
            "effective_auto_summarize": cast(dict[str, object], a_effective["ui"])["auto_summarize"],
            "fanout": observed[a.process.workspace_id],
        }
        evidence["b"] = {
            "effective_auto_summarize": cast(dict[str, object], b_effective["ui"])["auto_summarize"],
            "fanout": observed[b.process.workspace_id],
        }
        evidence["c_stopped"] = c_record
        evidence["d"] = {
            "source_digest_unchanged": d_after["file_digest"] == d_before["file_digest"],
            "effective_unchanged": d_after["effective"] == d_before["effective"],
            "source_generation": d_after["source_generation"],
        }

    # C 已停止后重新启动同一工作区；它读取最新文件并报告本次真实应用。
    with _backend(
        workspace=workspaces["c-stopped"], home=shared_home,
        port=ports.port(2), name="c-catch-up", artifacts_dir=artifacts,
    ) as c_restarted:
        caught_up = _source_document(c_restarted.client)
        assert cast(dict[str, object], cast(dict[str, object], caught_up["effective"])["ui"])[
            "auto_summarize"
        ] is False
        assert caught_up["source_generation"] == saved["source_generation"]
        catch_up_summary = _wait_for_fanout(
            c_restarted.client,
            None,
            {c_restarted.process.workspace_id: "applied"},
        )
        catch_up_record = next(
            cast(dict[str, object], record)
            for record in cast(list[dict[str, object]], catch_up_summary["workspaces"])
            if record["workspace_id"] == c_restarted.process.workspace_id
        )
        assert catch_up_summary["coverage"] == "observed_only"
        assert catch_up_record["result"] == "applied"
        evidence["c_restarted"] = {
            "workspace_id": c_restarted.process.workspace_id,
            "source_generation": caught_up["source_generation"],
            "effective_auto_summarize": cast(
                dict[str, object], cast(dict[str, object], caught_up["effective"])["ui"],
            )["auto_summarize"],
            "fanout": catch_up_record,
        }

    # 非法绑定与过期 file CAS 都通过真实 Backend API 验证，拒绝时不改写源文件。
    with _backend(
        workspace=workspaces["b-live"], home=shared_home,
        port=ports.port(1), name="b-rejections", artifacts_dir=artifacts,
    ) as b_restarted:
        user_config = shared_home / _USER_CONFIG_RELATIVE_PATH
        before_invalid = user_config.read_bytes()
        invalid_payload = commentjson.loads(before_invalid.decode("utf-8"))
        settings = cast(dict[str, object], invalid_payload["settings"])
        workspace_settings = cast(dict[str, object], settings["workspace"])
        plugins = cast(dict[str, object], workspace_settings["plugins"])
        runtime = cast(dict[str, object], plugins["runtime"])
        runtime["entries"] = [{
            "package_id": "invalid.shared.user",
            "plugin_id": "invalid-binding",
            "package_version": "1.0.0",
            "enabled": False,
        }]
        invalid_request = _source_edit_request(
            _source_document(b_restarted.client),
            jsonc=json.dumps(invalid_payload, ensure_ascii=False, indent=2) + "\n",
            idempotency_key="shared-user-source-invalid-plugin-binding",
        )
        invalid = b_restarted.client.put(_SOURCE_URL, json=invalid_request)
        assert invalid.status_code == 422, invalid.text
        assert user_config.read_bytes() == before_invalid
        evidence["invalid_plugin_binding"] = {
            "status_code": invalid.status_code,
            "detail": invalid.json().get("detail"),
            "file_digest_before": hashlib.sha256(before_invalid).hexdigest(),
            "file_digest_after": hashlib.sha256(user_config.read_bytes()).hexdigest(),
        }

        stale_document = _source_document(b_restarted.client)
        stale_request = _source_edit_request(
            stale_document,
            jsonc=str(stale_document["jsonc"]).replace(
                '"auto_summarize": false', '"auto_summarize": true',
            ),
            idempotency_key="shared-user-source-stale-file-cas",
        )
        user_config.write_bytes(user_config.read_bytes() + "// 外部并发修改\n".encode())
        externally_edited = user_config.read_bytes()
        rejected = b_restarted.client.put(_SOURCE_URL, json=stale_request)
        assert rejected.status_code == 409, rejected.text
        assert user_config.read_bytes() == externally_edited
        evidence["stale_file_cas"] = {
            "status_code": rejected.status_code,
            "detail": rejected.json().get("detail"),
            "file_digest_before": hashlib.sha256(externally_edited).hexdigest(),
            "file_digest_after": hashlib.sha256(user_config.read_bytes()).hexdigest(),
        }

    source_paths = (
        Path(__file__),
        Path("app/services/infrastructure/config_service/config_source_edit.py"),
        Path("app/services/infrastructure/config_service/config_source_layers.py"),
        Path("app/services/infrastructure/config_service/config_reload.py"),
        Path("app/services/infrastructure/config/source_owner.py"),
        Path("app/schemas/internal_v2/config.py"),
    )
    evidence["command"] = _TEST_COMMAND
    evidence["source_fingerprints"] = {
        path.as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in source_paths
    }
    context.write_result("passed", evidence)
