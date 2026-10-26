"""候选应用必须保留当次消费者证明和对象，不能重新构造旧配置来回退。"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from fastapi import FastAPI

from app.gateway.config import GatewayConfigReloadService, load_gateway_config
from app.gateway.control.gateway_state import GatewayStateStore
from app.gateway.federation.policy import FederationPolicyStore
from app.gateway.runtime_proof import _apply_gateway_runtime_config
from app.services.infrastructure.config.state import ConfigConflictError
from tests.harness.python.run_context import TestRunContext


@dataclass(frozen=True)
class ConfigApplicationCase:
    app: FastAPI
    service: GatewayConfigReloadService
    store: GatewayStateStore
    path: Path
    policy: FederationPolicyStore


@pytest.fixture
def config_application(request: pytest.FixtureRequest) -> Iterator[ConfigApplicationCase]:
    context = TestRunContext.from_test_file(Path(__file__)).prepare()
    root = context.boxteam_home_for_node(request.node.nodeid)
    root.mkdir(parents=True, exist_ok=True)
    path = root / "gateway.jsonc"
    path.write_text(json.dumps({"config_version": 3}), encoding="utf-8")
    with GatewayStateStore(path=root / "gateway.sqlite") as store:
        schema = context.project_root / "configs/gateway_schema.jsonc"
        config = load_gateway_config(config_path=path, local_config_path=root / "gateway_local.jsonc", schema_path=schema, state_store=store)
        app = FastAPI()
        app.state.gateway_config = config
        policy = FederationPolicyStore(initial={})
        app.state.federation_policy_store = policy
        service = GatewayConfigReloadService(
            state_store=store, config=config, config_path=path, local_config_path=root / "gateway_local.jsonc", schema_path=schema,
            on_runtime_config=lambda candidate, previous, token, check: _apply_gateway_runtime_config(
                app, candidate, previous, token, check,
            ),
        )
        service.initialize_active_snapshot()
        yield ConfigApplicationCase(app, service, store, path, policy)


@pytest.mark.asyncio
@pytest.mark.parametrize("conflict", [False, True])
async def test_reload_persists_real_proofs_and_compensates_the_frozen_consumers(
    config_application: ConfigApplicationCase, monkeypatch: pytest.MonkeyPatch, conflict: bool,
) -> None:
    case = config_application
    initial = case.service.config
    baseline = case.store.get_active_config_snapshot("gateway")
    replacement = FederationPolicyStore(initial={"default_effect": "deny"})
    case.path.write_text(json.dumps({
        "config_version": 3, "permissions": {"federation": {"default_effect": "deny"}},
    }), encoding="utf-8")

    def reject_promotion(**kwargs):
        assert case.policy.snapshot.default_effect == "deny"
        case.app.state.federation_policy_store = replacement
        raise ConfigConflictError("最终 CAS 冲突")

    if conflict:
        monkeypatch.setattr(case.store, "promote_active_config_snapshot", reject_promotion)
        with pytest.raises(ConfigConflictError, match="stage=promotion"):
            await case.service.reload()
        assert case.app.state.gateway_config is initial
        assert case.service.config is initial
        assert case.policy.snapshot.default_effect == "allow"
        assert replacement.snapshot.default_effect == "deny"
        assert case.store.get_active_config_snapshot("gateway") == baseline
    else:
        await case.service.reload()
        assert case.policy.snapshot.default_effect == "deny"
        assert case.app.state.gateway_config is case.service.config
        assert case.service.config.revision != initial.revision

    event = case.service.list_events()[-1]
    assert event.apply_id is not None
    journal = case.store.get_config_apply_journal(apply_id=event.apply_id)
    assert journal is not None and journal.state == ("compensated" if conflict else "committed")
    applied = next(effect for effect in journal.side_effects if effect["action"] == "apply")
    proofs = applied["consumer_proofs"]
    assert isinstance(proofs, list)
    assert {proof["consumer_id"] for proof in proofs} == {"federation-policy", "gateway-config-snapshot"}
    assert len({proof["generation"] for proof in proofs}) == 1
    assert all(proof["state"] == "healthy" and len(proof["fencing_token_digest"]) == 64 for proof in proofs)
