"""真实 factory 冻结的 activation 正文：单次读取共用快照及已校验摘要。"""

import json
from pathlib import Path

import pytest
import rfc8785

from tests.harness.python.run_context import TestRunContext
from tests.integration.backend.sessions.rollout_context.test_agent_production_context import (
    factory_context as factory_context,  # noqa: PLC0414 - 真实 factory/Bridge/seal
)
from tests.integration.backend.sessions.rollout_context.test_context_plan_registry import (
    registry_db as registry_db,  # noqa: PLC0414 - 当前文件隔离正式工作区
)


@pytest.mark.parametrize("read_count", [1, 8])
def test_activation_payload_read_reuses_its_snapshot_and_verified_digest(
    factory_context, monkeypatch, request, read_count,
):
    saver, session, thread, _connection, _selection, _manager, _captured, _registered, dispatch, _files = factory_context
    dispatch("payload-read-initial")
    sealed = saver._storage.assemblies.list_context_assemblies(session, thread)[-1]
    activation = saver.load_sealed_resource_activation(
        session, thread_id=thread, assembly_id=sealed.assembly_id,
        plan_hash=sealed.plan_hash, request_hash=sealed.request_hash,
    )
    binding = next(binding for binding in activation.bindings
                   if binding.resource_kind == "skill" and binding.facet == "activation")
    expected = saver.read_resource_activation_payload(activation, binding, storage_owner=(session, thread))
    storage = saver._storage.storage
    connect, open_snapshot = storage._connect, storage.open_read_snapshot
    manifest, read = saver._storage.assemblies.get_context_plan_detail, saver._detail_store.read
    encode = rfc8785.dumps
    calls, active = [], []

    def observe_connect(*args, **kwargs):
        if active:
            active[-1]["connections"] += 1
        return connect(*args, **kwargs)

    def observe_open(*args, **kwargs):
        snapshot = open_snapshot(*args, **kwargs)
        if active:
            active[-1]["opened"].append(snapshot)
        return snapshot

    def observe_manifest(*args, **kwargs):
        if active:
            active[-1]["manifests"].append(kwargs.get("snapshot"))
        return manifest(*args, **kwargs)

    def observe_read(*args, **kwargs):
        if active:
            active[-1]["bodies"].append(kwargs.get("snapshot"))
        return read(*args, **kwargs)

    def observe_encode(value):
        if active and value == expected:
            active[-1]["payload_encodings"] += 1
        return encode(value)

    monkeypatch.setattr(storage, "_connect", observe_connect)
    monkeypatch.setattr(storage, "open_read_snapshot", observe_open)
    monkeypatch.setattr(saver._storage.assemblies, "get_context_plan_detail", observe_manifest)
    monkeypatch.setattr(saver._detail_store, "read", observe_read)
    monkeypatch.setattr(rfc8785, "dumps", observe_encode)
    for _ in range(read_count):
        call = {"connections": 0, "opened": [], "manifests": [], "bodies": [], "payload_encodings": 0}
        calls.append(call)
        active.append(call)
        try:
            value = saver.read_resource_activation_payload(activation, binding, storage_owner=(session, thread))
        finally:
            active.pop()
        assert value == expected and value is not expected
    measurements = [{
        "connections": call["connections"], "snapshots": len(call["opened"]),
        "manifest_snapshots": [None if snapshot is None else call["opened"].index(snapshot) for snapshot in call["manifests"]],
        "body_snapshots": [None if snapshot is None else call["opened"].index(snapshot) for snapshot in call["bodies"]],
        "payload_encodings": call["payload_encodings"],
    } for call in calls]
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    (context.artifacts_dir / f"activation-payload-read-{read_count}.json").write_text(
        json.dumps(measurements, indent=2), encoding="utf-8",
    )
    print(f"activation payload work ({read_count} reads): {measurements}")
    assert len({id(snapshot) for call in calls for snapshot in call["opened"]}) == read_count
    for call in calls:
        assert len(call["opened"]) == 1 and call["opened"][0].closed
        assert call["connections"] == 1
        assert call["manifests"] == call["bodies"] == call["opened"]
        # parser 逐次重算正文摘要；外层只比对已验证字段，不再次编码同一正文。
        assert call["payload_encodings"] == 1
