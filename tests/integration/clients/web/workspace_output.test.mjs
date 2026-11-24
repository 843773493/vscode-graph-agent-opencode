import { afterAll, afterEach, expect, test } from 'bun:test';
import { createHash } from 'node:crypto';
import { readFile, writeFile } from 'node:fs/promises';
import { join } from 'node:path';
import ts from '../../../../src/clients/workbench/node_modules/typescript/lib/typescript.js';
import { Disposable, DisposableStore, MutableDisposable } from '../../../../src/clients/workbench/upstream/src/vs/base/common/lifecycle.js';
import { CancellationTokenSource } from '../../../../src/clients/workbench/upstream/src/vs/base/common/cancellation.js';
import { Emitter } from '../../../../src/clients/workbench/upstream/src/vs/base/common/event.js';
import { URI } from '../../../../src/clients/workbench/upstream/src/vs/base/common/uri.js';
import { VSBuffer } from '../../../../src/clients/workbench/upstream/src/vs/base/common/buffer.js';
import { joinPath } from '../../../../src/clients/workbench/upstream/src/vs/base/common/resources.js';
import { t } from '../../../../src/clients/shared/core/i18n/index.js';
import { errorSummary } from '../../../../src/clients/web/src/utils/errorMessage.js';
import { getGatewayDiagnostics, locateGatewayDiagnosticLog, queryGatewayDiagnosticLogs } from '../../../../src/clients/web/src/gatewayApi.js';
import { installGatewayFetch, restoreGatewayTestEnvironment } from '../../../../src/clients/web/src/tests/fixtures/session/gatewayEnvironment.ts';
import { TestRunContext } from '../../../harness/js/run-context.mjs';

const run = await TestRunContext.fromTestFile('tests/integration/clients/web/workspace_output.test.mjs').prepare();
const path = 'src/clients/workbench/src/workspaceOutput.ts', source = await readFile(path, 'utf8');
const parsed = ts.createSourceFile(path, source, ts.ScriptTarget.Latest, true);
await writeFile(join(run.artifactsDir, 'workspaceOutput.ts'), source);
await writeFile(join(run.artifactsDir, 'source-fingerprint.json'), JSON.stringify({ path, sha256: createHash('sha256').update(source).digest('hex') }));
const names = [], registered = [];
afterEach(restoreGatewayTestEnvironment);
afterAll(async () => { await run.writeResult(names.length === registered.length ? 'passed' : 'failed', { boundary: '实际 Output adapter 与 Web HTTP adapter；Gateway HTTP 回执、原生 Output/File/Editor/QuickInput 服务是具名受控边界，不替代真实产品 UI。', tests: names }); await run.close(); });

function compiled(names, environment, members) {
  const printer = ts.createPrinter();
  const declarations = names.map(name => {
    let node = parsed.statements.find(node => node.name?.text === name);
    if (members && ts.isClassDeclaration(node)) node = ts.factory.updateClassDeclaration(node, node.modifiers, node.name,
      node.typeParameters, node.heritageClauses, node.members.filter(member => members.includes(member.name?.getText(parsed))));
    return printer.printNode(ts.EmitHint.Unspecified, node, parsed).replace(/^export /gm, '');
  });
  const javascript = ts.transpile(declarations.join('\n'), { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.None });
  return new Function(...Object.keys(environment), `${javascript}; return {${names.join(',')}};`)(...Object.values(environment));
}
const base = { Disposable, DisposableStore, MutableDisposable, CancellationTokenSource, URI, VSBuffer, joinPath, t, errorSummary,
  OUTPUT_VIEW_ID: 'output', OutputChannelUpdateMode: { Replace: 1 }, StatusbarAlignment: { RIGHT: 1 },
  getGatewayDiagnostics, locateGatewayDiagnosticLog, queryGatewayDiagnosticLogs, DEFAULT_API_REQUEST_TIMEOUT_MS: 1500 };
