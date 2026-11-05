import { afterEach, beforeEach, expect, spyOn, test } from 'bun:test';
import * as api from '../../web/src/api/mcpConnections.js';
import * as authorization from '../src/mcp/authorizationWindow.js';
import { WorkspaceMcpCatalog } from '../src/mcp/catalog.js';
import { WorkspaceMcpServer } from '../src/mcp/server.js';
import { CancellationToken } from '../upstream/src/vs/base/common/cancellation.js';
import type { GatewayWorkspaceRequestTarget } from '../../web/src/api/workspaceIdentity.js';
import type { McpServerListResponseDTO } from '../../web/src/types/protocol_generated/boxteam/workspace/v2/public.js';
import * as gatewayApi from '../../web/src/gatewayApi.js';
import * as browserSettings from '../../web/src/hooks/settings/browserUiSettings.js';
import { createDefaultWebUiSettings } from '../../web/src/state/uiSettings/preferences.js';
import type { GatewayWorkspaceList } from '../../web/src/types/backend.js';
import { workspaceContext } from '../src/workspaceContext.js';
import { gatewayWorkspaceKey } from '../../web/src/api/workspaceIdentity.js';

const target = { gateway_id: 'gateway-a', workspace_id: '00000000-0000-4000-8000-000000000001' };
const other = { gateway_id: 'gateway-b', workspace_id: '00000000-0000-4000-8000-000000000002' };
const server: api.McpServerDTO = { server_id: 'secure', transport: 'streamable_http', enabled: true, status: 'authorization_required', tools: [] };
const operation: api.McpConnectionOperationDTO = { operation_id: 'op-a', connection_id: 'connection-a', server_id: 'secure', action: 'connect', status: 'waiting_user', stage: 'authorize', created_at: 1, updated_at: 1, deadline: 300 };
function envelope(owner = target, servers = [server], generation = 1): McpServerListResponseDTO {
  return { code: 0, message: 'ok', request_id: 'request-mcp', protocol_version: 1, target: { kind: 'workspace', ...owner }, catalog_revision: 'sha256:' + 'a'.repeat(64), catalog_generation: generation, data: servers };
}
function installMocks() {
  return {
    read: spyOn(api, 'getMcpCatalog').mockImplementation(async (_port, owner) => envelope(owner)),
    operations: spyOn(api, 'listMcpOperations').mockResolvedValue([]),
    submit: spyOn(api, 'submitMcpOperation').mockResolvedValue(operation),
    cancel: spyOn(api, 'cancelMcpOperation').mockResolvedValue({ ...operation, status: 'cancelled' }),
    authorize: spyOn(authorization, 'authorizeMcpInWindow').mockResolvedValue({ ...operation, status: 'verifying' }),
  };
}
let mocks: ReturnType<typeof installMocks>;
const catalogs: WorkspaceMcpCatalog[] = [];
function create(owner: GatewayWorkspaceRequestTarget = target, changed: () => void = () => {}) {
  const catalog = new WorkspaceMcpCatalog(owner, owner.gateway_id, changed);
  catalogs.push(catalog);
  return catalog;
}
const model = (catalog: WorkspaceMcpCatalog, name = server.server_id) => new WorkspaceMcpServer(catalog, name, async () => {}, () => ({ dispose() {} }));
beforeEach(() => { mocks = installMocks(); });
afterEach(() => {
  catalogs.splice(0).forEach(catalog => catalog.dispose());
  Object.values(mocks).forEach(mock => mock.mockRestore());
});

test('完整目录封套与精确 owner 保留，同名服务共享原对象但不同目标不串线', async () => {
  const selection = { ...target }, a = create(selection), b = create(other);
  selection.gateway_id = 'changed-selection';
  await Promise.all([a.refresh(), b.refresh()]);
  const ma = model(a), mb = model(b);
  expect(a.snapshot.catalog).toEqual(envelope(target));
  expect(a.target).toEqual(target);
  expect(Object.isFrozen(a.target)).toBe(true);
  expect(ma.id).not.toBe(mb.id);
  await ma.start();
  expect(mocks.submit).toHaveBeenCalledWith(0, target, 'secure', 'connect', expect.any(AbortSignal));
  expect(b.snapshot.operations).toEqual([]);
  expect(ma.local).toBeUndefined();
  expect(ma.tools.get()).toEqual([]);
});

