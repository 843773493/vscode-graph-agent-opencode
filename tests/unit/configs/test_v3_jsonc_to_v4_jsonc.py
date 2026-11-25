"""授权的完整 v3 配置和冻结快照通过原维护入口转换，不改写会话业务数据。"""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path

import pytest

from app.core.config_sources import config_revision
from app.core.workspace_identity import load_or_create_workspace_id
from app.gateway.config.loader import load_gateway_config
from app.gateway.control.gateway_state import GatewayStateStore
from app.services.infrastructure.config_service import ConfigService
from app.services.infrastructure.workspace_state_store import WorkspaceStateStore
from configs.maintenance.runtime_logging_commit import commit_runtime_logging_migration
from configs.migrations.workspace.v3_jsonc_to_v4_jsonc import (
    migrate_v3_jsonc_to_v4_jsonc,
)
from configs.runtime import read_jsonc_object


def _write(path: Path, payload: dict[str, object]) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = ('// 原始注释\n' + json.dumps(payload, ensure_ascii=False) + '\n').encode()
    path.write_bytes(content)
    return hashlib.sha256(content).hexdigest()


@pytest.fixture
def legacy_workspace_config() -> dict[str, object]:
    inline = read_jsonc_object(Path.cwd() / 'configs/workspace_inline.jsonc')
    payload = deepcopy(inline['settings']['workspace'])
    for definition in payload['llm']['providers'].values():
        name = definition.pop('config_ref').split('/')[-1]
        definition['defaults'] = deepcopy(inline['profiles'][name]['settings']['model_options'])
        for model in definition['models'].values():
            model.pop('config_ref')
    payload['llm']['providers']['primary']['defaults']['api_key'] = '${MIGRATION_TEST_KEY}'
    payload['llm']['providers']['primary']['defaults']['request_options']['no_proxy'] = True
    payload['config_version'] = 3
    return payload


@pytest.fixture
def migration_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    home, workspace = tmp_path / 'home', tmp_path / 'workspace'
    workspace.mkdir()
    monkeypatch.setenv('BOXTEAM_HOME', str(home))
    load_or_create_workspace_id(workspace)
    return home, workspace


def test_conversion_preserves_stable_ids_keys_parameters_and_model_override(legacy_workspace_config) -> None:
    payload = legacy_workspace_config
    payload['llm']['providers']['primary']['models']['custom/id'] = {
        'model': 'remote-model', 'overrides': {'request_options': {'parameters': {'temperature': 0.2}}},
    }
    original = deepcopy(payload)
    converted = migrate_v3_jsonc_to_v4_jsonc(payload, source_key='workspace.jsonc')
    assert payload == original
    provider = converted['settings']['workspace']['llm']['providers']['primary']
    assert set(provider['models']) == {'default', 'custom/id'}
    assert converted['profiles']['provider.primary']['settings']['network']['proxy']['mode'] == 'direct'
    assert converted['profiles']['provider.primary']['settings']['model_options']['api_key'] == '${MIGRATION_TEST_KEY}'
    assert provider['models']['custom/id']['config_ref'] == '#/profiles/model.primary.custom~1id'
    assert converted['profiles']['model.primary.custom/id']['settings']['model_options']['request_options']['parameters'] == {'temperature': 0.2}


