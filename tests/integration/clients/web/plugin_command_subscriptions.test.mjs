import { afterAll, expect, test } from 'bun:test';
import { writeFile } from 'node:fs/promises';
import { join, resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import { build } from '../../../../src/clients/workbench/node_modules/esbuild/lib/main.js';
import { buildSourcePlugin, pinnedUpstreamCommit, sourceTransform } from '../../../../src/clients/workbench/scripts/sourceTransform.mjs';
import { TestRunContext } from '../../../harness/js/run-context.mjs';

const testFile = 'tests/integration/clients/web/plugin_command_subscriptions.test.mjs';
const run = await TestRunContext.fromTestFile(testFile).prepare();
const root = resolve('src/clients/workbench'), bundle = join(run.artifactsDir, 'subscriptions.fixture.mjs');
await build({ stdin: { contents: `
import './tests/harness/js/workbench-locale.ts';
export { PluginCommands, notifyPluginCommandGatewayReconnected } from './src/clients/workbench/src/extensions/pluginCommands.js';
export { pluginCommandId } from './src/clients/workbench/src/extensions/pluginCommandRegistry.js';
export { workspaceContext } from './src/clients/workbench/src/workspaceContext.js';
export { ThreadInput } from './src/clients/workbench/src/threadInput.js';
export { gatewayWorkspaceKey } from './src/clients/web/src/api/workspaceIdentity.js';
export { CommandsRegistry } from './src/clients/workbench/upstream/src/vs/platform/commands/common/commands.js';
export { MenuId, MenuRegistry } from './src/clients/workbench/upstream/src/vs/platform/actions/common/actions.js';
export { Emitter } from './src/clients/workbench/upstream/src/vs/base/common/event.js';
export { installSessionApiFetchMock, unwrapSessionApiFetch } from './src/clients/web/src/api/session/sessionApiFetchMock.js';
export { invalidateGatewayToken } from './src/clients/web/src/api/http.js';
`, resolveDir: resolve('.') }, outfile: bundle, bundle: true, format: 'esm', platform: 'node', target: 'es2024',
  tsconfig: join(root, 'tsconfig.jsonc'), logLevel: 'silent', plugins: [buildSourcePlugin(root, sourceTransform(root, { commit: pinnedUpstreamCommit }, []))] });
const m = await import(pathToFileURL(bundle).href);
const checks = [];
const gatewayA = 'gateway-A', gatewayB = 'gateway-B';
const context = { createKey: () => ({ set() {}, reset() {} }) };
const workspace = (gateway, id) => ({ gateway_id: gateway, workspace_id: id, name: id,
  connection_kind: gateway === gatewayA ? 'local' : 'remote_gateway',
  remote_gateways: gateway === gatewayA ? [] : [{ gateway_id: gateway, gateway_connection_id: 'connection-B' }] });
const commandId = plugin => m.pluginCommandId('proof.subscriptions', plugin, 'run');
const globalTitles = () => m.MenuRegistry.getMenuItems(m.MenuId.CommandPalette)
  .filter(item => item.command?.id === commandId('global')).map(item => item.command.title);
function publication(target, global, title) {
  return { binding: { package_id: 'proof.subscriptions', package_version: '1.0.0', plugin_id: global ? 'global' : 'workspace',
    ...(global ? {} : { workspace_id: target.workspace_id }), enabled: true }, revision: title, state: 'ready', instance_id: title,
    commands: [{ id: 'run', title, handler: 'run', arguments_schema: {}, result_schema: {}, required_capabilities: [] }],
    registrations: [{ command_id: 'run', handler: 'run', registration_id: title }], menus: [], keybindings: [] };
}
function snapshot(target, title = 'Gateway current') {
  return { gateway_id: target.gateway_id, ...(target.workspace_id ? { workspace_id: target.workspace_id } : {}),
    publications: [publication(target, true, title), ...(target.workspace_id ? [publication(target, false, target.workspace_id)] : [])] };
}
async function until(predicate) {
  for (let i = 0; i < 200; i++) { if (predicate()) return; await new Promise(resolve => setTimeout(resolve, 5)); }
  throw new Error('命令订阅未按预期结算');
}
async function fixture(owners, callback) {
  const previous = m.workspaceContext.getSnapshot(), streams = [], errors = [], activeChanged = new m.Emitter();
  const visibleChanged = new m.Emitter(), groupAdded = new m.Emitter(), groupRemoved = new m.Emitter();
  const inputs = owners.map(({ gateway_id, workspace_id }) => new m.ThreadInput({ gatewayId: gateway_id, workspaceId: workspace_id,
    sessionId: 'ses_00000000400070008000000000000001', threadId: 'thr_00000000400070008000000000000002', title: workspace_id }));
  const editors = { activeEditor: inputs[0], onDidActiveEditorChange: activeChanged.event, onDidVisibleEditorsChange: visibleChanged.event };
  const groups = { groups: inputs.map(activeEditor => ({ activeEditor, scopedContextKeyService: context })),
    onDidAddGroup: groupAdded.event, onDidRemoveGroup: groupRemoved.event };
  m.workspaceContext.publish({ ...previous, gatewayId: gatewayA, workspaces: owners,
    activeId: owners[0] ? m.gatewayWorkspaceKey(owners[0]) : null, loading: false, error: null });
  m.installSessionApiFetchMock(({ path, init }) => {
    if (path !== '/api/gateway/service-plugins/commands/events') throw new Error(`非预期请求：${path}`);
    const target = JSON.parse(init.body), stream = { target, active: true };
    streams.push(stream);
    const body = new ReadableStream({ start(controller) {
      stream.publish = value => controller.enqueue(new TextEncoder().encode(`event: snapshot\ndata: ${JSON.stringify(value)}\n\n`));
      stream.close = () => { stream.active = false; controller.close(); };
      stream.publish(snapshot(target));
    }, cancel() { stream.active = false; } });
    return new Response(body, { headers: { 'Content-Type': 'text/event-stream' } });
  });
  const owner = new m.PluginCommands(editors, groups, context, { error: error => errors.push(String(error)) });
  const active = () => streams.filter(stream => stream.active);
  try {
    await until(() => streams.length > 0 && !!m.CommandsRegistry.getCommand(commandId('global')));
    await callback({ streams, active, errors, owner, editors, groups, inputs, visibleChanged, activeChanged });
  } finally {
    owner.dispose();
    await until(() => active().length === 0);
    for (const input of inputs) input.dispose();
    for (const event of [activeChanged, visibleChanged, groupAdded, groupRemoved]) event.dispose();
    m.unwrapSessionApiFetch(); m.workspaceContext.publish(previous);
  }
}

test('空窗口仍订阅Gateway，单工作区完整快照同时发布Gateway和工作区命令', async () => {
  await fixture([], async f => { expect(f.active().map(stream => stream.target)).toEqual([{ gateway_id: gatewayA }]); });
  await fixture([workspace(gatewayA, 'workspace-a')], async f => {
    expect(f.active().map(stream => stream.target)).toEqual([{ gateway_id: gatewayA, workspace_id: 'workspace-a' }]);
    expect(m.CommandsRegistry.getCommand(commandId('workspace'))).toBeDefined(); expect(f.errors).toEqual([]);
  });
  checks.push('空窗口Gateway原入口；有工作区时共享完整快照，两个逻辑目标均实际注册');
});

test('两个Gateway的可见工作区只占两条命令流，不按焦点重复订阅', async () => {
  await fixture([workspace(gatewayA, 'workspace-a'), workspace(gatewayB, 'workspace-b')], async f => {
    await until(() => f.active().length === 2);
    expect(f.active().map(stream => stream.target)).toEqual([
      { gateway_id: gatewayA, workspace_id: 'workspace-a' },
      { gateway_id: gatewayB, gateway_connection_id: 'connection-B', workspace_id: 'workspace-b' },
    ]);
    f.editors.activeEditor = f.inputs[1]; f.activeChanged.fire();
    expect(f.streams).toHaveLength(2); expect(f.errors).toEqual([]);
  });
  checks.push('双Gateway完整归属，两条物理流，切焦点零新增请求');
});

test('同Gateway只由稳定工作区来源发布全局命令，来源关闭后正常重建接管', async () => {
  await fixture([workspace(gatewayA, 'workspace-z'), workspace(gatewayA, 'workspace-a')], async f => {
    await until(() => f.active().length === 2);
    const secondary = f.active().find(stream => stream.target.workspace_id === 'workspace-z');
    const primary = f.active().find(stream => stream.target.workspace_id === 'workspace-a');
    primary.publish(snapshot(primary.target, 'Gateway new'));
    await until(() => globalTitles().length === 1 && globalTitles()[0] === 'Gateway new');
    secondary.publish(snapshot(secondary.target, 'Gateway old from sibling'));
    await new Promise(resolve => setTimeout(resolve, 0));
    expect(globalTitles()).toEqual(['Gateway new']);
    f.editors.activeEditor = f.inputs[0]; f.groups.groups = [f.groups.groups[0]]; f.visibleChanged.fire();
    await until(() => f.active().length === 1 && f.streams.length === 3
      && globalTitles().length === 1 && globalTitles()[0] === 'Gateway current');
    expect(primary.active).toBe(false); expect(secondary.active).toBe(false);
    expect(f.active()[0].target.workspace_id).toBe('workspace-z'); expect(f.errors).toEqual([]);
  });
  checks.push('稳定单一Gateway来源，兄弟迟到快照不覆盖；来源关闭后原流重建并发布完整回执');
});

test('远端连接重连只释放并重建原目标的两个逻辑投影，本地流保持', async () => {
  await fixture([workspace(gatewayA, 'workspace-a'), workspace(gatewayB, 'workspace-b')], async f => {
    await until(() => f.active().length === 2);
    const local = f.active().find(stream => stream.target.gateway_id === gatewayA);
    const remote = f.active().find(stream => stream.target.gateway_id === gatewayB);
    m.notifyPluginCommandGatewayReconnected('connection-B');
    await until(() => f.active().length === 2 && f.streams.length === 3);
    expect(local.active).toBe(true); expect(remote.active).toBe(false); expect(f.errors).toEqual([]);
  });
  checks.push('远端重连按原连接释放，不重启本地订阅；窗口释放结清所有reader');
});

test('断流释放所属Gateway与工作区贡献，焦点变化不重试且其它Gateway保持', async () => {
  await fixture([workspace(gatewayA, 'workspace-a'), workspace(gatewayB, 'workspace-b')], async f => {
    await until(() => f.active().length === 2 && globalTitles().length === 2);
    const local = f.active().find(stream => stream.target.gateway_id === gatewayA);
    const remote = f.active().find(stream => stream.target.gateway_id === gatewayB);
    remote.close();
    await until(() => f.errors.length === 1 && globalTitles().length === 1);
    expect(m.MenuRegistry.getMenuItems(m.MenuId.CommandPalette)
      .filter(item => item.command?.id === commandId('workspace')).map(item => item.command.title)).toEqual(['workspace-a']);
    f.editors.activeEditor = f.inputs[1]; f.activeChanged.fire();
    expect(f.streams).toHaveLength(2); expect(local.active).toBe(true);
    m.notifyPluginCommandGatewayReconnected('connection-B');
    await until(() => f.streams.length === 3 && globalTitles().length === 2);
  });
  checks.push('断流的两个逻辑投影一起释放；失效槽位不随焦点重试，显式重连重新取得完整快照');
});

test('凭据失效释放旧流与投影，仅用新订阅发布后续贡献', async () => {
  await fixture([workspace(gatewayA, 'workspace-a'), workspace(gatewayB, 'workspace-b')], async f => {
    await until(() => f.active().length === 2 && globalTitles().length === 2);
    const previous = [...f.active()];
    m.invalidateGatewayToken(0);
    expect(globalTitles()).toEqual([]);
    await until(() => f.streams.length === 4 && f.active().length === 2 && globalTitles().length === 2);
    expect(previous.map(stream => stream.active)).toEqual([false, false]);
    expect(f.errors).toEqual([]);
  });
  checks.push('原凭据撤销事件取消两条旧reader及其投影，新流不复用旧资格');
});

afterAll(async () => {
  await writeFile(join(run.artifactsDir, 'subscriptions.json'), JSON.stringify({
    boundary: '真实PluginCommands、原贡献Registry与SSE消费；编辑器事件、上下文键与HTTP响应为具名DI，不替代真实浏览器连接容量验收', checks,
  }, null, 2) + '\n');
});