test('提交前的迟到目录不能越过操作代次覆盖，收尾重新读取原 owner', async () => {
  const published: McpServerListResponseDTO[] = [];
  const catalog = create(target, () => { if (catalog.snapshot.catalog) published.push(catalog.snapshot.catalog); });
  await catalog.refresh();
  const late = Promise.withResolvers<McpServerListResponseDTO>();
  mocks.read.mockImplementationOnce(() => late.promise);
  const read = catalog.refresh();
  const submitted = Promise.withResolvers<void>();
  mocks.submit.mockImplementationOnce(async () => { submitted.resolve(); return operation; });
  const action = catalog.act('secure', 'connect');
  await submitted.promise;
  await Promise.resolve();
  mocks.read.mockResolvedValueOnce(envelope(target, [server], 2));
  mocks.operations.mockResolvedValueOnce([operation]);
  late.resolve(envelope(target, [{ ...server, status: 'ready' }], 99));
  await Promise.all([read, action]);
  expect(published.some(catalog => catalog.catalog_generation === 99)).toBe(false);
  expect(catalog.snapshot.catalog?.catalog_generation).toBe(2);
  expect(catalog.snapshot.operations).toEqual([operation]);
  expect(mocks.submit).toHaveBeenCalledTimes(1);
});

test('操作期间显式同代刷新失败，仍重读 owner、释放 busy 并允许下次操作', async () => {
  const catalog = create();
  await catalog.refresh();
  const submitted = Promise.withResolvers<api.McpConnectionOperationDTO>();
  mocks.submit.mockImplementationOnce(() => submitted.promise);
  const action = catalog.act('secure', 'connect');
  const late = Promise.withResolvers<McpServerListResponseDTO>();
  mocks.read.mockImplementationOnce(() => late.promise);
  const read = catalog.refresh();
  const readError = read.catch(error => error);
  submitted.resolve(operation);
  await Promise.resolve();
  await Promise.resolve();
  const failure = new Error('same-generation read failed');
  late.reject(failure);
  expect(await readError).toBe(failure);
  await expect(action).rejects.toBe(failure);
  expect(catalog.isBusy).toBe(false);
  expect(mocks.read).toHaveBeenCalledTimes(3);
  await catalog.act('secure', 'connect');
  expect(mocks.submit).toHaveBeenCalledTimes(2);
});

test('提交失败与状态重读失败均保留，不把原错误替换成重读错误或重放请求', async () => {
  const catalog = create();
  await catalog.refresh();
  const submitFailure = new Error('original submit failed'), readFailure = new Error('reconcile failed');
  mocks.submit.mockRejectedValueOnce(submitFailure);
  mocks.read.mockRejectedValueOnce(readFailure);
  const failure = await catalog.act('secure', 'connect').catch(error => error);
  expect(failure).toBeInstanceOf(AggregateError);
  expect(failure.errors).toEqual([submitFailure, readFailure]);
  expect(catalog.snapshot.actionError).toBe(submitFailure.message);
  expect(catalog.snapshot.error).toBe(readFailure.message);
  expect(catalog.isBusy).toBe(false);
  expect(mocks.submit).toHaveBeenCalledTimes(1);
});

test('提交回执未知时只重读真实状态，OAuth 拒绝错误同样保留到下一次显式操作', async () => {
  const catalog = create();
  await catalog.refresh();
  mocks.submit.mockRejectedValueOnce(new Error('unknown response'));
  mocks.read.mockResolvedValueOnce(envelope(target, [{ ...server, status: 'ready' }], 2));
  await expect(catalog.act('secure', 'connect')).rejects.toThrow('unknown response');
  expect(catalog.server('secure')?.status).toBe('ready');
  expect(mocks.submit).toHaveBeenCalledTimes(1);
  catalog.snapshot = { ...catalog.snapshot, operations: [operation] };
  mocks.authorize.mockRejectedValueOnce(new Error('authorization denied'));
  await expect(catalog.act('secure', 'authorize')).rejects.toThrow('authorization denied');
  expect(catalog.snapshot.actionError).toBe('authorization denied');
  expect(await model(catalog).getReadme(CancellationToken.None)).toContain('authorization&nbsp;denied');
});