const { diagnosticLogSelection } = compiled(['diagnosticLogSelection'], base);
const location = (start = 2) => ({ scope: { source: 'gateway', workspace_ids: [] }, reference: {
  gateway_id: 'gateway-original', log_id: 'opaque-log', device: '1', inode: '2', line_start: 42, line_end: 80,
  content_version: 'a'.repeat(64), start_codepoint: start, end_codepoint: start + 2,
} });
const log = (overrides = {}) => ({ log_id: 'opaque-log', source: 'gateway', workspace_id: null, service: 'gateway', label: '原日志',
  status: 'available', tail: '原尾部\r\n', truncated: false, ...overrides });
const describe = () => ({ log_id: 'opaque-log', source: 'gateway', service: 'gateway', label: '原日志', workspace_ids: [], status: 'available' });
const success = data => Response.json({ data, request_id: 'req_diagnostic_transport' });
function fixture(overrides = {}) {
  let visible = true, selected, nativeText = '', runtime, target = { gateway_id: 'gateway-original', gateway_connection_id: 'connection-A' };
  let snapshot = { gatewayId: 'entry', workspaces: [] };
  const errors = [], writes = [], selections = [], statuses = [], calls = [], channels = new Map(), fileText = new Map();
  const content = new Emitter(), dispose = new Emitter();
  const model = { getValue: () => nativeText, onDidChangeContent: content.event, onWillDispose: dispose.event };
  const environment = { ...base, workspaceContext: { getSnapshot: () => snapshot, ensureReady: async () => {} },
    managementGateway: () => target, managementWorkspace: () => { throw new Error('已选工作区不存在'); },
    selectManagementTarget: (_scope, value) => { target = value; }, listManagementTargets: async () => [target],
    createManagementTargetLifecycle: () => { const abort = new AbortController(); return { signal: abort.signal, check() { abort.signal.throwIfAborted(); }, dispose() { abort.abort(); } }; },
    watchPluginRuntimeStatus: async (_target, _signal, callback) => { runtime = callback; }, ...overrides };
  const methods = ['idFor', 'updateScope', 'clearScope', 'showStatus', 'writeText', 'readSelected', 'openLog', 'locate', 'reveal', 'findRequest'];
  const { WorkspaceOutput } = compiled(['WorkspaceOutput'], { ...environment, diagnosticLogSelection }, methods);
  const view = new WorkspaceOutput();
  Object.assign(view, { sources: new Map(), status: new MutableDisposable(), refresh: { cancel() {}, schedule() {} },
    workspaceScopedContext: { set() {}, reset() {} }, views: { isViewVisible: () => visible, openView: async () => { visible = true; return {}; } },
    notifications: { error: error => errors.push(error) }, environment: { windowLogsPath: URI.parse('vscode-log:/window') },
    files: { writeFile: async (resource, buffer) => { const value = buffer.toString(); writes.push(value); fileText.set(resource.toString(), value); } },
    statusbar: { addEntry: entry => { statuses.push(entry.text); return { dispose() {} }; } },
    models: { createModelReference: async () => ({ object: { textEditorModel: model }, dispose() {} }) },
    codeEditors: { listCodeEditors: () => [{ getModel: () => model, setSelection: selection => selections.push(selection), revealRangeInCenter() {} }] },
    registry: { registerChannel: channel => channels.set(channel.id, { ...channel, uri: URI.parse(`output:/${channel.id}`), update() { nativeText = fileText.get(channel.source.resource.toString()); content.fire({}); } }), removeChannel: id => channels.delete(id) },
    output: { getActiveChannel: () => channels.get(selected), getChannel: id => channels.get(id), showChannel: async id => { selected = id; } },
    quickInput: { pick: async choices => { calls.push(choices); return choices[0]; } },
  });
  return { view, errors, writes, selections, statuses, calls, get text() { return nativeText; }, get runtime() { return runtime; },
    setSnapshot(value) { snapshot = value; }, hide() { visible = false; view.updateScope(); }, close() { view.clearScope(); view.status.dispose(); view.dispose(); content.dispose(); dispose.dispose(); } };
}
function scenario(name, execute) { registered.push(name); test(name, async () => { await execute(); names.push(name); }); }

