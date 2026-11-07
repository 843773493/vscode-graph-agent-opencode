import { afterEach, expect, spyOn, test } from 'bun:test';
import * as api from '../../web/src/api/mcpConnections.js';
import { HttpRequestError } from '../../web/src/api/http.js';
import { manageMcpArtifacts } from '../src/mcp/artifacts.js';
import type { IQuickInputService, IQuickPickItem, QuickPickInput } from '../upstream/src/vs/platform/quickinput/common/quickInput.js';
import type { IProgressService } from '../upstream/src/vs/platform/progress/common/progress.js';
import type { INotificationService } from '../upstream/src/vs/platform/notification/common/notification.js';
import type { McpInstalledArtifactDTO, McpInstallationOperationDTO } from '../../web/src/types/protocol_generated/boxteam/workspace/v2/public.js';

const workspace = { gateway_id: 'gateway_00000000000040008000000000000001', workspace_id: '00000000-0000-4000-8000-000000000001' };
const target = { kind: 'backend', gateway_id: workspace.gateway_id, backend_id: '00000000-0000-4000-8000-000000000011' };
const binding = { backend_id: target.backend_id, installation_id: 'b'.repeat(32), generation: 'c'.repeat(32), artifact_digest: 'sha256:jcs:v1:' + 'a'.repeat(64) };
const artifact: McpInstalledArtifactDTO = { binding, source: { kind: 'manual', name: 'test', version: '1' },
  manifest_digest: binding.artifact_digest, variant: { kind: 'package', index: 0, digest: binding.artifact_digest },
  platform: { operating_system: 'linux', architecture: 'x86_64' }, registry_type: 'pypi', identifier: 'chosen', version: '1.0.0',
  entrypoint: 'chosen', command: '/published/chosen', lock_digest: 'sha256:' + 'd'.repeat(64),
  installed_distributions: { chosen: '1.0.0' }, state: 'published' };
const envelope = <T>(data: T) => ({ code: 0, message: 'ok', data, request_id: 'request', target, protocol_version: 1 });
const operation = (operationId: string): McpInstallationOperationDTO => ({ operation_id: operationId,
  intent_digest: binding.artifact_digest, status: 'succeeded', stage: 'complete', created_at: 1, updated_at: 2,
  kind: 'uninstall', cancellation_requested: false, log: '', artifact: { ...artifact, state: 'removed' } });
const mocks: { mockRestore(): void }[] = [];
afterEach(() => { mocks.splice(0).forEach(mock => mock.mockRestore()); });

function prepare(steps: ((items: QuickPickInput<IQuickPickItem>[]) => IQuickPickItem | undefined | Promise<IQuickPickItem | undefined>)[]) {
  const capabilities = spyOn(api, 'getMcpInstallationCapabilities').mockResolvedValue(envelope({ platform: artifact.platform,
    pypi_wheel_available: false, npm_available: false, unsupported_registry_types: [], limitations: [] }));
  const listing = spyOn(api, 'listMcpInstalledArtifacts').mockResolvedValue(envelope([artifact]));
  const references = spyOn(api, 'getMcpArtifactReferences').mockResolvedValue(envelope({ binding, references: [] }));
  const uninstall = spyOn(api, 'uninstallMcpArtifact').mockImplementation(async (_port, _workspace, intent) => envelope(operation(intent.operation_id)));
  const query = spyOn(api, 'getMcpInstallationOperation').mockResolvedValue(envelope(operation('original-operation')));
  mocks.push(capabilities, listing, references, uninstall, query);
  const notifications: string[] = [];
  const input = { pick: async (items: QuickPickInput<IQuickPickItem>[]) => {
    const step = steps.shift();
    if (!step) throw new Error('出现计划外的 Quick Input');
    return step(items);
  }, input: async () => 'original-operation' } as unknown as IQuickInputService;
  const progress = { withProgress: async (_options: unknown, task: (report: { report(): void }) => Promise<void>) => task({ report() {} }) } as unknown as IProgressService;
  const notify = { info: (message: string) => { notifications.push(message); } } as unknown as INotificationService;
  const controller = new AbortController();
  const run = (route = workspace, assertAccess = () => {}) => manageMcpArtifacts(route, '目标 A', input, progress, notify, assertAccess, controller.signal);
  return { run, capabilities, listing, references, uninstall, query, notifications, controller };
}
const choose = (label: string) => (items: QuickPickInput<IQuickPickItem>[]) => {
  const item = items.find(item => item.type !== 'separator' && item.label === label);
  expect(item).toBeDefined();
  return item as IQuickPickItem;
};

test('查看引用和取消确认均不写入，安装runtime不可用仍可读取原制品', async () => {
  const fixture = prepare([choose('chosen 1.0.0'), choose('查看保留引用'), choose('chosen 1.0.0'), choose('物理卸载此制品'), () => undefined, () => undefined]);
  await fixture.run();
  expect(fixture.references).toHaveBeenCalledTimes(2);
  expect(fixture.uninstall).not.toHaveBeenCalled();
  expect(fixture.notifications[0]).toContain('当前没有保留引用');
});

