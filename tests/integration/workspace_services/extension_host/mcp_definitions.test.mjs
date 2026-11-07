import { afterAll, expect, test } from 'bun:test';
import { createHash } from 'node:crypto';
import { readFile, writeFile } from 'node:fs/promises';
import { join, resolve } from 'node:path';
import { build } from '../../../../src/clients/workbench/node_modules/esbuild/lib/main.js';
import { buildSourcePlugin, pinnedUpstreamCommit, sourceTransform } from '../../../../src/clients/workbench/scripts/sourceTransform.mjs';
import { TestRunContext } from '../../../harness/js/run-context.mjs';

const testFile = 'tests/integration/workspace_services/extension_host/mcp_definitions.test.mjs';
const run = await TestRunContext.fromTestFile(testFile).prepare(), root = resolve('src/clients/workbench');
const modules = {};
for (const runtime of ['browser', 'node']) {
  const bundle = join(run.artifactsDir, `${runtime}.provider.fixture.mjs`);
  await build({ stdin: { loader: 'ts', resolveDir: root, contents: `
export { ${runtime === 'node' ? 'NodeExtHostMpcService as ExtHostMcpService' : 'ExtHostMcpService'} } from './upstream/src/vs/workbench/api/${runtime === 'node' ? 'node/extHostMcpNode' : 'common/extHostMcp'}.js';
export { Emitter } from './upstream/src/vs/base/common/event.js';
export { CancellationToken, CancellationTokenSource } from './upstream/src/vs/base/common/cancellation.js';
export { McpStdioServerDefinition, McpHttpServerDefinition } from './upstream/src/vs/workbench/api/common/extHostTypes.js';
export { McpServerLaunch } from './upstream/src/vs/workbench/contrib/mcp/common/mcpTypes.js';
` }, outfile: bundle, bundle: true, format: 'esm', platform: 'node', target: 'es2024',
    plugins: [buildSourcePlugin(root, sourceTransform(root, { commit: pinnedUpstreamCommit }, [], { runtime }))],
    tsconfig: join(root, 'tsconfig.jsonc'), logLevel: 'silent' });
  modules[runtime] = await import(bundle);
}
const sourcePaths = [testFile, 'src/clients/workbench/scripts/mcpProviderTransform.mjs', 'src/clients/workbench/src/mcp/providerRegistration.ts'];
const fingerprints = Object.fromEntries(await Promise.all(sourcePaths.map(async path => [path, createHash('sha256').update(await readFile(path)).digest('hex')])));
const observations = [];
afterAll(async () => writeFile(join(run.artifactsDir, 'receipt.json'), JSON.stringify({ fingerprints, observations,
  boundary: '两种真实 sourceTransform 的原 ExtHost 注册与 resolve 入口；扩展 Provider 和 MainThread RPC 为具名替身，不代替真实 Gateway／Node 进程／MCP SDK 验收。' }, null, 2)));