scenario('原 Output 打开等待真实尾读，正文不混入空日志或截断状态', async () => {
  const requests = []; let current = log({ truncated: true });
  installGatewayFetch(({ path, url }) => { expect(path).toBe('/api/gateway/diagnostics'); requests.push(new URL(url, 'http://local'));
    return success({ gateway_id: 'gateway-original', logs: [current], selected_log_id: current.log_id, workspaces: [] }); });
  const f = fixture();
  try {
    await f.view.openLog({ gateway_id: 'gateway-original', gateway_connection_id: 'connection-A', log_id: 'opaque-log' });
    expect(f.text).toBe('原尾部\r\n'); expect(f.text).not.toContain('尾部日志');
    expect(f.statuses.at(-1)).toContain(t('gateway.diagnostics.latestTailOnly'));
    expect(requests[0].searchParams.get('include_tail')).toBe('false');
    expect(requests[1].searchParams.get('log_id')).toBe('opaque-log');
    current = log({ status: 'empty', tail: '' }); await f.view.readSelected(false);
    expect(f.text).toBe(''); expect(f.statuses.at(-1)).toContain(t('gateway.diagnostics.emptyLog'));
    current = log({ status: 'unavailable', error: '原文件已删除' });
    await expect(f.view.readSelected(false)).rejects.toThrow('原文件已删除');
    expect(f.text).toBe(''); expect(f.errors).toEqual([]); expect(f.statuses.at(-1)).toContain('原文件已删除');
  } finally { f.close(); }
});

scenario('原行定位只用精确引用及明确连接，非 BMP 重复命中转换 UTF-16，尾读不覆盖定位', async () => {
  const requests = [], text = '开头😀词后续😀词';
  installGatewayFetch(({ path, url, init }) => { requests.push({ path, url, init });
    if (path.endsWith('/locate')) return success({ source: describe(), reference: JSON.parse(init.body).reference, text });
    return success({ gateway_id: 'gateway-original', logs: [log()], selected_log_id: 'opaque-log', workspaces: [] }); });
  const f = fixture();
  try {
    await f.view.openLog({ gateway_id: 'gateway-original', gateway_connection_id: 'connection-A', location: location(6) });
    expect(f.text).toBe(text); expect(f.selections.at(-1)).toEqual({ startLineNumber: 1, startColumn: 8, endLineNumber: 1, endColumn: 11 });
    expect(requests.map(request => request.path)).toEqual(['/api/gateway/diagnostics', '/api/gateway/diagnostics/locate']);
    expect(new URL(requests[1].url, 'http://local').searchParams.get('gateway_connection_id')).toBe('connection-A');
    expect(JSON.parse(requests[1].init.body)).toEqual(location(6));
    await f.view.readSelected(); expect(requests).toHaveLength(2);
    expect(diagnosticLogSelection(text, 2, 4).startColumn).toBe(3);
  } finally { f.close(); }
});

scenario('失效原行不回读尾部，保留原失败和诊断请求关联', async () => {
  const requests = [];
  installGatewayFetch(({ path }) => { requests.push(path);
    if (path.endsWith('/locate')) return Response.json({ detail: { message: '原行已轮转', code: 'stale_runtime_log_reference' } }, { status: 409, headers: { 'X-Request-ID': 'req_stale' } });
    return success({ gateway_id: 'gateway-original', logs: [log()], selected_log_id: 'opaque-log', workspaces: [] }); });
  const f = fixture();
  try { const error = await f.view.openLog({ gateway_id: 'gateway-original', location: location() }).catch(error => error);
    expect(error.message).toBe('原行已轮转'); expect(error.requestId).toBe('req_stale');
    expect(requests).toHaveLength(2); expect(f.text).toBe(''); expect(f.errors).toEqual([]);
  } finally { f.close(); }
});