test.each(['cancelled', 'expired', 'failed'] as const)('原操作 %s 不被旧 server.status 覆盖，工具摘要只展示不注册执行', async status => {
  const catalog = create();
  await catalog.refresh();
  catalog.snapshot = { catalog: envelope(target, [{ ...server, status: 'authorization_required', tools: [{ server_id: 'secure', tool_id: 'tool-a', remote_name: 'lookup', description: '真实摘要' }] }]), operations: [{ ...operation, status, error_code: status + '_code' }] };
  const item = model(catalog);
  expect(item.statusLabel).toBe({ cancelled: '已取消', expired: '已过期', failed: '原连接不可用' }[status]);
  const readme = await item.getReadme(CancellationToken.None);
  expect(readme).toContain(status + '\\_code');
  expect(readme).toContain('真实摘要');
  expect(item.description).toContain('1 个已知工具');
  expect(item.tools.get()).toEqual([]);
});

test('兄弟服务等待授权不会改变 ready 服务的 Stop 派生；只改变 actionError 也发布更新', async () => {
  const catalog = create();
  await catalog.refresh();
  catalog.snapshot = { catalog: envelope(target, [server, { ...server, server_id: 'schema', status: 'ready' }]), operations: [operation] };
  const secure = model(catalog), schema = model(catalog, 'schema');
  expect(secure.isPending).toBe(true);
  expect(schema.isPending).toBe(false);
  expect(schema.canStop).toBe(true);
  expect(schema.changed()).toBe(true);
  expect(schema.changed()).toBe(false);
  catalog.snapshot = { ...catalog.snapshot, actionError: 'new-error' };
  expect(schema.changed()).toBe(true);
  await schema.stop();
  expect(mocks.submit).toHaveBeenCalledWith(0, target, 'schema', 'disconnect', expect.any(AbortSignal));
  expect(mocks.cancel).not.toHaveBeenCalled();
});

test('原 start 对 ready／pending 幂等，不重复提交；禁用、移除及读取错误明确拒绝', async () => {
  const catalog = create();
  await catalog.refresh();
  catalog.snapshot = { catalog: envelope(target, [{ ...server, status: 'ready' }]), operations: [] };
  const item = model(catalog);
  await item.start();
  catalog.snapshot = { catalog: envelope(), operations: [operation] };
  item.changed();
  await item.start();
  expect(mocks.submit).not.toHaveBeenCalled();
  for (const snapshot of [
    { catalog: envelope(target, [{ ...server, enabled: false, status: 'disabled' as const }]), operations: [] },
    { catalog: envelope(target, []), operations: [] },
    { catalog: envelope(), operations: [], error: 'owner unavailable' },
  ]) {
    catalog.snapshot = snapshot;
    item.changed();
    await expect(item.start()).rejects.toThrow('不可发起连接');
  }
});

test('移除目标中止原请求，迟到结果不发布，也不改投当前工作区', async () => {
  const catalog = create();
  await catalog.refresh();
  const late = Promise.withResolvers<McpServerListResponseDTO>();
  mocks.read.mockImplementationOnce(() => late.promise);
  const read = catalog.refresh();
  const signal = mocks.read.mock.calls.at(-1)![2]!;
  catalog.dispose();
  late.resolve(envelope(target, [], 2));
  await read;
  expect(signal.aborted).toBe(true);
  expect(catalog.snapshot.catalog?.catalog_generation).toBe(1);
  await expect(catalog.act('secure', 'connect')).rejects.toThrow('目标已移除');
  expect(mocks.submit).not.toHaveBeenCalled();
});

