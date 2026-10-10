"""正式隔离工作区中的文件树/Gateway 自有 mutation 量化硬门控。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest

from tests.harness.python.run_context import TestRunContext
from tests.support.gateway_processes import (
    LOCAL_TOKEN_HEADERS,
    acquire_gateway_guest,
    close_gateway_process,
    start_gateway_process,
)
from tests.support.ports import integration_port_block_for_file
from tests.support.processes import close_backend_process, start_backend_process


@pytest.fixture
async def navigation_measurement_stack(
    request: pytest.FixtureRequest,
    integration_workspace_root_path: str,
) -> AsyncIterator[tuple[TestRunContext, httpx.AsyncClient]]:
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    workspace = Path(integration_workspace_root_path)
    for index in range(200):
        directory = workspace / f"d{index:03d}"
        directory.mkdir()
        for child in range(3):
            (directory / f"seed_{child}.txt").write_text("正式量化目录内容\n", encoding="utf-8")
    (workspace / "concurrent").mkdir()
    sources = workspace / "a_sources"
    sources.mkdir()
    for kind in ("paste", "copy"):
        for expanded in (1, 20, 200):
            for index in range(20):
                (sources / f"{kind}_{expanded}_{index:03d}.txt").write_text(
                    "正式量化复制内容\n", encoding="utf-8"
                )
    ports = integration_port_block_for_file(Path(request.node.path))
    backend = start_backend_process(
        workspace_root=str(workspace), port=ports.port(0), log_name="measurements",
        asgi_app="tests.integration.clients.web.navigation_measurement_app:app",
        env_overrides={
            "BOXTEAM_MEASUREMENT_RUNTIME": "backend",
            "BOXTEAM_MEASUREMENT_READ_ROOTS": json.dumps([str(workspace)]),
            "BOXTEAM_NAVIGATION_TRACE": os.environ.get("BOXTEAM_NAVIGATION_TRACE", ""),
        },
    )
    context.add_cleanup("measurement Backend", lambda: close_backend_process(backend))
    try:
        gateway = start_gateway_process(
            workspace_root=workspace, default_backend_url=f"http://127.0.0.1:{backend.port}",
            port=ports.port(1), artifacts_dir=context.artifacts_dir,
            asgi_app="tests.integration.clients.web.navigation_measurement_app:app",
            extra_env={
                "BOXTEAM_MEASUREMENT_RUNTIME": "gateway",
                "BOXTEAM_MEASUREMENT_READ_ROOTS": json.dumps([str(context.boxteam_home)]),
            },
        )
    except BaseException:
        context.close()
        raise
    context.add_cleanup("measurement Gateway", lambda: close_gateway_process(gateway))
    try:
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{gateway.port}", headers=LOCAL_TOKEN_HEADERS, timeout=60,
        ) as client:
            await acquire_gateway_guest(client)
            trace_path = context.artifacts_dir / "backend-concurrent-observation.json"

            async def save_trace() -> None:
                async with httpx.AsyncClient(
                    base_url=f"http://127.0.0.1:{backend.port}",
                    headers=LOCAL_TOKEN_HEADERS,
                    timeout=30,
                ) as diagnostic_client:
                    response = await diagnostic_client.get(
                        "/api/v1/test/navigation-measurements/concurrent-trace"
                    )
                response.raise_for_status()
                trace_path.write_text(
                    json.dumps(response.json(), ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )

            try:
                yield context, client
            except BaseException as original_error:
                if os.environ.get("BOXTEAM_NAVIGATION_TRACE") == "concurrent" and not trace_path.exists():
                    try:
                        await save_trace()
                    except (httpx.HTTPError, json.JSONDecodeError, OSError) as diagnostic_error:
                        error_path = context.artifacts_dir / "backend-concurrent-observation-error.json"
                        try:
                            error_path.write_text(json.dumps({
                                "error_type": type(diagnostic_error).__name__,
                                "error": str(diagnostic_error),
                            }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                        except OSError as artifact_error:
                            original_error.add_note(
                                f"并发 trace 诊断失败: {diagnostic_error!r}; 诊断文件写入也失败: {artifact_error!r}"
                            )
                        else:
                            original_error.add_note(
                                f"并发 trace 诊断也失败，详情见 {error_path}: {diagnostic_error!r}"
                            )
                raise
            else:
                if os.environ.get("BOXTEAM_NAVIGATION_TRACE") == "concurrent" and not trace_path.exists():
                    await save_trace()
    finally:
        context.close()


@pytest.mark.asyncio
async def test_navigation_mutation_measurement_gate(
    navigation_measurement_stack: tuple[TestRunContext, httpx.AsyncClient],
) -> None:
    context, client = navigation_measurement_stack
    focus_concurrent = os.environ.get("BOXTEAM_NAVIGATION_FOCUS") == "concurrent"
    if focus_concurrent:
        response = await client.post("/api/v1/sessions", json={"title": "并发阶段诊断会话"})
        assert response.status_code == 200, response.text
        session = response.json()["data"]
        navigation_response = await client.get("/api/gateway/workspace-navigation")
        assert navigation_response.status_code == 200, navigation_response.text
        navigation = navigation_response.json()["data"]
        workspace_response = await client.get("/api/gateway/workspaces")
        assert workspace_response.status_code == 200, workspace_response.text
        workspace_id = workspace_response.json()["data"]["active_workspace_id"]
        nodes = navigation["nodes"]
        assert isinstance(nodes, list)
        workspace_node = next(node for node in nodes if node.get("workspace_id") == workspace_id)
        folders = [node["node_id"] for node in nodes if node["kind"] == "workspace_folder"]
        configuration = {
            "gatewayUrl": str(client.base_url), "gatewayPort": client.base_url.port,
            "workspaceId": workspace_id, "backendWorkspaceId": session["workspace_id"],
            "workspaceRoot": str(context.workspace_root), "sessionId": session["session_id"],
            "workspaceNodeId": workspace_node["node_id"], "gatewayFolderIds": folders[:2],
            "cookies": [{"name": cookie.name, "value": cookie.value} for cookie in client.cookies.jar],
            "observationHeaders": dict(LOCAL_TOKEN_HEADERS),
            "seed_counts": {"created_sessions": 1, "workspace_directories": 200,
                            "workspace_source_files": 120, "gateway_folders": len(folders)},
        }
        configuration_path = context.artifacts_dir / "concurrent-configuration.json"
        configuration_path.write_text(json.dumps(configuration, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        browser_log = context.artifacts_dir / "concurrent-browser.raw.log"
        with browser_log.open("wb") as stream:
            process = await asyncio.create_subprocess_exec(
                "bun", "tests/integration/clients/web/navigation_mutation_measurements.mjs",
                str(configuration_path), str(context.artifacts_dir),
                stdout=stream, stderr=asyncio.subprocess.STDOUT,
            )
            try:
                await asyncio.wait_for(process.wait(), 180)
            except BaseException:
                if process.returncode is None:
                    process.kill()
                    await process.wait()
                raise
        output = browser_log.read_bytes()
        assert process.returncode == 0, output.decode(errors="replace")[-10000:]
        measurements = json.loads((context.artifacts_dir / "measurements.json").read_text(encoding="utf-8"))
        persisted = sorted(path.name for path in (context.workspace_root / "concurrent").iterdir())
        measurements["concurrent"]["persisted_names"] = persisted
        measurements["concurrent"]["missing_from_disk"] = sorted(
            set(measurements["concurrent"]["expected_names"]) - set(persisted)
        )
        (context.artifacts_dir / "measurements.json").write_text(
            json.dumps(measurements, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        actions = measurements["concurrent"]["actions"]
        assert len(actions) == 20
        assert len(measurements["concurrent"]["expected_names"]) == 20
        assert {action["name"] for action in actions} == set(measurements["concurrent"]["expected_names"])
        assert len({action["operation_id"] for action in actions}) == 20
        assert not measurements["concurrent"]["missing_from_ui"]
        assert len(measurements["concurrent"]["missing_from_disk"]) == 0
        assert all(action["accepted_state"] in {"queued", "running", "committed"}
            and action["terminal_state"] == "committed" and action["terminal_effect"] == "complete"
            and action["confirmation_ui_completed"] and action["initiating_ui_path_visible"]
            for action in measurements["concurrent"]["actions"])
        assert all(len(action["mutation_attempts"]) >= 1
            and any(attempt["outcome"] == "accepted" for attempt in action["mutation_attempts"])
            for action in measurements["concurrent"]["actions"])
        assert measurements["file_gate"]["decision"] == "not_evaluated"
        trace_count = 0
        if os.environ.get("BOXTEAM_NAVIGATION_TRACE") == "concurrent":
            trace = json.loads((context.artifacts_dir / "backend-concurrent-observation.json").read_text(encoding="utf-8"))
            assert trace["dropped_events"] == 0
            assert any(event["event"] == "scan_directory" for event in trace["events"])
            assert any(event["event"] == "store_observe" for event in trace["events"])
            observed_operation_ids = {event.get("operation_id") for event in trace["events"]}
            assert {action["operation_id"] for action in actions} <= observed_operation_ids
            trace_count = len(trace["events"])
        context.write_result("passed", {"focus": "concurrent", "pairs": 10,
            "actions": len(measurements["concurrent"]["actions"]), "backend_trace_events": trace_count})
        return

    last_session: dict[str, object] = {}
    for index in range(200):
        response = await client.post("/api/v1/sessions", json={"title": f"量化会话 {index:03d}"})
        assert response.status_code == 200, response.text
        last_session = response.json()["data"]
    navigation: dict[str, object] = {}
    for index in range(200):
        response = await client.post("/api/gateway/workspace-navigation/folders", json={"name": f"量化 Gateway 目录 {index:03d}"})
        assert response.status_code == 200, response.text
        navigation = response.json()["data"]
    workspace_response = await client.get("/api/gateway/workspaces")
    assert workspace_response.status_code == 200, workspace_response.text
    workspace_id = workspace_response.json()["data"]["active_workspace_id"]
    nodes = navigation["nodes"]
    assert isinstance(nodes, list)
    workspace_node = next(node for node in nodes if node.get("workspace_id") == workspace_id)
    folders = [node["node_id"] for node in nodes if node["kind"] == "workspace_folder"]
    configuration = {
        "gatewayUrl": str(client.base_url), "gatewayPort": client.base_url.port,
        "workspaceId": workspace_id, "backendWorkspaceId": last_session["workspace_id"],
        "workspaceRoot": str(context.workspace_root), "sessionId": last_session["session_id"],
        "workspaceNodeId": workspace_node["node_id"], "gatewayFolderIds": folders[:2],
        "cookies": [{"name": cookie.name, "value": cookie.value} for cookie in client.cookies.jar],
        "observationHeaders": dict(LOCAL_TOKEN_HEADERS),
        "seed_counts": {"created_sessions": 200, "workspace_directories": 200,
                        "workspace_source_files": 120, "gateway_folders": len(folders)},
    }
    configuration_path = context.artifacts_dir / "configuration.json"
    configuration_path.write_text(json.dumps(configuration, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    browser_log = context.artifacts_dir / "browser.raw.log"
    with browser_log.open("wb") as stream:
        process = await asyncio.create_subprocess_exec(
            "bun", "tests/integration/clients/web/navigation_mutation_measurements.mjs",
            str(configuration_path), str(context.artifacts_dir),
            stdout=stream, stderr=asyncio.subprocess.STDOUT,
        )
        try:
            await asyncio.wait_for(process.wait(), 900)
        except BaseException:
            if process.returncode is None:
                process.kill()
                await process.wait()
            raise
    output = browser_log.read_bytes()
    assert process.returncode == 0, output.decode(errors="replace")[-10000:]
    measurements = json.loads((context.artifacts_dir / "measurements.json").read_text(encoding="utf-8"))
    assert len(measurements["mutation_samples"]) == 300
    assert len(measurements["gateway_samples"]) == 40
    concurrent_operation_ids = {
        action["operation_id"] for action in measurements["concurrent"]["actions"]
    }
    if os.environ.get("BOXTEAM_NAVIGATION_TRACE") == "concurrent":
        trace = json.loads((context.artifacts_dir / "backend-concurrent-observation.json").read_text(encoding="utf-8"))
        assert trace["dropped_events"] == 0
        assert any(event["event"] == "scan_directory" for event in trace["events"])
        assert any(event["event"] == "store_observe" for event in trace["events"])
        observed_operation_ids = {event.get("operation_id") for event in trace["events"]}
        assert len(concurrent_operation_ids) == 20
        assert concurrent_operation_ids <= observed_operation_ids
    clock_alignment = measurements["clock_alignment"]
    assert {sample["label"] for sample in clock_alignment["samples"]} == {"run_before", "run_after"}
    assert not clock_alignment["errors"]
    for bracket in clock_alignment["samples"]:
        for point in (bracket["node_before"], bracket["browser"], bracket["node_after"]):
            assert all(math.isfinite(point[field]) for field in ("time_origin_ms", "now_ms", "epoch_ms"))
            assert point["epoch_ms"] == point["time_origin_ms"] + point["now_ms"]
        assert bracket["node_after"]["epoch_ms"] >= bracket["node_before"]["epoch_ms"]
    for sample in measurements["mutation_samples"]:
        snapshot = sample["ui_completion"]
        assert snapshot["observation_source"] == "mounted_mutation_observer"
        assert snapshot["operation_id"] == sample["operation_id"]
        assert snapshot["client_sequence"] == sample["client_sequence"]
        assert snapshot["queue_seq"] == sample["accepted_queue_seq"]
        assert snapshot["receipt_revision"] == sample["receipt_revision"]
        assert snapshot["event_index"] >= snapshot["baseline_event_offset"]
        assert snapshot["snapshot_index"] >= snapshot["baseline_snapshot_offset"]
        assert snapshot["target_row_visible"] is True
        assert snapshot["target_path_title"] == str(
            context.workspace_root / sample["directory"] / sample["name"]
        )
        assert snapshot["browser_epoch_ms"] == snapshot["browser_time_origin_ms"] + snapshot["browser_now_ms"]
        assert math.isfinite(sample["elapsed_ms"]) and sample["elapsed_ms"] >= 0
        assert sample["elapsed_ms"] == snapshot["browser_epoch_ms"] - sample["ui_elapsed_clock"]["node"]["start_epoch_ms"]
        assert sample["fs_window"]["endpoint_epoch_ms"] == snapshot["browser_epoch_ms"]
        assert sample["fs_window"]["deadline_epoch_ms"] == snapshot["browser_epoch_ms"] + 650
        assert sample["fs_window"]["timer_observed_epoch_ms"] >= sample["fs_window"]["deadline_epoch_ms"]
    assert measurements["file_gate"]["decision"] in {"10.4b-1", "10.4b-2", "10.4b-3"}
    persisted = sorted(path.name for path in (context.workspace_root / "concurrent").iterdir())
    measurements["concurrent"]["persisted_names"] = persisted
    measurements["concurrent"]["missing_from_disk"] = sorted(
        set(measurements["concurrent"]["expected_names"]) - set(persisted)
    )
    if measurements["concurrent"]["missing_from_disk"]:
        measurements["file_gate"]["decision"] = "10.4b-1"
    (context.artifacts_dir / "measurements.json").write_text(
        json.dumps(measurements, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps({"file_gate": measurements["file_gate"], "gateway_summary": measurements["gateway_summary"],
        "concurrent": measurements["concurrent"], "formal_artifacts": str(context.artifacts_dir)}, ensure_ascii=False))
    context.write_result("passed", {"file_gate": measurements["file_gate"], "sample_count": 300})


@pytest.mark.parametrize("navigation_measurement_stack", [pytest.param(True, id="minimal")], indirect=True)
@pytest.mark.asyncio
async def test_navigation_mutation_measurement_minimal_samples(
    navigation_measurement_stack: tuple[TestRunContext, httpx.AsyncClient],
) -> None:
    context, client = navigation_measurement_stack
    response = await client.post("/api/v1/sessions", json={"title": "导航 mutation 最小样本"})
    assert response.status_code == 200, response.text
    session = response.json()["data"]
    navigation: dict[str, object] = {}
    for index in range(1):
        response = await client.post("/api/gateway/workspace-navigation/folders", json={"name": f"最小 Gateway 目录 {index}"})
        assert response.status_code == 200, response.text
        navigation = response.json()["data"]
    workspace_response = await client.get("/api/gateway/workspaces")
    assert workspace_response.status_code == 200, workspace_response.text
    workspace_id = workspace_response.json()["data"]["active_workspace_id"]
    nodes = navigation["nodes"]
    assert isinstance(nodes, list)
    workspace_node = next(node for node in nodes if node.get("workspace_id") == workspace_id)
    folders = [node["node_id"] for node in nodes if node["kind"] == "workspace_folder"]
    configuration = {
        "gatewayUrl": str(client.base_url), "gatewayPort": client.base_url.port,
        "workspaceId": workspace_id, "backendWorkspaceId": session["workspace_id"],
        "workspaceRoot": str(context.workspace_root), "sessionId": session["session_id"],
        "workspaceNodeId": workspace_node["node_id"], "gatewayFolderIds": folders,
        "cookies": [{"name": cookie.name, "value": cookie.value} for cookie in client.cookies.jar],
        "observationHeaders": dict(LOCAL_TOKEN_HEADERS),
        "seed_counts": {"created_sessions": 1, "workspace_directories": 200, "gateway_folders": len(folders)},
    }
    configuration_path = context.artifacts_dir / "minimal-configuration.json"
    configuration_path.write_text(json.dumps(configuration, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    browser_log = context.artifacts_dir / "minimal-browser.raw.log"
    with browser_log.open("wb") as stream:
        process = await asyncio.create_subprocess_exec(
            "bun", "tests/integration/clients/web/navigation_mutation_measurements.mjs",
            str(configuration_path), str(context.artifacts_dir), "--minimal-samples",
            stdout=stream, stderr=asyncio.subprocess.STDOUT,
        )
        try:
            await asyncio.wait_for(process.wait(), 150)
        except BaseException:
            if process.returncode is None:
                process.kill()
                await process.wait()
            raise
    output = browser_log.read_bytes()
    assert process.returncode == 0, output.decode(errors="replace")[-10000:]
    measurements = json.loads((context.artifacts_dir / "measurements.json").read_text(encoding="utf-8"))
    clock_alignment = measurements["clock_alignment"]
    assert clock_alignment["method"].startswith("performance.timeOrigin + performance.now()")
    assert {sample["label"] for sample in clock_alignment["samples"]} == {"run_before", "run_after"}
    for bracket in clock_alignment["samples"]:
        for point in (bracket["node_before"], bracket["browser"], bracket["node_after"]):
            assert all(math.isfinite(point[field]) for field in ("time_origin_ms", "now_ms", "epoch_ms"))
            assert point["epoch_ms"] == point["time_origin_ms"] + point["now_ms"]
        assert bracket["node_after"]["epoch_ms"] >= bracket["node_before"]["epoch_ms"]
    file_samples = measurements["mutation_samples"]
    assert sorted(sample["kind"] for sample in file_samples) == ["copy", "directory", "file", "paste", "upload"]
    assert len(file_samples) == 5
    cross_page = measurements["cross_page_observation"]
    assert len(cross_page["samples"]) == 2
    assert cross_page["distinct_operation_ids"] == 2
    assert cross_page["cross_page_dispatch_count"] == sum(
        sample["cross_page_dispatched"] for sample in cross_page["samples"]
    )
    assert {sample["name"] for sample in cross_page["samples"]} == {
        "cross_page_a.txt", "cross_page_b.txt",
    }
    assert len({sample["initiating_page"] for sample in cross_page["samples"]}) == 2
    assert {sample["initiating_page"] for sample in cross_page["samples"]} == set(
        cross_page["attached_page_ids"]
    )
    assert all(sample["accepted_state"] in {"queued", "running", "committed"}
        and sample["terminal_state"] == "committed" and sample["terminal_effect"] == "complete"
        and sample["confirmation_ui_completed"] and sample["initiating_ui_path_visible"] and sample["physical_path"]
        == str(context.workspace_root / "concurrent" / sample["name"])
        and Path(sample["physical_path"]).is_file()
        and hashlib.sha256(Path(sample["physical_path"]).read_bytes()).hexdigest()
        == sample["physical_sha256"] for sample in cross_page["samples"])
    assert all(len(sample["mutation_attempts"]) >= 1
        and any(attempt["outcome"] == "accepted" for attempt in sample["mutation_attempts"])
        and len({attempt["browser_request_id"] for attempt in sample["mutation_attempts"]})
        == len(sample["mutation_attempts"])
        and len({attempt["proxy_exchange_id"] for attempt in sample["mutation_attempts"]})
        == len(sample["mutation_attempts"])
        and all(attempt["owner_preimage_sha256"] for attempt in sample["mutation_attempts"])
        and len({attempt["owner_preimage_sha256"] for attempt in sample["mutation_attempts"]}) == 1
        for sample in cross_page["samples"])
    assert cross_page["cross_page_confirmation_count"] == 2
    assert len(cross_page["terminal_handoffs"]) == 2
    assert {item["operation_id"] for item in cross_page["terminal_handoffs"]} == {
        sample["operation_id"] for sample in cross_page["samples"]
    }
    assert len(cross_page["completion_identity_guards"]) == 2
    assert all(not item["wrong_operation_accepted"] and not item["out_of_order_accepted"]
        for item in cross_page["completion_identity_guards"])
    assert all(item["observation_source"] == "mounted_mutation_observer"
        and item["snapshot_index"] >= 0 and item["event_index"] >= 0
        and item["client_sequence"] >= 1 and item["queue_seq"] >= 1
        and item["receipt_revision"] >= 1
        and item["snapshot_index"] >= item["baseline_snapshot_offset"]
        and item["event_index"] >= item["baseline_event_offset"]
        for item in cross_page["completion_identity_guards"])
    assert all(item["release_condition"] == "peer-frozen-operation-dom-snapshot"
        and item["observation_source"] == "mounted_mutation_observer"
        and item["operation_id_from_snapshot"] == item["operation_id"]
        and item["client_sequence"] >= 1 and item["queue_seq"] >= 1 and item["receipt_revision"] >= 1
        and item["snapshot_index"] >= 0 and item["event_index"] >= 0
        and item["snapshot_index"] >= item["baseline_snapshot_offset"]
        and item["event_index"] >= item["baseline_event_offset"]
        for item in cross_page["terminal_query_holds"])
    sample_by_operation = {sample["operation_id"]: sample for sample in cross_page["samples"]}
    assert all(item["operation_id"] in sample_by_operation
        for item in cross_page["terminal_query_holds"])
    for sample in cross_page["samples"]:
        assert sample["confirmation_page"] != sample["initiating_page"]
        handoff = next(item for item in cross_page["terminal_handoffs"] if item["operation_id"] == sample["operation_id"])
        assert handoff["initiating_page"] == sample["initiating_page"]
        assert handoff["completion_page"] == sample["confirmation_page"]
        assert handoff["response_url"] == sample["ui_completion"]["event"]["terminal"]["response_url"]
        assert handoff["receipt_ordinal"] < handoff["completion_ordinal"]
        query_holds = [item for item in cross_page["terminal_query_holds"]
            if item["operation_id"] == sample["operation_id"]]
        for query_hold in query_holds:
            assert query_hold["completion_page"] == sample["confirmation_page"]
            assert query_hold["target_path_title"] == str(
                context.workspace_root / "concurrent" / sample["name"]
            )
    for sample in [*file_samples, *cross_page["samples"]]:
        assert sample["ui_completion"]["message"] == "文件操作已完成"
        snapshot = sample["ui_completion"]
        assert snapshot["observation_source"] == "mounted_mutation_observer"
        assert snapshot["observed_dom_text"] == snapshot["message"]
        assert snapshot["target_row_visible"] is True
        assert snapshot["operation_id"] == sample["operation_id"]
        assert snapshot["client_sequence"] == sample["client_sequence"]
        assert snapshot["queue_seq"] == sample["accepted_queue_seq"]
        assert snapshot["receipt_revision"] == sample["receipt_revision"]
        assert snapshot["target_path_title"] == str(
            context.workspace_root / sample["directory"] / sample["name"]
        )
        assert snapshot["snapshot_index"] >= snapshot["baseline_snapshot_offset"]
        assert snapshot["event_index"] >= snapshot["baseline_event_offset"]
        assert snapshot["completed_count"] > snapshot["baseline_completed_before"]
        assert snapshot["browser_epoch_ms"] == snapshot["browser_time_origin_ms"] + snapshot["browser_now_ms"]
        assert math.isfinite(sample["elapsed_ms"]) and sample["elapsed_ms"] >= 0
        assert sample["elapsed_ms"] == snapshot["browser_epoch_ms"] - sample["ui_elapsed_clock"]["node"]["start_epoch_ms"]
        assert sample["ui_elapsed_clock"]["browser"]["endpoint_epoch_ms"] == snapshot["browser_epoch_ms"]
        assert sample["ui_elapsed_clock"]["node"]["observed_after_snapshot"]["epoch_ms"] >= 0
        assert sample["fs_window"]["endpoint_epoch_ms"] == snapshot["browser_epoch_ms"]
        assert sample["fs_window"]["deadline_epoch_ms"] == snapshot["browser_epoch_ms"] + 650
        assert sample["fs_window"]["timer_observed_epoch_ms"] >= sample["fs_window"]["deadline_epoch_ms"]
        assert sample["fs_window"]["observed_epoch_ms"] >= sample["fs_window"]["endpoint_epoch_ms"]
        event = sample["ui_completion"]["event"]
        terminal = event["terminal"]
        assert event["attribution_error"] is None and event["message"] == "文件操作已完成"
        assert snapshot["event_index"] == sample["ui_completion"]["event_index"]
        assert snapshot["event"]["ordinal"] == event["ordinal"]
        assert terminal["operation_id"] == sample["operation_id"]
        assert terminal["client_sequence"] == sample["client_sequence"]
        assert terminal["queue_seq"] == sample["accepted_queue_seq"]
        assert terminal["receipt_revision"] == sample["receipt_revision"]
        assert terminal["state"] == "committed" and terminal["effect"] == "complete"
        assert terminal["ordinal"] < event["ordinal"] and terminal["at_ms"] <= event["at_ms"]
        assert sample["confirmation_page"] >= 1
        assert snapshot["event_index"] >= 0
        expected_key = {"workspace_id": session["workspace_id"], "scope": "workspace",
            "root": str(context.workspace_root), "path": sample["directory"]}
        assert sample["directory_key"] == sample["target_directory_key"] == sample["request_directory_key"] == expected_key
        assert sample["owner_preimage_sha256"]
        assert sample["owner_preimage"]["directory_key"] == expected_key
        assert sample["owner_preimage"]["intent"]["client_operation_id"] == sample["operation_id"]
        assert sample["owner_preimage"]["intent"]["client_sequence"] == sample["client_sequence"]
        assert all(attempt["owner_preimage_sha256"] == sample["owner_preimage_sha256"]
            for attempt in sample["mutation_attempts"])
        assert sample["accepted_queue_seq"] >= 1
        for attempt in sample["mutation_attempts"]:
            if attempt["outcome"] == "accepted":
                assert attempt["browser_network_state"] == "response_completed"
                assert attempt["response_observation_state"] == "completed"
                assert attempt["browser_state"] == attempt["proxy_state"] == "response_completed"
                assert attempt["browser_status"] == attempt["proxy_response_status"] == 202
                assert attempt["response_error"] is None
                assert attempt["receipt"]["operation_id"] == sample["operation_id"]
                assert attempt["receipt"]["client_sequence"] == sample["client_sequence"]
                assert attempt["receipt"]["queue_seq"] == sample["accepted_queue_seq"]
    expected_directory_key = {"workspace_id": session["workspace_id"], "scope": "workspace",
        "root": str(context.workspace_root), "path": "d000"}
    for sample in file_samples:
        assert sample["target_directory_key"] == expected_directory_key
        assert sample["request_directory_key"] == expected_directory_key
        assert sample["receipt_item_path"] == sample["name"]
        assert sample["receipt_item_kind"] == ("directory" if sample["kind"] == "directory" else "file")
        assert sample["snapshot_path"] == "d000"
        assert sample["snapshot_item_path"] == f"d000/{sample['name']}"
        assert sample["terminal_state"] == "committed" and sample["terminal_effect"] == "complete"
        assert sample["request_has_local_token"] and sample["request_workspace_id"] == workspace_id
        assert sample["initiating_ui_path_visible"] and sample["confirmation_ui_completed"]
        assert sample["mutation_attempt_count"] == len(sample["mutation_attempts"]) >= 1
        assert sample["owner_preimage_sha256"]
        assert all(attempt["owner_preimage_sha256"] == sample["owner_preimage_sha256"]
            for attempt in sample["mutation_attempts"])
        assert any(attempt["outcome"] == "accepted" and attempt["receipt"]["operation_id"]
            == sample["operation_id"] and attempt["receipt"]["client_sequence"]
            == sample["client_sequence"] for attempt in sample["mutation_attempts"])
        physical_path = Path(sample["physical_path"])
        if sample["kind"] == "directory":
            assert physical_path.is_dir()
        else:
            assert physical_path.is_file()
            expected_content = "正式量化上传内容\n" if sample["kind"] == "upload" else (
                "正式量化复制内容\n" if sample["kind"] in {"copy", "paste"} else ""
            )
            assert physical_path.read_text(encoding="utf-8") == expected_content
    gateway_samples = measurements["gateway_samples"]
    assert sorted(sample["kind"] for sample in gateway_samples) == ["create_folder", "move_workspace"]
    assert len(gateway_samples) == 2
    assert all(sample["request_has_local_token"] for sample in gateway_samples)
    assert all(sample["returned_navigation_node"] and sample["ui_node"] for sample in gateway_samples)
    persisted_navigation = await client.get("/api/gateway/workspace-navigation")
    assert persisted_navigation.status_code == 200, persisted_navigation.text
    persisted_nodes = persisted_navigation.json()["data"]["nodes"]
    created_folder = next(node for node in persisted_nodes if node["name"] == "gateway_measure_0")
    moved_workspace = next(node for node in persisted_nodes if node.get("workspace_id") == workspace_id)
    assert created_folder["kind"] == "workspace_folder" and created_folder.get("parent_node_id") is None
    assert moved_workspace["parent_node_id"] == folders[0]
    print(json.dumps({"file_samples": file_samples, "gateway_samples": gateway_samples,
        "cross_page_observation": cross_page, "formal_artifacts": str(context.artifacts_dir)}, ensure_ascii=False))
    context.write_result("passed", {"file_sample_count": 5, "gateway_sample_count": 2,
        "cross_page_dispatch_count": cross_page["cross_page_dispatch_count"]})


@pytest.mark.asyncio
async def test_navigation_observation_requests_use_fixture_credentials(
    request: pytest.FixtureRequest,
    navigation_measurement_stack: tuple[TestRunContext, httpx.AsyncClient],
) -> None:
    context, client = navigation_measurement_stack
    workspace_response = await client.get("/api/gateway/workspaces")
    assert workspace_response.status_code == 200, workspace_response.text
    backend_response = await client.get("/api/v1/workspace")
    assert backend_response.status_code == 200, backend_response.text
    configuration = {
        "gatewayUrl": str(client.base_url),
        "workspaceId": workspace_response.json()["data"]["active_workspace_id"],
        "backendWorkspaceId": backend_response.json()["data"]["workspace_id"],
        "cookies": [{"name": cookie.name, "value": cookie.value} for cookie in client.cookies.jar],
        "observationHeaders": dict(LOCAL_TOKEN_HEADERS),
    }
    artifacts = context.runtime_root_for_node(request.node.nodeid) / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=False)
    configuration_path = artifacts / "configuration.json"
    configuration_path.write_text(
        json.dumps(configuration, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    browser_log = artifacts / "browser.raw.log"
    with browser_log.open("xb") as stream:
        process = await asyncio.create_subprocess_exec(
            "bun", "tests/integration/clients/web/navigation_mutation_measurements.mjs",
            str(configuration_path), str(artifacts), "--observation-only",
            stdout=stream, stderr=asyncio.subprocess.STDOUT,
        )
        try:
            await asyncio.wait_for(process.wait(), 60)
        except BaseException:
            if process.returncode is None:
                process.kill()
                await process.wait()
            raise
    assert process.returncode == 0, browser_log.read_text(encoding="utf-8")[-10000:]
    observation = json.loads((artifacts / "observation-requests.json").read_text(encoding="utf-8"))
    assert not observation["observation_headers_exposed_to_browser"]
    records = observation["observation_requests"]
    assert [(record["runtime"], record["status"]) for record in records] == [
        ("backend", 200), ("gateway", 200),
    ]
    assert [record["path"] for record in records] == [
        "/api/v1/test/navigation-measurements/fs-reads",
        "/api/gateway/test/navigation-measurements/fs-reads",
    ]
    print(json.dumps({"observation_requests": records, "formal_artifacts": str(artifacts)}, ensure_ascii=False))