scenario('关闭输出后取消读取，迟到原文不能发布', async () => {
  const pending = Promise.withResolvers(); let signal;
  installGatewayFetch(({ path, init }) => {
    if (path.endsWith('/locate')) { signal = init.signal; return pending.promise; }
    return success({ gateway_id: 'gateway-original', logs: [log()], selected_log_id: 'opaque-log', workspaces: [] }); });
  const f = fixture();
  try { const opening = f.view.openLog({ gateway_id: 'gateway-original', location: location() });
    while (!signal) await new Promise(resolve => setTimeout(resolve, 0));
    f.view.clearScope(); expect(signal.aborted).toBe(true);
    pending.resolve(success({ source: describe(), reference: location().reference, text: '开头😀词' }));
    await expect(opening).rejects.toThrow(); expect(f.writes).not.toContain('开头😀词');
  } finally { f.close(); }
});

scenario('未完成零命中不能表示无记录，续页只由用户选择，ID 子串和多候选不自动定位', async () => {
  let queryCount = 0;
  installGatewayFetch(({ path, init, url }) => {
    if (path.endsWith('/query')) { queryCount++; const body = JSON.parse(init.body);
      expect(new URL(url, 'http://local').searchParams.get('gateway_connection_id')).toBe('connection-A');
      expect(body.cursor).toBe(queryCount === 1 ? undefined : 'next');
      return success({ gateway_id: 'gateway-original', scope: body.scope, query: body.query, sources: [describe()],
        items: queryCount === 1 ? [] : [location(2), location(6)].map(item => ({ source: describe(), reference: item.reference, preview: 'req_10 req_1', preview_start_codepoint: 0 })),
        complete: queryCount === 2, cursor: queryCount === 1 ? 'next' : undefined, limited_reason: queryCount === 1 ? 'scan_limit' : undefined }); }
    if (path.endsWith('/locate')) throw new Error('取消候选选择时不得读原行');
    return success({ gateway_id: 'gateway-original', logs: [log()], selected_log_id: 'opaque-log', workspaces: [] });
  });
  const f = fixture(); f.view.quickInput.pick = async choices => { f.calls.push(choices); return choices[0]?.more ? choices[0] : undefined; };
  try { await f.view.openLog({ gateway_id: 'gateway-original', gateway_connection_id: 'connection-A', log_id: 'opaque-log', request_id: 'req_1' });
    expect(queryCount).toBe(2); expect(f.calls[0]).toHaveLength(1); expect(f.calls[1]).toHaveLength(2);
    expect(f.statuses.some(text => text.includes('scan_limit'))).toBe(true); expect(f.selections).toHaveLength(0);
  } finally { f.close(); }
});

scenario('已加载 CRLF 正文命中只在原模型选择，多次出现仍明确选择', async () => {
  installGatewayFetch(() => success({ gateway_id: 'gateway-original', logs: [log({ tail: '😀 req_1 req_10\r\n第二行 req_1\r\n' })], selected_log_id: 'opaque-log', workspaces: [] }));
  const f = fixture(); f.view.quickInput.pick = async choices => { f.calls.push(choices); return choices[2]; };
  try { await f.view.openLog({ gateway_id: 'gateway-original', log_id: 'opaque-log', request_id: 'req_1' });
    expect(f.calls[0]).toHaveLength(3); expect(f.selections.at(-1)).toEqual({ startLineNumber: 2, startColumn: 5, endLineNumber: 2, endColumn: 10 });
  } finally { f.close(); }
});

scenario('仅尾读来源不冒充可查询，已有工作区选择错误不被吞掉', async () => {
  installGatewayFetch(() => success({ gateway_id: 'gateway-original', logs: [log({ source: 'workspace', workspace_id: 'workspace-A' })], selected_log_id: 'opaque-log', workspaces: [] }));
  const f = fixture();
  try { await f.view.openLog({ gateway_id: 'gateway-original', log_id: 'opaque-log', request_id: 'not-here' });
    expect(f.statuses.at(-1)).toContain(t('bottom.gatewayLog.tailOnly')); expect(f.calls).toHaveLength(0);
    f.setSnapshot({ gatewayId: 'entry', workspaces: [{ gateway_id: 'gateway-original', workspace_id: 'other' }] });
    f.view.updateScope(true); await expect(f.view.loading).rejects.toThrow('已选工作区不存在');
  } finally { f.close(); }
});

