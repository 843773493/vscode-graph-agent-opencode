import { afterAll, expect, test } from 'bun:test';
import { writeFile } from 'node:fs/promises';
import { join, resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import { build } from '../../../../src/clients/workbench/node_modules/esbuild/lib/main.js';
import { buildSourcePlugin, pinnedUpstreamCommit, sourceTransform } from '../../../../src/clients/workbench/scripts/sourceTransform.mjs';
import { TestRunContext } from '../../../harness/js/run-context.mjs';

const run = await TestRunContext.fromTestFile('tests/integration/workspace_services/extension_host/extension_diagnostics.test.mjs').prepare();
const root = resolve('src/clients/workbench'), bundle = join(run.artifactsDir, 'diagnostics.fixture.mjs');
const upstream = './src/clients/workbench/upstream/src/vs/';
const transform = sourceTransform(root, { commit: pinnedUpstreamCommit }, []);
const built = await build({ stdin: { contents: `
export { MainThreadDiagnostics } from '${upstream}workbench/api/browser/mainThreadDiagnostics.js';
export { ExtHostDiagnostics } from '${upstream}workbench/api/common/extHostDiagnostics.js';
export { ExtHostFileSystemInfo } from '${upstream}workbench/api/common/extHostFileSystemInfo.js';
export { MainContext, ExtHostContext } from '${upstream}workbench/api/common/extHost.protocol.js';
export { MarkerService } from '${upstream}platform/markers/common/markerService.js';
export { ExtensionIdentifier } from '${upstream}platform/extensions/common/extensions.js';
export { NullLogService } from '${upstream}platform/log/common/log.js';
export { RPCProtocol } from '${upstream}workbench/services/extensions/common/rpcProtocol.js';
export { URI } from '${upstream}base/common/uri.js';
export { Emitter } from '${upstream}base/common/event.js';
export { TargetUriTransformer } from './src/clients/workbench/src/extensions/targetUris.js';
`, resolveDir: resolve('.') }, outfile: bundle, bundle: true, format: 'esm', platform: 'node', target: 'es2024', metafile: true,
  plugins: [buildSourcePlugin(root, transform)], tsconfig: join(root, 'tsconfig.jsonc'), logLevel: 'silent' });
await writeFile(join(run.artifactsDir, 'bundle-meta.json'), JSON.stringify(built.metafile));
const m = await import(pathToFileURL(bundle).href);
const checks = [];

function target(label, workspaceIds = [label.toLowerCase()]) {
  const binding = { gateway_id: 'gateway-' + label, backend_id: 'backend-' + label, view_id: 'default',
    initialization: { os: 'linux', logs_location_uri: 'file:///hosts/' + label + '/logs',
      environment: { app_root_uri: 'file:///hosts/' + label + '/app', global_storage_home_uri: 'file:///hosts/' + label + '/global',
        workspace_storage_home_uri: 'file:///hosts/' + label + '/workspace' },
      workspace: { id: 'view-' + label, folders: workspaceIds.map(workspace_id => ({ workspace_id, name: workspace_id,
        resource_uri: 'boxteam://workspace/' + workspace_id + '/resources/files/root', native_uri: 'file:///targets/' + label + '/' + workspace_id })) } } };
  const uriTransformer = new m.TargetUriTransformer(binding, () => []);
  return { binding, uriTransformer, runningLocation: { kind: 3 },
    resource: m.URI.joinPath(uriTransformer.folders[0].uri, 'main.ts') };
}
const marker = (message, extra = {}) => ({ message, severity: 8, startLineNumber: 1, startColumn: 1, endLineNumber: 1, endColumn: 2, ...extra });
const identity = { asCanonicalUri: uri => uri };
const tick = () => new Promise(resolve => setTimeout(resolve, 0));
function bridge(host, markers) {
  const events = [];
  const main = new m.MainThreadDiagnostics({ extensionHost: host, getProxy: () => ({ $acceptMarkersChange: data => events.push(data) }) }, markers, identity);
  return { main, events };
}

test('同名诊断集合清空与关闭只撤销所属宿主，重叠视图与新版代际互不覆盖', async () => {
  const markers = new m.MarkerService(), a = target('A'), b = target('B');
  const first = bridge(a, markers), second = bridge(b, markers), replacement = bridge(a, markers);
  try {
    first.main.$changeMany('shared', [[a.resource, [marker('A')], true]]);
    second.main.$changeMany('shared', [[b.resource, [marker('B')], false]]);
    replacement.main.$changeMany('shared', [[a.resource, [marker('A replacement')], false]]);
    expect(markers.read({ resource: a.resource }).map(value => value.message).sort()).toEqual(['A', 'A replacement']);
    expect(markers.isSourceTruncated(a.resource)).toBe(true);
    first.main.$clear('shared');
    expect(markers.read({ resource: b.resource }).map(value => value.message)).toEqual(['B']);
    expect(markers.read({ resource: a.resource }).map(value => value.message)).toEqual(['A replacement']);
    expect(markers.isSourceTruncated(a.resource)).toBe(false);
    first.main.dispose();
    expect(markers.read({ resource: a.resource }).map(value => value.message)).toEqual(['A replacement']);
    replacement.main.dispose();
    expect(markers.read({ resource: a.resource })).toHaveLength(0);
    expect(markers.read({ resource: b.resource }).map(value => value.message)).toEqual(['B']);
    checks.push('同名 collection、重叠 view、新旧代际、clear/dispose 与原截断标记独立');
  } finally { first.main.dispose(); second.main.dispose(); replacement.main.dispose(); markers.dispose(); }
  await tick();
});

test('窗口诊断事件只发送所属视图，并投影关联位置而不修改原 MarkerService 正文', async () => {
  const markers = new m.MarkerService(), a = target('A', ['a1', 'a2']), b = target('B');
  const { main, events } = bridge(a, markers);
  const a2 = m.URI.joinPath(a.uriTransformer.folders[1].uri, 'related.ts');
  const related = resource => ({ resource, message: 'related', startLineNumber: 1, startColumn: 1, endLineNumber: 1, endColumn: 2 });
  try {
    const webHelp = m.URI.parse('https://example.com/related'), customHelp = m.URI.parse('probe-help:/related'), nativeHelp = m.URI.file('/hosts/A/app/help.md');
    markers.changeOne('language', a.resource, [marker('A', { relatedInformation: [related(a2), related(b.resource), related(webHelp), related(customHelp), related(nativeHelp)],
      code: { value: 'foreign-link', target: b.resource } }), marker('docs', { code: { value: 'docs', target: m.URI.parse('https://example.com/help') } }),
      marker('/etc/hosts: diagnostic body', { code: { value: 'native-help', target: m.URI.file('/hosts/A/app/help.md') } }),
      marker('unbound native link', { code: { value: 'native-unbound', target: m.URI.file('/other-target/private.md') } })]);
    markers.changeOne('language', b.resource, [marker('B')]);
    await tick();
    expect(events).toHaveLength(1);
    expect(events[0].map(([uri]) => uri.toString())).toEqual([a.resource.toString()]);
    const sent = events[0][0][1];
    expect(sent[0].relatedInformation.map(value => value.resource.toString())).toEqual([a2, webHelp, customHelp, nativeHelp].map(uri => uri.toString()));
    expect(sent[0].code).toBe('foreign-link');
    expect(sent[1].code.target.toString()).toBe('https://example.com/help');
    expect(sent[2].message).toBe('/etc/hosts: diagnostic body');
    expect(sent[2].code.target.toString()).toBe(m.URI.file('/hosts/A/app/help.md').toString());
    expect(sent[3].code).toBe('native-unbound');
    const original = markers.read({ resource: a.resource });
    expect(original[0].relatedInformation).toHaveLength(5);
    expect(original[0].code.target.toString()).toBe(b.resource.toString());
    events.length = 0;
    main.$changeMany('own', [[a.resource, [marker('self')], false]]);
    await tick();
    expect(events[0][0][1].map(value => value.message)).toEqual(['A', 'docs', '/etc/hosts: diagnostic body', 'unbound native link']);
    checks.push('原 MicrotaskEmitter 的批量事件投影；多根关联位置与文档链接保留，原正文不变，自有诊断不回声');
  } finally { main.dispose(); markers.dispose(); }
});

test('诊断批次先核全部位置，非法资源与关闭代际没有部分写入或迟到清空', async () => {
  const markers = new m.MarkerService(), a = target('A'), b = target('B');
  const { main } = bridge(a, markers);
  const invalidRelated = marker('foreign related', { relatedInformation: [{ resource: b.resource, message: 'B',
    startLineNumber: 1, startColumn: 1, endLineNumber: 1, endColumn: 2 }] });
  try {
    expect(() => main.$changeMany('batch', [[a.resource, [marker('valid')], false], [b.resource, [marker('invalid')], false]])).toThrow('视图');
    expect(markers.read({})).toHaveLength(0);
    expect(() => main.$changeMany('batch', [[a.resource, [invalidRelated], false]])).toThrow('视图');
    expect(markers.read({})).toHaveLength(0);
    expect(() => main.$changeMany('batch', [[a.resource, [marker('invalid link', { code: { value: 'x', target: b.resource } })], false]])).toThrow('视图');
    expect(markers.read({})).toHaveLength(0);
    const related = [m.URI.parse('https://example.com/related'), m.URI.parse('probe-help:/related'), m.URI.file('/hosts/A/app/help.md')]
      .map(resource => ({ resource, message: 'help', startLineNumber: 1, startColumn: 1, endLineNumber: 1, endColumn: 2 }));
    main.$changeMany('help', [[a.resource, [marker('native help', { relatedInformation: related, code: { value: 'x', target: m.URI.file('/hosts/A/app/help.md') } })], false]]);
    expect(markers.read({ resource: a.resource })[0].code.target.toString()).toBe(m.URI.file('/hosts/A/app/help.md').toString());
    expect(markers.read({ resource: a.resource })[0].relatedInformation.map(value => value.resource.toString())).toEqual(related.map(value => value.resource.toString()));
    main.$changeMany('batch', [[a.resource, [marker('valid')], false]]);
    main.dispose();
    expect(() => main.$changeMany('batch', [[a.resource, [marker('late')], false]])).toThrow('关闭');
    expect(() => main.$clear('batch')).toThrow('关闭');
    expect(markers.read({})).toHaveLength(0);
    checks.push('整批预检无部分发布；关联位置不跨目标，dispose 后原 RPC 方法拒绝写入和清空');
  } finally { main.dispose(); markers.dispose(); }
  await tick();
});

test('Web 诊断保留原 owner、原生及非文件资源与完整关联正文', async () => {
  const markers = new m.MarkerService(), { main, events } = bridge({ runningLocation: { kind: 2 } }, markers);
  const resource = m.URI.parse('untitled:Untitled-1'), related = m.URI.file('/tmp/related.ts');
  try {
    main.$changeMany('web-owner', [[resource, [marker('web', { relatedInformation: [{ resource: related, message: 'related',
      startLineNumber: 1, startColumn: 1, endLineNumber: 1, endColumn: 2 }] })], false]]);
    expect(markers.read({ owner: 'web-owner' })).toHaveLength(1);
    markers.changeOne('language', related, [marker('native')]);
    await tick();
    expect(events.flatMap(data => data.map(([uri]) => uri.toString()))).toContain(related.toString());
    expect(markers.read({ resource })[0].relatedInformation[0].resource.toString()).toBe(related.toString());
    main.$clear('web-owner');
    expect(markers.read({ owner: 'web-owner' })).toHaveLength(0);
    expect(markers.read({ owner: 'language' })).toHaveLength(1);
    checks.push('Web 沿原 owner 与共享窗口诊断、untitled/native URI 合同');
  } finally { main.dispose(); markers.dispose(); }
});

test('原公开 DiagnosticCollection 经真实 RPC 和 typed URI 转换发布、镜像和清空', async () => {
  const markers = new m.MarkerService(), a = target('A'), b = target('B'), resources = [];
  function connect(host) {
    const toMain = new m.Emitter(), toHost = new m.Emitter();
    const mainRpc = new m.RPCProtocol({ onMessage: toMain.event, send: data => queueMicrotask(() => toHost.fire(data)), drain: async () => {} }, null, host.uriTransformer);
    const hostRpc = new m.RPCProtocol({ onMessage: toHost.event, send: data => queueMicrotask(() => toMain.fire(data)), drain: async () => {} });
    const main = new m.MainThreadDiagnostics({ extensionHost: host, getProxy: mainRpc.getProxy.bind(mainRpc) }, markers, identity);
    const diagnostics = new m.ExtHostDiagnostics(hostRpc, new m.NullLogService(), new m.ExtHostFileSystemInfo(), { getDocument: () => undefined });
    mainRpc.set(m.MainContext.MainThreadDiagnostics, main);
    hostRpc.set(m.ExtHostContext.ExtHostDiagnostics, diagnostics);
    resources.push(() => { main.dispose(); mainRpc.dispose(); hostRpc.dispose(); toMain.dispose(); toHost.dispose(); });
    return diagnostics;
  }
  const first = connect(a), second = connect(b);
  const aFile = a.uriTransformer.transformOutgoingURI(a.resource), bFile = b.uriTransformer.transformOutgoingURI(b.resource);
  const range = { start: { line: 0, character: 0 }, end: { line: 0, character: 1 } };
  const aCollection = first.createDiagnosticCollection(new m.ExtensionIdentifier('probe.shared'), 'same');
  const bCollection = second.createDiagnosticCollection(new m.ExtensionIdentifier('probe.shared'), 'same');
  try {
    aCollection.set(aFile, [{ range, message: 'A API', severity: 0 }]);
    bCollection.set(bFile, [{ range, message: 'B API', severity: 0 }]);
    await tick(); await tick();
    expect(markers.read({ resource: a.resource }).map(value => value.message)).toEqual(['A API']);
    expect(markers.read({ resource: b.resource }).map(value => value.message)).toEqual(['B API']);
    markers.changeOne('language', b.resource, [marker('B mirror')]);
    await tick(); await tick();
    expect(first.getDiagnostics().flatMap(([, data]) => data.map(value => value.message))).toEqual(['A API']);
    expect(second.getDiagnostics(bFile).map(value => value.message).sort()).toEqual(['B API', 'B mirror']);
    aCollection.clear();
    await tick(); await tick();
    expect(markers.read({ resource: a.resource })).toHaveLength(0);
    expect(markers.read({ resource: b.resource }).map(value => value.message)).toEqual(['B API', 'B mirror']);
    checks.push('真实 ExtHostDiagnostics/DiagnosticCollection/RPCProtocol/TargetUriTransformer/MarkerService；目标 file URI 往返与镜像清空，不伪造 owner');
  } finally { aCollection.dispose(); bCollection.dispose(); await tick(); resources.reverse().forEach(dispose => dispose()); markers.dispose(); }
});

afterAll(async () => {
  await writeFile(join(run.artifactsDir, 'receipt.json'), JSON.stringify({ checks,
    boundary: '真实原诊断与 MarkerService/RPC/URI 链；host binding、字节传输和文档版本服务为具名装具，未运行真实 Gateway、Node 进程或浏览器页面。' }, null, 2));
  await run.close();
});
