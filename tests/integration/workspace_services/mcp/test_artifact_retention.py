"""原安装事务、工作区配置 writer、连接代际与原 lease 的制品保留闭包。"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

import pytest

from app.core.lifecycle import LifetimeScope
from app.core.workspace_identity import load_or_create_workspace_id
from app.domain.itemized.identity.tool_call_identity import software_tool_operation_id
from app.domain.mcp_candidates import McpCandidateConflict
from app.schemas.internal_v2.mcp import (
    McpCandidateProvenanceDTO,
    McpCandidateVariantRefDTO,
    McpInstallationOperationDTO,
    McpInstallationTargetDTO,
    McpInstalledArtifactDTO,
    McpManagedLaunchDTO,
    McpUninstallationRequestDTO,
)
from app.services.infrastructure.config.state import ConfigConflictError
from app.services.infrastructure.events.event_channel_service import EventChannelService
from app.services.infrastructure.external_resource_leases import (
    ExternalResourceLeaseLedger,
)
from app.services.infrastructure.mcp import McpCatalogOwner
from app.services.infrastructure.mcp.candidate_source import native_mcp_platform
from app.services.infrastructure.mcp.installation.owner import McpInstallationOwner
from app.services.infrastructure.mcp.installation.python import artifact_digest
from app.services.infrastructure.mcp.installation.references import McpArtifactInUse
from app.services.infrastructure.mcp.installation.store import (
    InstallationStore,
    artifact_key,
)
from app.services.infrastructure.mcp.invocation import McpResourceUse
from app.services.infrastructure.workspace_state_store import WorkspaceStateStore
from tests.support.mcp.session_doubles import FakeMcpSessionFactory


@dataclass
class RetentionCase:
    home: Path
    owner: McpInstallationOwner
    store: InstallationStore
    workspaces: tuple[Path, Path]
    configs: tuple[WorkspaceStateStore, WorkspaceStateStore]
    ledgers: tuple[ExternalResourceLeaseLedger, ExternalResourceLeaseLedger]
    target: McpInstallationTargetDTO

    def publish(self, version: str = '1') -> McpInstalledArtifactDTO:
        operation = McpInstallationOperationDTO(operation_id=f'install-{version}', intent_digest=f'intent-{version}',
            status='running', stage='install', created_at=1, updated_at=1)
        artifact = McpInstalledArtifactDTO(
            binding=McpManagedLaunchDTO(backend_id=self.owner.backend_id, installation_id='a' * 32,
                generation='b' * 32, artifact_digest='sha256:jcs:v1:' + 'c' * 64),
            source=McpCandidateProvenanceDTO(kind='manual', name='retained-server', version=version),
            manifest_digest='sha256:jcs:v1:' + 'd' * 64,
            variant=McpCandidateVariantRefDTO(kind='package', index=0, digest='sha256:jcs:v1:' + 'e' * 64),
            platform=native_mcp_platform(), identifier='retained-server', version=version, entrypoint='server', command='reserved',
            lock_digest='sha256:' + 'f' * 64, installed_distributions={'retained-server': version})
        reservation = self.store.reserve(operation, key=artifact_key(artifact, identifier=artifact.identifier, version=version, entrypoint='server'))
        assert reservation.preparation is not None
        binding = artifact.binding.model_copy(update={'installation_id': reservation.preparation.installation_id, 'generation': reservation.preparation.generation})
        root = self.home / 'artifacts/mcp' / self.owner.backend_id / 'generations' / binding.installation_id / binding.generation
        command = root / '.venv' / ('Scripts/server.exe' if os.name == 'nt' else 'bin/server')
        command.parent.mkdir(parents=True)
        command.write_bytes(f'fixture executable {version}'.encode())
        (root / 'dependency').write_text(version, encoding='utf-8')
        artifact = artifact.model_copy(update={'binding': binding.model_copy(update={'artifact_digest': artifact_digest(root)}), 'command': str(command)})
        self.store.publish(operation, artifact)
        return artifact

    def request(self, artifact: McpInstalledArtifactDTO, operation_id: str = 'uninstall') -> McpUninstallationRequestDTO:
        return McpUninstallationRequestDTO(target=self.target, binding=artifact.binding, operation_id=operation_id)

    def source(self, index: int, payload: dict[str, object], key: str = 'workspace_root_mutable_override') -> None:
        self.configs[index].sync_config_source(config_key=key, uri=None, config_version=2,
            presence='present', payload=payload, layer_digest=json.dumps(payload, sort_keys=True))


@pytest.fixture
async def retention_case(tmp_path: Path) -> AsyncIterator[RetentionCase]:
    home = tmp_path / 'home'
    backend_id = '00000000-0000-4000-8000-000000000001'
    owner = McpInstallationOwner(home=home, backend_id=backend_id)
    workspaces = (tmp_path / 'A1', tmp_path / 'A2')
    configs = []
    ledgers = []
    for workspace in workspaces:
        workspace.mkdir()
        load_or_create_workspace_id(workspace)
        config = WorkspaceStateStore(workspace_root=workspace)
        ledger = ExternalResourceLeaseLedger(state_path=workspace / '.boxteam/resources.json')
        owner.register_workspace(workspace, ledger)
        config.bind_mcp_installations(owner)
        configs.append(config)
        ledgers.append(ledger)
    case = RetentionCase(home, owner, InstallationStore(home / 'artifacts/mcp' / backend_id), workspaces,
        (configs[0], configs[1]), (ledgers[0], ledgers[1]),
        McpInstallationTargetDTO(gateway_id='gateway_' + '2' * 32, backend_id=backend_id))
    try:
        yield case
    finally:
        await owner.close()
        for config in configs:
            config.close()


def payload(artifact: McpInstalledArtifactDTO, *, enabled: bool = False) -> dict[str, object]:
    return {'mcp': {'servers': {'same-server': {'enabled': enabled, 'transport': 'stdio', 'managed_launch': artifact.binding.model_dump()}}}}


async def test_disabled_shadowed_sources_active_and_unfinished_pending_survive_cold_lookup(retention_case: RetentionCase) -> None:
    case = retention_case
    artifact = case.publish()
    original = payload(artifact)
    case.source(0, original)
    case.source(1, original, 'workspace_mutable_override')
    case.source(1, {'mcp': {'servers': {}}}, 'workspace_root_mutable_override')
    case.configs[1].ensure_active_config_snapshot(config_domain='workspace', payload=original, source_baseline={},
        source_generation=1, layer_revisions={}, layer_digests={}, effective_digest='active', secret_bindings={}, schema_version=2)
    case.configs[1].create_pending_config_candidate(config_domain='workspace', candidate_id='unfinished', idempotency_key='pending',
        payload=original, source_baseline={}, candidate_digest='pending', effective_digest='pending', target_generation=None,
        fencing_token=None, state='candidate_validated')
    case.source(0, {'mcp': {'servers': {}}})
    before = (case.workspaces[1] / '.boxteam/resources.json').read_bytes()
    cold = McpInstallationOwner(home=case.home, backend_id=case.owner.backend_id)
    try:
        references = cold.references(artifact.binding.installation_id).references
        workspace_id = load_or_create_workspace_id(case.workspaces[1])
        assert {ref.kind for ref in references} == {'source_layer', 'active', 'pending'}
        assert {ref.workspace_id for ref in references} == {workspace_id}
        assert any(ref.source_key == 'workspace_mutable_override' for ref in references)
        with pytest.raises(McpArtifactInUse) as caught:
            await cold.uninstall(case.request(artifact), actual_target=case.target)
        assert caught.value.references.references == references
        assert cold.artifacts() == [artifact] and Path(artifact.command).is_file()
        assert (case.workspaces[1] / '.boxteam/resources.json').read_bytes() == before
    finally:
        await cold.close()


async def test_original_generation_and_unknown_invocation_retain_old_artifact(retention_case: RetentionCase) -> None:
    case = retention_case
    old, new = case.publish(), case.publish('2')
    config = payload(old, enabled=True)['mcp']
    factory = FakeMcpSessionFactory({'same-server': ['echo']})
    catalog = McpCatalogOwner(raw_config=config, workspace_root=case.workspaces[0], event_service=EventChannelService(),
        leases=case.ledgers[0], installation_owner=case.owner, session_factory=factory)
    scope = LifetimeScope('candidate')
    await catalog.start()
    try:
        async def reject_switch() -> None:
            rejected = LifetimeScope('rejected-version-switch')
            effects = []
            try:
                with pytest.raises(ConfigConflictError, match='未结清'):
                    await catalog.prepare_config(payload(new, enabled=True)['mcp'], rejected, effects.append)
                assert effects == []
                assert catalog.server_configuration('same-server').managed_launch == old.binding
                assert catalog.binding_snapshot() == snapshot
            finally:
                await rejected.close()

        snapshot = catalog.binding_snapshot()
        target = snapshot.resolve('mcp__same-server__echo')
        assert target.connection_ref is not None
        original = case.ledgers[0].get(target.connection_ref)
        assert original is not None and original.managed_launch == old.binding and original.mcp_server_id == 'same-server'
        with catalog.execution_handles.reserve_model_call(snapshot, session_id='session', thread_id='thread', model_call_id='model') as retain:
            retain([{'name': 'invoke_extension_tool', 'id': 'call', 'args': {'tool_name': target.target_id}}])
        use = McpResourceUse('holder', software_tool_operation_id('mcp_call', 'session', 'thread', 'model', 'call'), 'session', 'thread', 'model')
        with catalog.execution_handles.execution_target(target, use) as execution:
            execution.started = True
            await reject_switch()
            assert await execution.tool.ainvoke({'text': 'original-call'}) == 'echo:original-call'
        await reject_switch()
        cold = ExternalResourceLeaseLedger(state_path=case.workspaces[0] / '.boxteam/resources.json')
        record = cold.get(target.connection_ref)
        assert record is not None and record.managed_launch == old.binding and record.process is not None
        assert record.status == 'running' and not record.process.exit_verified
        lease, = cold.leases_for_resource(target.connection_ref)
        assert lease.status == 'reconcile_required' and lease.lease_id.startswith('mcp-invocation:')
        references = case.owner.references(old.binding.installation_id).references
        assert {(ref.kind, ref.connection_ref, ref.lease_id) for ref in references} == {
            ('connection', target.connection_ref, None), ('lease', target.connection_ref, lease.lease_id)}
        with pytest.raises(McpArtifactInUse):
            await case.owner.uninstall(case.request(old), actual_target=case.target)
        await case.ledgers[0].settle(lease.lease_id)
        assert catalog.server_configuration('same-server').managed_launch == old.binding
        with pytest.raises(McpArtifactInUse):
            await case.owner.uninstall(case.request(old), actual_target=case.target)
        publish, _ = await catalog.prepare_config(payload(new, enabled=True)['mcp'], scope, lambda _effect: None)
        publish()
        assert catalog.server_configuration('same-server').managed_launch == new.binding
        record = ExternalResourceLeaseLedger(state_path=case.workspaces[0] / '.boxteam/resources.json').get(target.connection_ref)
        assert record is not None and record.process is not None and record.process.exit_verified and record.status == 'stopped'
        assert case.owner.references(old.binding.installation_id).references == []
        await case.owner.uninstall(case.request(old), actual_target=case.target)
        await case.owner.close()
        assert case.owner.operation('uninstall').status == 'succeeded'
        assert not Path(old.command).exists() and Path(new.command).is_file()
    finally:
        await scope.close()
        await catalog.shutdown()


async def test_cleanup_failure_retains_artifact_even_after_verified_no_process(retention_case: RetentionCase) -> None:
    case = retention_case
    artifact = case.publish()
    original = FakeMcpSessionFactory({'same-server': ['echo']})
    @asynccontextmanager
    async def failing(server, notify, lifecycle):
        async with original(server, notify, lifecycle) as session:
            yield session
        raise RuntimeError('原 SDK 清理失败')
    catalog = McpCatalogOwner(raw_config=payload(artifact, enabled=True)['mcp'], workspace_root=case.workspaces[0],
        event_service=EventChannelService(), leases=case.ledgers[0], installation_owner=case.owner, session_factory=failing)
    await catalog.start()
    with pytest.raises(BaseExceptionGroup):
        await catalog.shutdown()
    references = case.owner.references(artifact.binding.installation_id).references
    assert any(ref.kind == 'connection' and ref.status == 'cleanup_failed' for ref in references)
    record, = case.ledgers[0].records_by_kind('mcp_connection')
    assert record.process is not None and record.process.exit_verified and record.cleanup_failed
    with pytest.raises(McpArtifactInUse):
        await case.owner.uninstall(case.request(artifact), actual_target=case.target)


@pytest.mark.parametrize('damage', ['missing_workspace', 'missing_database', 'corrupt_database', 'missing_ledger', 'corrupt_ledger'])
async def test_unverifiable_locator_never_means_no_references(retention_case: RetentionCase, damage: str) -> None:
    case = retention_case
    artifact = case.publish()
    root = case.workspaces[1]
    case.configs[1].close()
    if damage == 'missing_workspace':
        root.rename(root.with_name('external-move'))
    elif damage in {'missing_database', 'corrupt_database'}:
        database = root / '.boxteam/state/workspace.sqlite'
        if damage == 'missing_database':
            database.unlink()
        else:
            database.write_bytes(b'not sqlite')
    else:
        ledger = root / '.boxteam/resources.json'
        if damage == 'missing_ledger':
            ledger.unlink()
        else:
            ledger.write_bytes(b'not json')
    with pytest.raises(McpArtifactInUse) as caught:
        await case.owner.uninstall(case.request(artifact), actual_target=case.target)
    assert any(ref.kind == 'unavailable' for ref in caught.value.references.references)
    assert Path(artifact.command).is_file() and case.owner.artifacts() == [artifact]


async def test_partial_removal_failure_is_durable_and_new_intent_can_finish_cleanup(retention_case: RetentionCase, monkeypatch: pytest.MonkeyPatch) -> None:
    case = retention_case
    artifact = case.publish()
    def fail(root: Path) -> None:
        (root / 'dependency').unlink()
        raise PermissionError('实际目录删除失败')
    monkeypatch.setattr(case.owner, '_remove_directory', fail)
    await case.owner.uninstall(case.request(artifact), actual_target=case.target)
    await case.owner.close()
    operation = case.owner.operation('uninstall')
    assert operation.status == 'failed' and operation.error == '实际目录删除失败'
    assert case.owner.artifacts()[0].state == 'removal_failed'
    cold = McpInstallationOwner(home=case.home, backend_id=case.owner.backend_id)
    try:
        with pytest.raises(McpCandidateConflict, match='removal_failed'):
            cold.resolve(artifact.binding, verify=True)
        assert await cold.uninstall(case.request(artifact), actual_target=case.target) == operation
        assert Path(artifact.command).is_file()
        await cold.uninstall(case.request(artifact, 'explicit-cleanup'), actual_target=case.target)
        await cold.close()
        assert cold.operation('explicit-cleanup').status == 'succeeded' and cold.artifacts() == []
        assert cold.operation('install-1').status == 'succeeded'
    finally:
        await cold.close()


async def test_dead_uninstaller_only_reconciles_original_operation(retention_case: RetentionCase) -> None:
    case = retention_case
    artifact = case.publish()
    operation = McpInstallationOperationDTO(operation_id='lost-uninstall', intent_digest='remove-intent', kind='uninstall',
        status='running', stage='remove', created_at=1, updated_at=1)
    accepted, _ = case.store.reserve_uninstall(operation, artifact.binding, lambda: None)
    with sqlite3.connect(case.home / 'artifacts/mcp' / case.owner.backend_id / 'installations.sqlite') as connection:
        connection.execute('UPDATE operation_owners SET pid=?, started=? WHERE operation_id=?', (2147483647, 0, accepted.operation_id))
    cold = McpInstallationOwner(home=case.home, backend_id=case.owner.backend_id)
    try:
        observed = cold.operation(accepted.operation_id)
        assert observed.status == 'interrupted' and observed.artifact is not None and observed.artifact.state == 'removal_failed'
        assert Path(artifact.command).is_file()
        with pytest.raises(McpCandidateConflict):
            cold.artifact(artifact.binding)
    finally:
        await cold.close()


async def test_cancel_after_durable_admission_settles_without_deleting_or_replaying(retention_case: RetentionCase,
    monkeypatch: pytest.MonkeyPatch) -> None:
    case = retention_case
    artifact = case.publish()
    committed, release = threading.Event(), threading.Event()
    original = case.owner._store.reserve_uninstall

    def delayed(*args, **kwargs):
        result = original(*args, **kwargs)
        committed.set()
        if not release.wait(10):
            raise TimeoutError('准入测试未收到结算信号')
        return result

    monkeypatch.setattr(case.owner._store, 'reserve_uninstall', delayed)
    admission = asyncio.create_task(case.owner.uninstall(case.request(artifact), actual_target=case.target))
    try:
        assert await asyncio.to_thread(committed.wait, 5)
        admission.cancel()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await admission
    interrupted = case.owner.operation('uninstall')
    assert interrupted.status == 'interrupted' and interrupted.artifact is not None and interrupted.artifact.state == 'removal_failed'
    assert Path(artifact.command).is_file()
    assert await case.owner.uninstall(case.request(artifact), actual_target=case.target) == interrupted
    assert Path(artifact.command).is_file()
    await case.owner.uninstall(case.request(artifact, 'explicit-cleanup'), actual_target=case.target)
    await case.owner.close()
    assert case.owner.operation('explicit-cleanup').status == 'succeeded' and not Path(artifact.command).exists()


async def test_conflicting_binding_in_original_config_cannot_be_mistaken_for_no_reference(retention_case: RetentionCase) -> None:
    case = retention_case
    artifact = case.publish()
    case.source(0, payload(artifact))
    damaged = artifact.model_copy(update={'binding': artifact.binding.model_copy(update={'generation': '0' * 32})})
    with case.configs[0].connection() as connection:
        connection.execute('UPDATE config_source_layers SET payload_json=?', (json.dumps(payload(damaged)),))
    with pytest.raises(McpArtifactInUse) as caught:
        await case.owner.uninstall(case.request(artifact), actual_target=case.target)
    assert any(ref.kind == 'unavailable' for ref in caught.value.references.references)
    assert Path(artifact.command).is_file()


_WRITER = r'''
import json, sys, time
from contextlib import contextmanager
from pathlib import Path
from app.services.infrastructure.mcp.installation.owner import McpInstallationOwner
from app.schemas.internal_v2.mcp import McpManagedLaunchDTO
from app.services.infrastructure.workspace_state_store import WorkspaceStateStore
home, backend, root, binding, control, mode = sys.argv[1:]
owner = McpInstallationOwner(home=Path(home), backend_id=backend)
store = WorkspaceStateStore(workspace_root=Path(root))
store.bind_mcp_installations(owner)
original = owner.reference_write
@contextmanager
def delayed(bindings):
    with original(bindings):
        Path(control + '.ready').touch()
        deadline = time.monotonic() + 10
        while not Path(control + '.release').exists():
            if time.monotonic() >= deadline:
                raise TimeoutError('写入测试未收到结算信号')
            time.sleep(.01)
        yield
if mode == 'hold':
    owner.reference_write = delayed
payload = {'mcp': {'servers': {'same-server': {'enabled': False, 'transport': 'stdio', 'managed_launch': json.loads(binding)}}}}
try:
    store.sync_config_source(config_key='workspace_root_mutable_override', uri=None, config_version=2, presence='present', payload=payload, layer_digest='child-source')
except Exception as error:
    print(json.dumps({'error': type(error).__name__, 'message': str(error)}), flush=True)
    if mode != 'reject':
        raise
else:
    print(json.dumps({'saved': True}), flush=True)
    assert mode != 'reject'
finally:
    store.close()
'''


async def _wait_file(path: Path) -> None:
    async with asyncio.timeout(10):
        while not path.exists():
            await asyncio.sleep(.01)


@pytest.mark.parametrize('first', ['writer', 'uninstall'])
async def test_first_reference_and_uninstall_serialize_across_real_processes(retention_case: RetentionCase, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch, first: str) -> None:
    case = retention_case
    artifact = case.publish()
    case.configs[0].close()
    control = tmp_path / 'writer'
    release = threading.Event()
    started = threading.Event()
    original_remove = case.owner._remove_directory
    def delayed_remove(root: Path) -> None:
        started.set()
        if not release.wait(10):
            raise TimeoutError('卸载测试未收到结算信号')
        original_remove(root)
    if first == 'uninstall':
        monkeypatch.setattr(case.owner, '_remove_directory', delayed_remove)
        await case.owner.uninstall(case.request(artifact), actual_target=case.target)
        assert await asyncio.to_thread(started.wait, 5)
    child = await asyncio.create_subprocess_exec('uv', 'run', '--no-sync', 'python', '-c', _WRITER, str(case.home), case.owner.backend_id,
        str(case.workspaces[0]), json.dumps(artifact.binding.model_dump()), str(control), 'hold' if first == 'writer' else 'reject',
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        if first == 'writer':
            await _wait_file(Path(str(control) + '.ready'))
            removing = asyncio.create_task(case.owner.uninstall(case.request(artifact), actual_target=case.target))
            await asyncio.sleep(.1)
            assert not removing.done() and case.owner.artifacts() == [artifact]
            Path(str(control) + '.release').touch()
            with pytest.raises(McpArtifactInUse):
                await removing
        stdout_bytes, stderr_bytes = await asyncio.wait_for(child.communicate(), timeout=15)
        stdout, stderr = stdout_bytes.decode(), stderr_bytes.decode()
        assert child.returncode == 0, (stdout, stderr)
        observed = json.loads(stdout.strip())
        if first == 'writer':
            assert observed == {'saved': True} and Path(artifact.command).is_file()
        else:
            assert observed['error'] == 'McpCandidateConflict' and 'removing' in observed['message']
    finally:
        Path(str(control) + '.release').touch()
        release.set()
        if child.returncode is None:
            child.kill()
            await asyncio.wait_for(child.communicate(), timeout=5)
    if first == 'uninstall':
        await case.owner.close()
        assert case.owner.operation('uninstall').status == 'succeeded' and case.owner.artifacts() == []