scenario('隐藏输出时取消定位，模型异步更新完成前命令不能宣称读取成功', async () => {
  installGatewayFetch(() => success({ gateway_id: 'gateway-original', logs: [log()], selected_log_id: 'opaque-log', workspaces: [] }));
  const f = fixture();
  try {
    await f.view.openLog({ gateway_id: 'gateway-original', log_id: 'opaque-log' });
    const source = [...f.view.sources.values()][0], channel = f.view.output.getChannel(source.id), update = channel.update;
    let waiting = false, settled = false;
    channel.update = () => { waiting = true; };
    const pending = f.view.writeText(source, '异步模型正文', new AbortController().signal).then(() => { settled = true; });
    while (!waiting) await new Promise(resolve => setTimeout(resolve, 0));
    expect(settled).toBe(false); expect(source.text).toBe('原尾部\r\n');
    update(); await pending;
    expect(settled).toBe(true); expect(source.text).toBe('异步模型正文');
    f.hide(); expect(f.view.scope.abort.signal.aborted).toBe(true);
  } finally { f.close(); }
});

scenario('换通道时取消正在尾读的旧命令，迟到旧正文不能写入', async () => {
  const pending = Promise.withResolvers(); let signal;
  installGatewayFetch(({ url, init }) => {
    if (new URL(url, 'http://local').searchParams.get('include_tail') !== 'false') { signal = init.signal; return pending.promise; }
    return success({ gateway_id: 'gateway-original', logs: [log()], selected_log_id: 'opaque-log', workspaces: [] });
  });
  const f = fixture();
  try {
    const opening = f.view.openLog({ gateway_id: 'gateway-original', log_id: 'opaque-log' });
    while (!signal) await new Promise(resolve => setTimeout(resolve, 0));
    f.view.navigation.abort.abort(); expect(signal.aborted).toBe(true);
    pending.resolve(success({ gateway_id: 'gateway-original', logs: [log({ tail: '迟到尾部' })], selected_log_id: 'opaque-log', workspaces: [] }));
    await expect(opening).rejects.toThrow(); expect(f.writes).not.toContain('迟到尾部');
  } finally { f.close(); }
});

scenario('工作区插件日志仍核对 Gateway descriptor，实例替换废弃原通道与迟到尾读', async () => {
  const pending = Promise.withResolvers(); let tailSignal, hold = false;
  installGatewayFetch(({ url, init }) => {
    const request = new URL(url, 'http://local');
    if (hold && request.searchParams.get('include_tail') !== 'false') { tailSignal = init.signal; return pending.promise; }
    return success({ gateway_id: 'gateway-original', logs: [log()], selected_log_id: 'opaque-log', workspaces: [] });
  });
  const workspace = { gateway_id: 'gateway-original', workspace_id: 'workspace-A' };
  const f = fixture({ managementWorkspace: () => workspace });
  f.setSnapshot({ gatewayId: 'entry', workspaces: [workspace] });
  try {
    await f.view.openLog({ gateway_id: 'gateway-original', log_id: 'opaque-log' });
    f.runtime({ plugins: [{ log_id: 'opaque-log', binding: { workspace_id: 'workspace-A', plugin_id: 'workspace-plugin' } }] });
    const original = [...f.view.sources.values()][0];
    expect(original.plugin).toBe(true); expect(original.queryScope).toEqual({ source: 'gateway', workspace_ids: [] });
    hold = true;
    const reading = f.view.readSelected(false);
    while (!tailSignal) await new Promise(resolve => setTimeout(resolve, 0));
    f.runtime({ plugins: [] }); expect(tailSignal.aborted).toBe(true); expect(f.view.sources.size).toBe(0);
    pending.resolve(success({ gateway_id: 'gateway-original', logs: [log({ tail: '被替换实例迟到正文' })], selected_log_id: 'opaque-log', workspaces: [] }));
    await reading; expect(f.writes).not.toContain('被替换实例迟到正文');
  } finally { f.close(); }
});