test('有引用时列出真实工作区和原因，不进入确认或提交卸载', async () => {
  const fixture = prepare([choose('chosen 1.0.0'), choose('物理卸载此制品'), items => {
    expect(items[0]).toMatchObject({ label: 'source_layer · disabled', description: 'same-server',
      detail: 'workspace-B · 遮蔽配置仍引用原制品' });
    return undefined;
  }, () => undefined]);
  fixture.references.mockResolvedValue(envelope({ binding, references: [{ workspace_id: 'workspace-B', kind: 'source_layer',
    status: 'disabled', reason: '遮蔽配置仍引用原制品', server_id: 'same-server' }] }));
  await fixture.run();
  expect(fixture.uninstall).not.toHaveBeenCalled();
});

test('显式确认只提交一次并冻结路由，完成后重新读取原列表', async () => {
  const route = { ...workspace };
  const fixture = prepare([choose('chosen 1.0.0'), choose('物理卸载此制品'), items => {
    route.workspace_id = 'different-focus';
    return choose('确认物理卸载')(items);
  }, () => undefined]);
  await fixture.run(route);
  expect(fixture.uninstall).toHaveBeenCalledTimes(1);
  const call = fixture.uninstall.mock.calls[0]!;
  expect(call[1]).toEqual(workspace);
  expect(call[2]).toMatchObject({ target, binding });
  expect(call[3]).toBe(fixture.controller.signal);
  expect(fixture.listing).toHaveBeenCalledTimes(2);
  expect(fixture.notifications[0]).toContain('卸载已完成');
});

test('确认期间新增引用的409保留原错误，回读并展示实际引用，不自动重放', async () => {
  const fixture = prepare([choose('chosen 1.0.0'), choose('物理卸载此制品'), choose('确认物理卸载'), () => undefined]);
  fixture.references.mockResolvedValueOnce(envelope({ binding, references: [] })).mockResolvedValue(envelope({ binding,
    references: [{ workspace_id: 'workspace-B', kind: 'connection', status: 'running', reason: '新连接仍在使用' }] }));
  const error = new HttpRequestError(409, 'Conflict', '制品仍有引用', '/installations', 'request-conflict');
  fixture.uninstall.mockRejectedValueOnce(error);
  await expect(fixture.run()).rejects.toBe(error);
  expect(fixture.references).toHaveBeenCalledTimes(2);
  expect(fixture.listing).toHaveBeenCalledTimes(2);
  expect(fixture.uninstall).toHaveBeenCalledTimes(1);
});

test('卸载响应丢失后只回读且保留原操作ID，不自动重放', async () => {
  const fixture = prepare([choose('chosen 1.0.0'), choose('物理卸载此制品'), choose('确认物理卸载')]);
  fixture.uninstall.mockRejectedValueOnce(new Error('connection lost after acceptance'));
  let failure: unknown;
  try { await fixture.run(); } catch (cause) { failure = cause; }
  const id = fixture.uninstall.mock.calls[0]![2].operation_id;
  expect(failure).toBeInstanceOf(Error);
  expect((failure as Error).message).toContain(id);
  expect(fixture.listing).toHaveBeenCalledTimes(2);
  expect(fixture.uninstall).toHaveBeenCalledTimes(1);
});

test('卸载409后引用回读失败仍保留原卸载错误及request_id', async () => {
  const fixture = prepare([choose('chosen 1.0.0'), choose('物理卸载此制品'), choose('确认物理卸载')]);
  const original = new HttpRequestError(409, 'Conflict', '制品仍有引用', '/installations', 'original-conflict-request');
  const readError = new HttpRequestError(503, 'Unavailable', '引用读取失败', '/references', 'secondary-read-request');
  fixture.uninstall.mockRejectedValueOnce(original);
  fixture.references.mockResolvedValueOnce(envelope({ binding, references: [] })).mockRejectedValueOnce(readError);
  const failure = await fixture.run().catch((cause: unknown) => cause);
  expect(failure).toBeInstanceOf(AggregateError);
  expect((failure as AggregateError).errors).toEqual([original, readError]);
  expect((failure as AggregateError).errors[0].requestId).toBe('original-conflict-request');
  expect(fixture.uninstall).toHaveBeenCalledTimes(1);
});

test('查询原操作不提交新的删除', async () => {
  const queryOnly = prepare([choose('查询已有安装／卸载操作'), () => undefined]);
  await queryOnly.run();
  expect(queryOnly.query.mock.calls[0]![3]).toBe('original-operation');
  expect(queryOnly.uninstall).not.toHaveBeenCalled();
});

test('确认前身份失效拒绝卸载', async () => {
  let revoked = false;
  const fixture = prepare([choose('chosen 1.0.0'), choose('物理卸载此制品'), items => {
    revoked = true; return choose('确认物理卸载')(items);
  }]);
  await expect(fixture.run(workspace, () => { if (revoked) throw new Error('identity changed'); })).rejects.toThrow('identity changed');
  expect(fixture.uninstall).not.toHaveBeenCalled();
});

test('另一操作的成功回执不能被当作原卸载完成', async () => {
  const fixture = prepare([choose('chosen 1.0.0'), choose('物理卸载此制品'), choose('确认物理卸载')]);
  fixture.uninstall.mockResolvedValue(envelope(operation('different-operation')));
  await expect(fixture.run()).rejects.toThrow('目标未返回原 MCP 操作');
  expect(fixture.notifications).toEqual([]);
  expect(fixture.uninstall).toHaveBeenCalledTimes(1);
});
