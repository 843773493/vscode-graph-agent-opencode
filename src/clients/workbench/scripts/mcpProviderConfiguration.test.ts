import { afterEach, expect, spyOn, test } from 'bun:test';
import * as sources from '../../web/src/api/configSources.js';
import { addMcpProviderConfiguration } from '../src/mcp/providerConfiguration.js';
import type { LiveMcpProviderDefinition } from '../src/mcp/providerBridge.js';
import { CancellationToken } from '../upstream/src/vs/base/common/cancellation.js';
import type { IQuickInputService } from '../upstream/src/vs/platform/quickinput/common/quickInput.js';
import type { INotificationService } from '../upstream/src/vs/platform/notification/common/notification.js';
import type { ConfigSourceDocumentDTO } from '../../web/src/types/protocol_generated/boxteam/workspace/v2/public.js';

const route = { gateway_id: 'gateway_' + 'a'.repeat(32), workspace_id: '00000000-0000-4000-8000-000000000001' };
const reference = { gateway_id: route.gateway_id, backend_id: '00000000-0000-4000-8000-000000000002',
  client_id: 'original-client', view_id: 'default', host_generation: 'original-generation', host_kind: 'node',
  extension_id: 'probe.dynamic', extension_key: 'probe.dynamic-1.0.0', collection_id: 'probe.dynamic/definitions',
  provider_ref: 'original-provider', definition_ref: 'original-object' };
const document: ConfigSourceDocumentDTO = { ...route, source_key: 'workspace_root_mutable_override', layer: 'workspace', writable: true,
  jsonc: '// 保留原来源注释\n{"original":true}\n', file_presence: 'present', file_digest: 'original-file', layer_revision: 3,
  layer_digest: 'original-layer', source_generation: 'original-source', active_revision: 2, active_digest: 'original-active',
  json_schema: {}, effective: {}, source_effective: {}, layers: [], origins: [] };
const mocks: { mockRestore(): void }[] = [];
afterEach(() => { mocks.splice(0).forEach(mock => mock.mockRestore()); });

function fixture(answers: (number | undefined)[] = [0, 1, 0]) {
  const messages: string[] = [], calls: number[] = [];
  let valid = true, candidateValid = true;
  const candidate: LiveMcpProviderDefinition = { reference, definition: { definition_ref: reference.definition_ref,
    label: 'Duplicate', version: 'same', transport: 'stdio' }, workspaceIds: [route.workspace_id], gatewayConnectionId: undefined,
    check() { if (!candidateValid) throw new Error('原 Provider 快照已失效'); } };
  const quickInput = { async pick(items: unknown[]) {
    calls.push(items.length);
    const index = answers.shift();
    return index === undefined ? undefined : items[index];
  } } as unknown as IQuickInputService;
  const notifications = { info(value: string) { messages.push(value); } } as unknown as INotificationService;
  const preflight = spyOn(sources, 'preflightConfigSource').mockResolvedValue({ document, candidate: {},
    candidate_digest: 'candidate-digest', changed_paths: ['mcp.servers.dynamic'] });
  const save = spyOn(sources, 'saveConfigSource').mockImplementation(async (_port, _route, intent) => ({ ...document,
    edit: { idempotency_key: intent.idempotency_key, candidate_id: 'original-candidate', state: 'pending',
      file_digest: 'saved-file', source_saved: true, layer_imported: true } }));
  const read = spyOn(sources, 'getConfigSourceDocument').mockResolvedValue(document);
  mocks.push(preflight, save, read);
  const run = () => addMcpProviderConfiguration([candidate], { route, owner: '原工作区', document, serverId: 'dynamic', quickInput,
    notifications, token: CancellationToken.None, signal: new AbortController().signal,
    check() { if (!valid) throw new Error('原身份已失效'); } });
  return { run, preflight, save, read, messages, calls, invalidate() { valid = false; }, invalidateCandidate() { candidateValid = false; } };
}

test('Provider来源只保存原引用和完整CAS，保留注释并如实显示pending', async () => {
  const f = fixture();
  await f.run();
  expect(f.save).toHaveBeenCalledTimes(1);
  const [port, selectedRoute, intent] = f.save.mock.calls[0]!;
  expect(port).toBe(0); expect(selectedRoute).toEqual(route);
  expect(intent).toMatchObject({ ...route, source_key: document.source_key, base_file_digest: document.file_digest,
    base_layer_revision: document.layer_revision, base_layer_digest: document.layer_digest,
    expected_active_revision: document.active_revision, expected_active_digest: document.active_digest });
  expect(intent.jsonc).toStartWith('// 保留原来源注释\n');
  const payload = JSON.parse(intent.jsonc!.slice(intent.jsonc!.indexOf('{')));
  expect(payload).toEqual({ original: true, mcp: { servers: { dynamic: { enabled: true, transport: 'stdio', provider_definition: reference } } } });
  expect(f.preflight.mock.calls[0]![2]).toEqual(intent);
  expect(f.messages[0]).toContain('source 状态 pending'); expect(f.messages[0]).toContain('source_saved=true');
  expect(f.read).not.toHaveBeenCalled();
});

test('选择或确认取消不写来源；预检不等同成功保存', async () => {
  const first = fixture([undefined]); await first.run(); expect(first.preflight).not.toHaveBeenCalled(); expect(first.save).not.toHaveBeenCalled();
  first.preflight.mockRestore(); first.save.mockRestore(); first.read.mockRestore();
  const confirm = fixture([0, 1, 1]); await confirm.run();
  expect(confirm.preflight).toHaveBeenCalledTimes(1); expect(confirm.save).not.toHaveBeenCalled(); expect(confirm.messages).toEqual([]);
});

test('预检期间身份或原Provider失效都拒绝保存', async () => {
  for (const invalidate of ['identity', 'provider']) {
    const f = fixture();
    f.preflight.mockImplementation(async () => {
      if (invalidate === 'identity') f.invalidate(); else f.invalidateCandidate();
      return { document, candidate: {}, candidate_digest: 'digest', changed_paths: [] };
    });
    await expect(f.run()).rejects.toThrow(invalidate === 'identity' ? '原身份已失效' : '原 Provider 快照已失效');
    expect(f.save).not.toHaveBeenCalled(); expect(f.calls).toHaveLength(2);
    f.preflight.mockRestore(); f.save.mockRestore(); f.read.mockRestore();
  }
});

test('保存响应丢失只以原幂等键回读，保留原拒绝原因且不重发PUT', async () => {
  const f = fixture();
  f.save.mockRejectedValue(new Error('原保存连接中断'));
  await expect(f.run()).rejects.toThrow('原保存连接中断');
  expect(f.save).toHaveBeenCalledTimes(1); expect(f.read).toHaveBeenCalledTimes(1);
  const intent = f.save.mock.calls[0]![2];
  expect(f.read.mock.calls[0]).toEqual([0, route, document.source_key, { idempotencyKey: intent.idempotency_key, signal: expect.any(AbortSignal) }]);
  expect(f.messages).toEqual([]);
});