for (const runtime of ['browser', 'node']) {
  const m = modules[runtime];
  function host(provider) {
    const snapshots = [], invalidations = [], errors = [];
    const service = new m.ExtHostMcpService({ getProxy: () => ({
      $upsertMcpCollection(value) { snapshots.push(value); }, $deleteMcpCollection(reference) { invalidations.push(reference); },
    }) }, { error(...args) { errors.push(args); } });
    const extension = { identifier: { value: 'probe.dynamic' }, name: 'dynamic', version: '1.0.0', extensionKey: 'probe.dynamic-1.0.0',
      installationTarget: { kind: 'backend', gatewayId: 'gateway_' + 'a'.repeat(32), backendId: '00000000-0000-4000-8000-000000000001' },
      contributes: { mcpServerDefinitionProviders: [{ id: 'definitions', label: 'Probe' }] } };
    const registration = service.registerMcpConfigurationProvider(extension, 'definitions', provider);
    return { service, registration, snapshots, invalidations, errors };
  }

  test(`${runtime}: 原注册只发送 metadata，同 label/version 的不同原对象各自 resolve`, async () => {
    const originals = [0, 1].map(index => new m.McpStdioServerDefinition('duplicate', '/native/secret-command', [String(index)], { TOKEN: 'provide-secret' }, 'same-version'));
    const received = [];
    const fixture = host({ provideMcpServerDefinitions: () => originals, resolveMcpServerDefinition: (original, token) => {
      expect(token.isCancellationRequested).toBe(false); received.push(original);
      return new m.McpStdioServerDefinition('resolved', '/native/runtime', [String(originals.indexOf(original))], { TOKEN: 'resolve-secret' });
    } });
    try {
      await fixture.service.$waitForInitialCollectionProviders();
      const snapshot = fixture.snapshots.at(-1);
      expect(snapshot.definitions.map(item => [item.label, item.version])).toEqual([['duplicate', 'same-version'], ['duplicate', 'same-version']]);
      expect(new Set(snapshot.definitions.map(item => item.definition_ref)).size).toBe(2);
      expect(JSON.stringify(snapshot)).not.toContain('secret'); expect(JSON.stringify(snapshot)).not.toContain('command');
      for (const index of [1, 0]) {
        const launch = await fixture.service.$resolveMcpLaunch(snapshot.provider_ref, snapshot.definitions[index].definition_ref, m.CancellationToken.None);
        expect(m.McpServerLaunch.fromSerialized(launch).args).toEqual([String(index)]);
      }
      expect(received).toEqual([originals[1], originals[0]]);
      expect(() => fixture.service.$startMcp(1, {})).toThrow('Workspace Owner');
      observations.push({ runtime, duplicate_refs_distinct: true, original_object_order: [1, 0], snapshot_secret_free: true });
    } finally { fixture.registration.dispose(); fixture.service.dispose(); }
  });

  test(`${runtime}: change 取消 provide 与 resolve，迟到的旧定义／秘密不能复活`, async () => {
    const changes = new m.Emitter();
    let finishProvide, finishResolve, provideToken, resolveToken, calls = 0;
    const original = new m.McpStdioServerDefinition('one', '/native/runtime');
    const fixture = host({ onDidChangeMcpServerDefinitions: changes.event,
      provideMcpServerDefinitions(token) { calls++; provideToken = token; return calls === 1 ? [original] : new Promise(resolve => { finishProvide = resolve; }); },
      resolveMcpServerDefinition(_original, token) { resolveToken = token; return new Promise(resolve => { finishResolve = resolve; }); },
    });
    try {
      await fixture.service.$waitForInitialCollectionProviders();
      const old = fixture.snapshots.at(-1), pending = fixture.service.$resolveMcpLaunch(old.provider_ref, old.definitions[0].definition_ref, m.CancellationToken.None);
      const rejection = pending.catch(error => error);
      changes.fire();
      expect(resolveToken.isCancellationRequested).toBe(true);
      expect(await rejection).toBeInstanceOf(Error);
      const late = finishProvide, oldToken = provideToken;
      changes.fire(); expect(oldToken.isCancellationRequested).toBe(true);
      late([new m.McpStdioServerDefinition('late', '/native/late', [], { TOKEN: 'late-secret' })]);
      finishResolve(new m.McpStdioServerDefinition('late-resolved', '/native/late', [], { TOKEN: 'late-secret' }));
      finishProvide([original]);
      await Promise.resolve(); await Promise.resolve(); await Promise.resolve();
      expect(fixture.snapshots.every(snapshot => !snapshot.definitions.some(item => item.label === 'late'))).toBe(true);
      await expect(fixture.service.$resolveMcpLaunch(old.provider_ref, old.definitions[0].definition_ref, m.CancellationToken.None)).rejects.toThrow();
      expect(fixture.invalidations.filter(value => value === old.provider_ref).length).toBeGreaterThanOrEqual(3);
    } finally { fixture.registration.dispose(); fixture.service.dispose(); changes.dispose(); }
  });

  test(`${runtime}: undefined、异常、调用取消与 dispose 保留明确语义`, async () => {
    const original = new m.McpStdioServerDefinition('one', '/native/runtime');
    let mode = 'undefined', observedToken;
    const fixture = host({ provideMcpServerDefinitions: () => [original], resolveMcpServerDefinition(_original, token) {
      observedToken = token;
      if (mode === 'error') throw new Error('provider resolve failure');
      if (mode === 'pending') return new Promise(() => {});
      return undefined;
    } });
    try {
      await fixture.service.$waitForInitialCollectionProviders();
      const snapshot = fixture.snapshots.at(-1), reference = snapshot.definitions[0].definition_ref;
      expect(await fixture.service.$resolveMcpLaunch(snapshot.provider_ref, reference, m.CancellationToken.None)).toBeUndefined();
      mode = 'error'; await expect(fixture.service.$resolveMcpLaunch(snapshot.provider_ref, reference, m.CancellationToken.None)).rejects.toThrow('provider resolve failure');
      mode = 'pending';
      const source = new m.CancellationTokenSource();
      const cancelled = fixture.service.$resolveMcpLaunch(snapshot.provider_ref, reference, source.token), rejected = cancelled.catch(error => error);
      source.cancel(); expect(observedToken.isCancellationRequested).toBe(true); expect(await rejected).toBeInstanceOf(Error); source.dispose();
      const disposed = fixture.service.$resolveMcpLaunch(snapshot.provider_ref, reference, m.CancellationToken.None), disposedRejection = disposed.catch(error => error);
      fixture.registration.dispose(); expect(observedToken.isCancellationRequested).toBe(true); expect(await disposedRejection).toBeInstanceOf(Error);
      await expect(fixture.service.$resolveMcpLaunch(snapshot.provider_ref, reference, m.CancellationToken.None)).rejects.toThrow();
    } finally { fixture.registration.dispose(); fixture.service.dispose(); }
  });
}