def test_workspace_active_replays_frozen_values_after_file_changes_and_restores_exact_bytes(
    migration_paths, legacy_workspace_config,
) -> None:
    home, workspace = migration_paths
    user = home / 'config/workspace.jsonc'
    root = workspace / '.boxteam/workspace.jsonc'
    digest = _write(user, legacy_workspace_config)
    root_payload = {'config_version': 3, 'plugins': {'runtime': {'entries': []}}}
    root_digest = _write(root, root_payload)
    original = {path: path.read_bytes() for path in (user, root)}
    with WorkspaceStateStore(workspace_root=workspace) as store:
        for key, payload, value in [('workspace_mutable_override', legacy_workspace_config, digest),
                                    ('workspace_root_mutable_override', root_payload, root_digest)]:
            store.sync_config_source(config_key=key, uri=None, config_version=3, presence='present', payload=payload, layer_digest=value)
        store.ensure_active_config_snapshot(
            config_domain='workspace', payload=legacy_workspace_config,
            source_baseline={'workspace_root_mutable_override': {'layer_digest': root_digest}}, source_generation=0,
            layer_revisions={}, layer_digests={}, effective_digest=config_revision(legacy_workspace_config), secret_bindings={}, schema_version=3,
        )
    result = commit_runtime_logging_migration(boxteam_home=home, workspaces=(workspace,), project_root=Path.cwd())
    assert result['state'] == 'completed'
    assert read_jsonc_object(home / 'config/workspace_local.jsonc')['extends'] == 'user:workspace'
    with WorkspaceStateStore(workspace_root=workspace) as store:
        record = store.get_active_config_snapshot('workspace')
        assert record.schema_version == 4
        config = ConfigService(config_path=user, workspace_root=workspace)
        frozen = config._build_persisted_snapshot(record, loaded_source='active')
        model = config._llm_resolution.resolve_model_reference(frozen.graph, {'provider_id': 'primary', 'model_id': 'default'}, expand_credentials=False)
        assert model['api_key'] == '${MIGRATION_TEST_KEY}'
        assert model['network']['proxy']['mode'] == 'direct'
        # 源文件发生变动也不能改变已经冻结的 active。
        migrated_user = user.read_bytes()
        changed = read_jsonc_object(user)
        changed['profiles']['provider.primary']['settings']['model_options']['api_key'] = '${OTHER_KEY}'
        _write(user, changed)
        recovered = config._build_persisted_snapshot(record, loaded_source='active')
        assert recovered.revision == frozen.revision
        user.write_bytes(migrated_user)
    assert commit_runtime_logging_migration(boxteam_home=home, workspaces=(workspace,), project_root=Path.cwd())['state'] == 'unchanged'
    assert commit_runtime_logging_migration(boxteam_home=home, restore=True)['state'] == 'restored'
    assert {path: path.read_bytes() for path in original} == original
    assert not (home / 'config/workspace_local.jsonc').exists()


def test_gateway_active_and_pending_keep_distinct_frozen_configuration(migration_paths) -> None:
    home, _ = migration_paths
    source = home / 'config/gateway.jsonc'
    payload = deepcopy(read_jsonc_object(Path.cwd() / 'configs/gateway_inline.jsonc')['settings']['gateway'])
    payload['config_version'] = 5
    active = deepcopy(payload)
    active['logging']['runtime']['pretty'] = True
    payload['logging']['runtime']['pretty'] = False
    digest = _write(source, payload)
    with GatewayStateStore(path=home / 'state/gateway/gateway.sqlite') as store:
        store.sync_config_source(config_key='gateway_mutable_override', uri=None, config_version=5,
                                 presence='present', payload=payload, layer_digest=digest)
        baseline = {'gateway_mutable_override': {'layer_digest': 'prior-digest'}}
        record = store.ensure_active_config_snapshot(
            config_domain='gateway', payload=active, source_baseline=baseline, source_generation=0, layer_revisions={}, layer_digests={},
            effective_digest=config_revision(active), secret_bindings={}, schema_version=5,
        )
        store.create_pending_config_candidate(
            config_domain='gateway', candidate_id='pending-config', idempotency_key='pending-config', payload=payload,
            source_baseline=baseline, candidate_digest=config_revision(payload), effective_digest=config_revision(payload),
            target_generation=None, fencing_token=None, state='candidate_validated', base_active_revision=record.active_revision,
        )
        store.update_pending_config_candidate_state(config_domain='gateway', candidate_id='pending-config',
                                                    expected_state='candidate_validated', state='pending_restart')
    assert commit_runtime_logging_migration(boxteam_home=home, project_root=Path.cwd())['state'] == 'completed'
    with GatewayStateStore(path=home / 'state/gateway/gateway.sqlite') as store:
        loaded = load_gateway_config(config_path=source, local_config_path=source.with_name('gateway_local.jsonc'),
                                     entry_path=source.with_name('gateway_local.jsonc'), schema_path=home / 'config/gateway_schema.jsonc',
                                     state_store=store, startup=True)
        assert loaded.payload['logging']['runtime']['pretty'] is True
        pending = store.get_pending_config_candidate(config_domain='gateway')
        assert pending.payload['logging']['runtime']['pretty'] is False
        assert pending.source_baseline['gateway_local_mutable_override']['document']['settings']['gateway']['logging']['runtime']['pretty'] is False