function workspaceList(owner = target): GatewayWorkspaceList {
  return { active_workspace: owner, items: [{ ...owner, name: owner.gateway_id, backend_url: 'http://workspace.example',
    connection_kind: 'local', status: 'ready', active: true, managed: true, removable: true, system_default: false,
    services: {}, checked_at: '2026-11-05T00:00:00Z', affected_workspace_ids: [], remote_gateways: [] }] };
}
function setupContext() {
  const saved = workspaceContext.getSnapshot();
  workspaceContext.publish({ gatewayId: null, workspaces: [], activeId: null, settings: null, loading: true, error: null });
  const list = spyOn(gatewayApi, 'listGatewayWorkspaces').mockResolvedValue(workspaceList());
  const settings = spyOn(browserSettings, 'getBrowserUiSettings').mockResolvedValue(createDefaultWebUiSettings());
  const identity = spyOn(browserSettings, 'browserGatewayId').mockResolvedValue(target.gateway_id);
  return { list, restore() { list.mockRestore(); settings.mockRestore(); identity.mockRestore(); workspaceContext.publish(saved); } };
}

test('MCP 与外部视图并发刷新共用原 workspaceContext 请求，等待真实目标发布后一起结算', async () => {
  const fixture = setupContext();
  try {
    const response = Promise.withResolvers<GatewayWorkspaceList>();
    fixture.list.mockImplementationOnce(() => response.promise);
    const a = workspaceContext.refresh(), b = workspaceContext.refresh();
    expect(a).toBe(b);
    expect(fixture.list).toHaveBeenCalledTimes(1);
    expect(workspaceContext.getSnapshot().loading).toBe(true);
    response.resolve(workspaceList());
    await Promise.all([a, b]);
    expect(workspaceContext.getSnapshot()).toMatchObject({ loading: false, error: null, activeId: gatewayWorkspaceKey(target) });
  } finally { fixture.restore(); }
});

test('导航的 loading 发布淘汰旧刷新时，原请求不提前当无目标完成，下一读取取得当前 owner', async () => {
  const fixture = setupContext();
  try {
    const first = Promise.withResolvers<GatewayWorkspaceList>(), latest = Promise.withResolvers<GatewayWorkspaceList>();
    const retried = Promise.withResolvers<void>();
    fixture.list.mockImplementationOnce(() => first.promise).mockImplementationOnce(() => { retried.resolve(); return latest.promise; });
    let settled = false;
    const read = workspaceContext.refresh().then(() => { settled = true; });
    workspaceContext.publish({ ...workspaceContext.getSnapshot(), loading: true });
    first.resolve(workspaceList());
    await retried.promise;
    expect(settled).toBe(false);
    expect(workspaceContext.getSnapshot().loading).toBe(true);
    latest.resolve(workspaceList(other));
    await read;
    expect(workspaceContext.getSnapshot()).toMatchObject({ loading: false, error: null, activeId: gatewayWorkspaceKey(other) });
    expect(fixture.list).toHaveBeenCalledTimes(2);
  } finally { fixture.restore(); }
});

test.each(['success', 'error', 'loading-error'] as const)('已发布的导航 %s 结算不会被被淘汰请求覆盖或补造成功', async outcome => {
  const fixture = setupContext();
  try {
    const stale = Promise.withResolvers<GatewayWorkspaceList>();
    fixture.list.mockImplementationOnce(() => stale.promise);
    const read = workspaceContext.refresh();
    const next = { gatewayId: other.gateway_id, workspaces: workspaceList(other).items, activeId: gatewayWorkspaceKey(other),
      settings: createDefaultWebUiSettings(), loading: outcome === 'loading-error', error: outcome === 'success' ? null : '真实导航失败' };
    workspaceContext.publish(next);
    stale.resolve(workspaceList());
    await read;
    expect(workspaceContext.getSnapshot()).toBe(next);
    expect(fixture.list).toHaveBeenCalledTimes(1);
  } finally { fixture.restore(); }
});

test('workspaceContext 真实读取失败发布原错误，下一次显式刷新会重新读取并恢复', async () => {
  const fixture = setupContext();
  try {
    fixture.list.mockRejectedValueOnce(new Error('gateway unavailable'));
    await workspaceContext.refresh();
    expect(workspaceContext.getSnapshot()).toMatchObject({ loading: false, error: 'gateway unavailable', activeId: null });
    await workspaceContext.refresh();
    expect(workspaceContext.getSnapshot()).toMatchObject({ loading: false, error: null, activeId: gatewayWorkspaceKey(target) });
    expect(fixture.list).toHaveBeenCalledTimes(2);
  } finally { fixture.restore(); }
});
