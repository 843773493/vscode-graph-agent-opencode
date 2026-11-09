"""验证采集的开关、真实HTTP计时和Gateway配置边界。"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI, Request

from app.core.runtime_observation import RuntimeObservation
from app.core.trace_middleware import TraceMiddleware
from app.gateway.config import load_gateway_config
from app.schemas.runtime_observation import RuntimeObservationSettings
from configs.runtime import read_jsonc_object, validate_config


@pytest.fixture
def observation_settings() -> RuntimeObservationSettings:
    return RuntimeObservationSettings.model_validate({
        "telemetry": {"enabled": True, "sample_buffer_limit": 2},
        "statistics": {"enabled": True, "collection_interval_ms": 1000},
    })


async def test_disabled_collection_does_no_work_and_hot_disable_releases_resources(
    observation_settings: RuntimeObservationSettings,
) -> None:
    collections = []

    def processes():
        collections.append(True)
        return [("gateway", os.getpid())]

    disabled = RuntimeObservationSettings.model_validate({"telemetry": {}, "statistics": {}})
    async with RuntimeObservation(instance_id="gateway-instance", revision="off", settings=disabled,
                                  processes=processes) as owner:
        owner.record_request(status_code=200, duration_ms=1, failed=False, proxy=True)
        assert collections == [] and owner._task is None and owner._samples is None
        assert owner.status().requests is None and owner.status().samples == []
        await owner.apply(observation_settings, "on")
        task = owner._task
        for status in (200, 201, 500):
            owner.record_request(status_code=status, duration_ms=2, failed=status == 500, proxy=True)
        snapshot = owner.status()
        assert snapshot.applied_revision == "on"
        assert snapshot.requests is not None
        assert (snapshot.requests.count, snapshot.requests.proxy_count, snapshot.requests.errors,
                snapshot.requests.proxy_errors, snapshot.requests.duration_ms) == (3, 3, 1, 1, 6)
        assert [sample.status_code for sample in snapshot.samples] == [201, 500]
        resource, = snapshot.processes
        assert resource.pid == os.getpid() and resource.rss_bytes > 0 and resource.cpu_seconds >= 0
        await owner.apply(disabled, "off-again")
        assert task is not None and task.done() and owner._samples is None
        before = len(collections)
        await asyncio.sleep(1.05)
        owner.record_request(status_code=500, duration_ms=5, failed=True, proxy=True)
        assert len(collections) == before
        state = owner.status()
        assert state.applied_revision == "off-again" and state.requests is None
        assert state.processes == [] and state.samples == []
        # 交给诊断消费者的旧快照不随后续采集而变。
        assert snapshot.requests.count == 3


@pytest.mark.parametrize(
    ("payload", "status_code", "expect_samples", "expect_requests", "expect_sample_buffer", "expect_timer"),
    [
        pytest.param(
            {"telemetry": {"enabled": True}, "statistics": {"enabled": False}},
            200, [200], False, True, False, id="telemetry-only",
        ),
        pytest.param(
            {"telemetry": {"enabled": False}, "statistics": {"enabled": True}},
            500, [], True, False, True, id="statistics-only",
        ),
        pytest.param(
            {"telemetry": {"enabled": True, "sample_ratio": 0}, "statistics": {"enabled": False}},
            201, [], False, True, False, id="zero-sample-ratio",
        ),
    ],
)
async def test_telemetry_and_statistics_switch_independently(
    payload: dict[str, object],
    status_code: int,
    expect_samples: list[int],
    expect_requests: bool,
    expect_sample_buffer: bool,
    expect_timer: bool,
) -> None:
    settings = RuntimeObservationSettings.model_validate(payload)
    async with RuntimeObservation(
        instance_id="independent-switches",
        revision="r1",
        settings=settings,
        processes=list,
    ) as owner:
        owner.record_request(status_code=status_code, duration_ms=1.5, failed=status_code >= 400, proxy=False)
        state = owner.status()

        assert [sample.status_code for sample in state.samples] == expect_samples
        assert (state.requests is not None) is expect_requests
        assert (owner._samples is not None) is expect_sample_buffer
        assert (owner._task is not None) is expect_timer
        if state.requests is not None:
            assert (state.requests.count, state.requests.errors) == (1, int(status_code >= 400))


async def test_trace_reports_http_errors_without_collecting_business_input(
    observation_settings: RuntimeObservationSettings,
) -> None:
    app = FastAPI()
    async with RuntimeObservation(instance_id="http-instance", revision="r1", settings=observation_settings,
                                  processes=list) as owner:
        app.add_middleware(TraceMiddleware, on_request_complete=lambda scope, status, duration, failed:
            owner.record_request(status_code=status, duration_ms=duration, failed=failed,
                                 proxy=scope["state"].get("proxy_request", False)))

        @app.get("/ok")
        async def ok(request: Request):
            request.state.proxy_request = True
            return {"secret": "not-in-telemetry"}

        @app.get("/failure")
        async def failure():
            raise RuntimeError("not-in-telemetry")

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            success = await client.get("/ok?token=not-in-telemetry", headers={"X-Request-ID": "same-request"})
            assert success.headers["X-Request-ID"] == "same-request"
            assert (await client.get("/failure")).status_code == 500
        state = owner.status()
        assert state.requests.count == 2 and state.requests.errors == 1 and state.requests.proxy_count == 1
        assert [sample.failed for sample in state.samples] == [False, True]
        assert all(sample.duration_ms >= 0 for sample in state.samples)
        assert "not-in-telemetry" not in state.model_dump_json()


def test_gateway_observation_uses_original_config_layers(tmp_path: Path) -> None:
    user, local = tmp_path / "gateway.jsonc", tmp_path / "gateway_local.jsonc"
    user.write_text(json.dumps({"telemetry": {"runtime": {"enabled": True, "sample_buffer_limit": 8}}}))
    local.write_text(json.dumps({"telemetry": {"runtime": {"sample_ratio": 0.25}},
                                 "statistics": {"runtime": {"enabled": True}}}))
    config = load_gateway_config(config_path=user, local_config_path=local,
                                 schema_path=Path.cwd() / "configs/gateway_schema.jsonc")
    settings = RuntimeObservationSettings.from_gateway_config(config.payload)
    assert settings.telemetry.model_dump() == {"enabled": True, "sample_ratio": 0.25, "sample_buffer_limit": 8}
    assert settings.statistics.model_dump() == {"enabled": True, "collection_interval_ms": 5000}


@pytest.mark.parametrize(("domain", "field", "value"), [
    ("telemetry", "sample_ratio", -0.01), ("telemetry", "sample_ratio", 1.01),
    ("telemetry", "sample_buffer_limit", 0), ("telemetry", "sample_buffer_limit", 4097),
    ("statistics", "collection_interval_ms", 999), ("statistics", "collection_interval_ms", 60001),
])
def test_observation_schema_rejects_out_of_bounds_settings(domain: str, field: str, value: float) -> None:
    payload = read_jsonc_object(Path.cwd() / "configs/gateway_inline.jsonc")
    payload[domain]["runtime"][field] = value
    with pytest.raises(ValueError):
        validate_config(payload, schema_path=Path.cwd() / "configs/gateway_schema.jsonc")


@pytest.mark.parametrize("domain", ["telemetry", "statistics"])
def test_workspace_cannot_override_gateway_collection(domain: str) -> None:
    payload = read_jsonc_object(Path.cwd() / "configs/workspace_inline.jsonc")
    payload[domain] = {"runtime": {"enabled": True}}
    with pytest.raises(ValueError):
        validate_config(payload, schema_path=Path.cwd() / "configs/workspace_schema.jsonc")
