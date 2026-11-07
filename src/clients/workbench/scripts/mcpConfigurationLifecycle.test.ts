import { expect, test } from 'bun:test';
import { readFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import ts from 'typescript';
import { CancellationTokenSource } from '../upstream/src/vs/base/common/cancellation.js';

// 执行原服务方法，隔离宿主 DI；身份监听和通知结束时机仍由生产方法决定。
const path = resolve('src/clients/workbench/src/mcp/service.ts');
const source = ts.createSourceFile(path, await readFile(path, 'utf8'), ts.ScriptTarget.Latest, true);
const service = source.statements.find(node => ts.isClassDeclaration(node) && node.name?.text === 'WorkspaceMcpWorkbenchService');
if (!service || !ts.isClassDeclaration(service)) throw new Error('原 MCP 管理服务声明缺失');
const method = service.members.find(node => ts.isMethodDeclaration(node) && node.name.getText() === 'install');
if (!method) throw new Error('原 MCP 安装方法缺失');
const executable = ts.transpileModule(`const operation = ${method.getText().replace(/^async install\(/, 'async function(')};`, {
  compilerOptions: { target: ts.ScriptTarget.ES2024, module: ts.ModuleKind.None },
}).outputText;
const target = { gateway_id: 'gateway_00000000000040008000000000000001', backend_id: '00000000-0000-4000-8000-000000000011',
  gateway_connection_id: 'original-connection', name: '原目标', connection_kind: 'remote_gateway' };

function prepare() {
  let snapshot = { gatewayId: target.gateway_id, workspaces: [] as { status: string }[], activeId: 'original-focus' };
  let invalidated: (() => void) | undefined, changed: (() => void) | undefined;
  let releasedIdentity = 0, releasedContext = 0;
  let signal: AbortSignal, guard: () => void, endOffer: () => void;
  const errors: unknown[] = [], signals: AbortSignal[] = [];
  const make = new Function('CancellationTokenSource', 'ProgressLocation', 'publishMcpArtifact', 'offerMcpArtifactConfiguration',
    'onGatewayUserSessionInvalidated', 'workspaceContext', 'mcpManagementTarget', `${executable}\nreturn operation;`);
  const install = make(CancellationTokenSource, { Notification: 'notification' },
    async (_gallery: unknown, route: typeof target, _label: string, _input: unknown, _progress: unknown, _token: unknown, original: AbortSignal) => {
      signals.push(original); expect(route).toMatchObject({ kind: 'backend', gateway_id: target.gateway_id, backend_id: target.backend_id,
        gateway_connection_id: target.gateway_connection_id }); expect(route).not.toHaveProperty('workspace_id');
      return { artifact: {}, candidate: {}, configuration_inputs: [] };
    },
    async (_published: unknown, route: typeof target, _label: string, _input: unknown, _notify: unknown, check: () => void, original: AbortSignal) => {
      signals.push(original); signal = original; guard = check;
      expect(route).toMatchObject({ gateway_id: target.gateway_id, backend_id: target.backend_id, gateway_connection_id: target.gateway_connection_id });
      await new Promise<void>((resolve, reject) => { endOffer = resolve; original.addEventListener('abort', () => reject(original.reason), { once: true }); });
    },
    (_port: number, listener: () => void) => { invalidated = listener; return () => { ++releasedIdentity; invalidated = undefined; }; },
    { getSnapshot: () => snapshot, subscribe: (listener: () => void) => { changed = listener; return () => { ++releasedContext; changed = undefined; }; } },
    async (selected: typeof target) => selected) as (this: object, server: object) => Promise<object>;
  const host = { owner: { installationTarget: () => ({ target, label: '原目标' }) }, quickInput: {},
    progress: { withProgress: (_options: unknown, run: (progress: object) => Promise<unknown>) => run({}) },
    notifications: { error: (cause: unknown) => errors.push(cause) } };
  const server = { gallery: {} };
  const settle = async () => { await Promise.resolve(); await Promise.resolve(); await Promise.resolve(); };
  return { run: () => install.call(host, server), server, errors, signals, settle,
    signal: () => signal, guard: () => guard(), releases: () => [releasedIdentity, releasedContext],
    invalidate: () => invalidated!(), dismiss: () => endOffer(),
    update: (change: Partial<typeof snapshot>) => { snapshot = { ...snapshot, ...change }; changed!(); },
  };
}

test.each(['identity', 'root-gateway'])('原安装通知仍可交互期间，%s 失效中止安装与配置共用信号并释放监听', async kind => {
  const fixture = prepare();
  expect(await fixture.run()).toBe(fixture.server);
  expect(fixture.signals).toHaveLength(2); expect(fixture.signals[0]).toBe(fixture.signals[1]);
  expect(fixture.releases()).toEqual([0, 0]);
  if (kind === 'identity') fixture.invalidate(); else fixture.update({ gatewayId: 'other-root-gateway' });
  await fixture.settle();
  expect(fixture.signal().aborted).toBe(true);
  expect(fixture.errors).toEqual([fixture.signal().reason]); expect(fixture.releases()).toEqual([1, 1]);
});

test('无工作区也能发布；切换焦点及工作区停止均不改变安装域，关闭通知释放同一身份生命周期', async () => {
  const fixture = prepare(); await fixture.run();
  fixture.update({ activeId: 'other-focus' }); expect(fixture.signal().aborted).toBe(false); expect(() => fixture.guard()).not.toThrow();
  fixture.update({ workspaces: [{ status: 'stopped' }] });
  expect(() => fixture.guard()).not.toThrow();
  fixture.dismiss(); await fixture.settle();
  expect(fixture.errors).toEqual([]); expect(fixture.signal().aborted).toBe(true); expect(fixture.releases()).toEqual([1, 1]);
});
