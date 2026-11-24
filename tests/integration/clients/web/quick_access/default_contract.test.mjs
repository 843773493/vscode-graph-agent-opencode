import { afterAll, beforeAll, expect, test } from 'bun:test';
import { join, resolve } from 'node:path';
import { chromium } from 'playwright';
import { build } from '../../../../../src/clients/workbench/node_modules/esbuild/lib/main.js';
import { buildSourcePlugin, pinnedUpstreamCommit, sourceTransform } from '../../../../../src/clients/workbench/scripts/sourceTransform.mjs';
import { TestRunContext } from '../../../../harness/js/run-context.mjs';

const run = await TestRunContext.fromTestFile('tests/integration/clients/web/quick_access/default_contract.test.mjs').prepare();
const sharedWorkspaceId = '019a0000-0000-7000-8000-000000000001';
const federatedOwners = ['gateway_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', 'gateway_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb']
  .map(gateway_id => ({ gateway_id, workspace_id: sharedWorkspaceId }));
const [ownerAKey, ownerBKey] = federatedOwners.map(owner => `${owner.gateway_id}/${owner.workspace_id}`);
const workbench = resolve('src/clients/workbench');
const bundles = new Map([['/default-contract.mjs', 'default_contract.fixture.mjs'], ['/composer-selection.mjs', 'composer_selection.fixture.mjs']]);
for (const [path, entry] of bundles) await build({ entryPoints: [resolve('tests/integration/clients/web/quick_access', entry)],
  outfile: join(run.artifactsDir, path.slice(1)), bundle: true, format: 'esm', platform: 'browser', target: 'es2024',
  tsconfig: join(workbench, 'tsconfig.jsonc'),
  plugins: [buildSourcePlugin(workbench, sourceTransform(workbench, { commit: pinnedUpstreamCommit }, []))], logLevel: 'silent' });
// 本地静态装具避免每次经浏览器控制通道传输数 MB 的 base64 源码。
const server = Bun.serve({ hostname: '127.0.0.1', port: 0, fetch(request) {
  const path = new URL(request.url).pathname;
  if (path === '/') return new Response('<!doctype html><title>Quick Access 合同装具</title>', { headers: { 'content-type': 'text/html' } });
  if (bundles.has(path)) return new Response(Bun.file(join(run.artifactsDir, path.slice(1))), { headers: { 'content-type': 'text/javascript' } });
  return new Response('Not Found', { status: 404 });
} });
run.addCleanup('合同静态服务', () => server.stop(true));
let browser;
beforeAll(async () => {
  browser = await chromium.launch({ executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH ?? Bun.which('chromium'),
    headless: true, args: ['--no-sandbox', '--disable-dev-shm-usage'] });
  run.addCleanup('Chromium', () => browser.close());
});
afterAll(() => run.close());

async function exercise(name, argument) {
  const context = await browser.newContext({ permissions: ['clipboard-read', 'clipboard-write'] });
  try {
    const page = await context.newPage(), errors = [];
    page.on('pageerror', error => errors.push(String(error)));
    await page.goto(server.url.href);
    // 显式组件装具：浏览器运行真实 Provider，宿主及 Picker 事件替身不证明产品页面接线。
    const path = name === 'composerResourceSelectionContract' ? '/composer-selection.mjs' : '/default-contract.mjs';
    const result = await page.evaluate(async ({ name, argument, path }) => (await import(path))[name](argument), { name, argument, path });
    expect(errors).toEqual([]);
    return result;
  } finally { await context.close(); }
}

test('配置保存原FileService保留cause，原保存通知显示原因和HTTP诊断且保留动作', async () => {
  const result = await exercise('configurationSaveFailureContract');
  expect(result.causePreserved).toBe(true);
  expect(result.originalOptions).toBe(true);
  expect(result.result).toBe(result.expectedResult);
  expect(result.visible).toEqual({ count: 1, message: '配置修改失败，草稿已保留。原配置字段拒绝',
    actions: ['RetrySaveModelAction', 'SaveModelAsAction', 'RevertModelAction'] });
  expect(result.copied).toContain('request_id: original-source-request');
  expect(result.copied).toContain('source_uri: boxteam://workspace/original/resources/config/source');
  expect(result.closes).toEqual([{ uri: 'boxteam-config-source://original-gateway/workspace/original-workspace/source.jsonc', handle: true }]);
});

test('原生通知仅交付未报告失败，混合聚合保留诊断且独立操作仍可显示', async () => {
  const result = await exercise('notificationFailureContract');
  expect(result.afterReported).toBe(0);
  expect(result.visible.count).toBe(1);
  expect(result.afterAggregate).toBe(1);
  expect(result.count).toBe(2);
  expect(result.visible.message).toBe('操作与状态重读部分失败');
  expect(result.visible.actions).toContain('boxteam.notification.copyDiagnostic');
  expect(result.copied).toContain('状态重读拒绝');
  expect(result.copied).toContain('request_id: original-request');
  expect(result.copied).not.toContain('private-test-token');
  expect(result.independent.length).toBeLessThanOrEqual(160);
  expect(result.independent).not.toContain('original-request');
  expect(result.logs.every(message => !message.includes('private-test-token'))).toBe(true);
  expect(result.originalMessage.length).toBeGreaterThan(160);
  expect(result.originalDetail).toContain('private-test-token');
  expect(result.retainedCause).toBe(true);
});

for (const ready of [false, true]) test(`原生阅读恢复保留当前展示且不触发定位提示，明确跳转才进入聊天：${ready}`, async () => {
  const result = await exercise('viewportRestoreContract', ready);
  const position = { reading_anchor: null, follow_latest: true };
  expect(result.restored.location).toEqual({ kind: 'viewport', position });
  expect(result.restored.moves).toEqual([{ position, preview: true }]);
  expect(result.restored.shows).toEqual([]);
  expect(result.moves).toEqual([{ position, preview: true }, { position, preview: false }]);
  expect(result.shows).toEqual([false]); expect(result.reasons).toEqual([1, 4]);
});

test('Thread标签切换和关闭重开沿原memento恢复，分组隔离并遵守原设置与明确定位', async () => {
  const result = await exercise('threadViewStateContract');
  const first = { kind: 'viewport', position: { reading_anchor: null, follow_latest: true } };
  expect(result.switchedBack).toEqual(first); expect(result.reopened).toEqual(first);
  expect(result.partialOptions.threadLocation).toEqual(first);
  expect(result.partialOptions.resourcesVisible).toBe(true); expect(result.partialOptions.resourceTab).toBe('debug');
  expect(result.separateGroup).toEqual({ kind: 'viewport', position: { reading_anchor: null, follow_latest: false } });
  expect(result.newWithRestoreDisabled).toBeNull(); expect(result.existingWithRestoreDisabled).toEqual(first);
  expect(result.explicit).toEqual(first);
  // 原生恢复不产生明确跳转，仍让 HistoryService 区分 NAVIGATION 与 PROGRAMMATIC。
  expect(result.reasons).toEqual([4, 1, 1, 1, 1, 4]);
});

for (const accept of [false, true]) test(`大纲加载时取消预览仍恢复展示和阅读位置，后续接受优先：${accept}`, async () => {
  const result = await exercise('loadingPreviewRestoreContract', accept);
  expect(result.afterCancel).toEqual(['selection', 'preview-presentation', 'presentation']);
  expect(result.events).not.toContain('cancel');
  expect(result.events.filter(event => event === 'preview-presentation')).toHaveLength(1);
  expect(result.events.filter(event => event === 'conversation')).toHaveLength(accept ? 1 : 0);
  expect(result.moves).toHaveLength(2);
  expect(result.moves[1]).toEqual({ position: { reading_anchor: null, follow_latest: !accept }, preview: !accept });
  expect(result.closedLocation).toEqual({ kind: 'viewport', position: { reading_anchor: null, follow_latest: true } });
});

for (const category of ['会话', 'Thread', '文件', '会话消息', '工具结果', 'LLM 请求记录', 'Trace']) test(`${category}接受原owner规范URI，跨Gateway同名及独立位置交原Opener`, async () => {
  const result = await exercise('canonicalSearchContract', { category });
  expect(result.errors).toEqual([]); expect(result.requests).toHaveLength(1);
  expect(result.beforeAccept.some(path => path.includes('resource-addresses') || path.endsWith('main-thread'))).toBe(false);
  const positions = ['会话消息', '工具结果', 'LLM 请求记录', 'Trace'].includes(category);
  expect(result.opened).toHaveLength(positions ? 2 : 1); expect(new Set(result.ids).size).toBe(positions ? 2 : 1);
  expect(result.parses).toEqual(Array(positions ? 2 : 1).fill({ uri: result.expectedUri,
    local_gateway_id: 'gateway_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', resource_gateway_id: result.expectedOwner.gatewayId }));
  expect(result.opened.every(item => item.owner.gatewayId === result.expectedOwner.gatewayId && item.owner.workspaceId === result.expectedOwner.workspaceId)).toBe(true);
  expect(result.opened[0].group).toBe(-2); expect(result.opened[0].options.preserveFocus).toBe(false);
  if (category !== '文件') expect(result.opened.every(item => item.label === (category === 'Thread' || category === '会话消息' || category === '工具结果' ? 'needle Thread' : 'needle Session'))).toBe(true);
  if (positions) {
    const location = category === 'Trace' ? 'boxteamTrace' : category === 'LLM 请求记录' ? 'boxteamRequestLog' : 'boxteamMessage';
    expect(result.opened.map(item => item.options[location].reference.start_codepoint)).toEqual([2, 8]);
    expect(result.opened[1].group).toBe(-1); expect(result.opened[1].options.preserveFocus).toBe(true);
  }
  if (category !== '文件') expect(result.opened.every(item => item.owner.threadId === (category === '会话' || category === 'LLM 请求记录' ? 'main' : 'child'))).toBe(true);
  expect(result.paths.filter(path => path.endsWith('main-thread'))).toHaveLength(category === '会话' ? 1 : category === 'LLM 请求记录' ? 2 : 0);
  expect(result.folderHasResource).toBe(false);
});

for (const category of ['Trace', 'LLM 请求记录']) test(`${category}独立位置与规范Session不符时拒绝打开及空分屏`, async () => {
  const result = await exercise('canonicalSearchContract', { category, mismatch: true });
  expect(result.errors).toHaveLength(1); expect(result.errors[0]).toContain('日志位置与规范 Session／Thread 资源不匹配');
  expect(result.opened).toEqual([]); expect(result.addedGroups).toBe(0); expect(result.parses).toHaveLength(1);
  expect(result.paths.slice(result.paths.lastIndexOf('/api/gateway/resource-addresses/parse') + 1)).toEqual([]);
});

for (const category of ['会话', 'Thread']) test(`${category}命中缺少规范URI时明确失败且不从ID重造打开`, async () => {
  const result = await exercise('canonicalSearchContract', { category, omitUri: true });
  expect(result.errors).toEqual([]); expect(result.opened).toEqual([]);
  expect(result.labels.filter(label => label.includes('查询失败：'))).toHaveLength(2);
  expect(result.labels.some(label => label.includes('规范 URI'))).toBe(true);
  expect(result.paths.some(path => path.includes('resource-addresses') || path.endsWith('main-thread'))).toBe(false);
});

test('英文Trace候选侧开仍原样消费规范URI及独立位置', async () => {
  const result = await exercise('canonicalSearchContract', { category: 'Trace', locale: 'en' });
  expect(result.errors).toEqual([]); expect(result.buttonTooltips).toEqual(['Open to the side', 'Copy resource URI']);
  expect(result.labels).toContain('Trace'); expect(result.labels.some(label => label.endsWith(' · Search complete'))).toBe(true);
  expect(result.placeholder).toContain('Search content · Trace');
  expect(result.parses.map(parse => parse.uri)).toEqual([result.expectedUri, result.expectedUri]);
  expect(result.opened.map(item => item.options.boxteamTrace.reference.start_codepoint)).toEqual([2, 8]);
  expect(result.opened.map(item => item.group)).toEqual([-2, -1]);
});

test('英文Opener明确拒绝与规范资源不匹配的位置并保持零打开', async () => {
  const result = await exercise('canonicalSearchContract', { category: 'Trace', mismatch: true, locale: 'en' });
  expect(result.errors).toEqual(['Error: The log location does not match the canonical Session/Thread resource']);
  expect(result.opened).toEqual([]); expect(result.addedGroups).toBe(0); expect(result.parses).toHaveLength(1);
  expect(result.paths.slice(result.paths.lastIndexOf('/api/gateway/resource-addresses/parse') + 1)).toEqual([]);
});

for (const mode of ['typing', 'retry']) test(`逐键 ${mode} 合并查询、同浮层复用成功文件根且重新打开后重读`, async () => {
  const result = await exercise('typingSearchContract', mode);
  expect(result.errors).toEqual([]); expect(result.synchronousHistory).toBe(true); expect(result.earlyRequests).toBe(0);
  expect(result.firstCalls).toBe(mode === 'retry' ? 1 : 2); expect(result.firstRoots).toBe(1);
  expect(result.rootReads).toBe(mode === 'retry' ? 3 : 2);
  expect(result.metadata.filter(path => path === '/api/gateway/workspaces')).toHaveLength(mode === 'retry' ? 3 : 2);
  expect(result.candidates).toHaveLength(1); expect(result.candidates[0]).toMatchObject({ label: 'main.ts', description: '逐键工作区 · src' });
  expect(result.candidates[0].highlights.label.length).toBeGreaterThan(0);
  expect(result.candidates[0].highlights.description.length).toBeGreaterThan(0);
  expect(result.requests.every(request => request.queries.every(query => query.query === 'main' || query.query === 'main src'))).toBe(true);
  expect(result.requests.filter(request => request.queries[0].category === 'file').every(request => request.queries[0].query === 'main')).toBe(true);
  expect(result.publications).toBeLessThanOrEqual(4);
});

test('输入变更共享在途根读取，同时取消旧搜索并拒绝旧结果', async () => {
  const result = await exercise('typingSearchContract', 'in-flight');
  expect(result.errors).toEqual([]); expect(result.retained).toBe(true); expect(result.oldCancelled).toBe(true);
  expect(result.rootReads).toBe(1); expect(result.registryReads).toBe(1); expect(result.requests).toHaveLength(3);
  expect(result.requests.slice(1).every(request => request.queries.every(query => query.query === 'main'))).toBe(true);
});

for (const mode of ['close', 'identity', 'close-root']) test(`${mode} 取消待执行查询或实际根读取`, async () => {
  const result = await exercise('typingSearchContract', mode);
  expect(result.errors).toEqual([]); expect(result.busy).toBe(false);
  expect(result.requests).toBe(mode === 'close-root' ? 1 : 0);
  if (mode === 'close-root') expect(result.aborted).toBe(true);
  else expect(result.metadata).toEqual([]);
  if (mode === 'identity') expect(result.labels).toEqual(['Gateway 或访问凭据已改变，请重新打开快速访问']);
});

for (const mode of ['read-error-before-scope', 'read-error-after-scope']) test(`真实 Provider ${mode} 展示原网络原因与响应身份且零自动重试`, async () => {
  const result = await exercise('contentBranchContract', mode);
  expect(result.requests).toHaveLength(1);
  expect(result.pending).toEqual([false, false]);
  expect(result.busy).toBe(false);
  const unfinished = mode === 'read-error-after-scope' ? [1] : [0, 1];
  for (const index of unfinished) {
    expect(result.diagnostics[index]).toContain('request_id: request-0');
    expect(result.details[index] ?? '').not.toContain('request_id');
    expect(result.labels[index]).toContain('搜索连接读取失败: 目标网络连接被重置');
  }
  if (mode === 'read-error-after-scope') {
    expect(result.labels[0]).toContain('本次查询完成');
    expect(result.details[0]).toBe('本次读取 1 项，不代表全局快照');
  }
  expect(result.picks.map(items => items.length)).toEqual(mode === 'read-error-after-scope' ? [1, 0] : [0, 0]);
});

for (const entry of [
  { title: '多选确认返回全部目标且阻止原打开动作', action: 'accept', canPickMany: true, labels: ['alpha.txt', 'beta.txt'], cancelled: 0, executions: 0 },
  { title: '多选勾选后 Esc 返回空集合并取消读取', action: 'cancel', canPickMany: true, labels: [], cancelled: 1, executions: 0 },
  { title: '单选确认保持原返回及只选择行为', action: 'accept', canPickMany: false, labels: ['alpha.txt'], cancelled: 0, executions: 0 },
  { title: '单选 Esc 保持原取消读取行为', action: 'cancel', canPickMany: false, labels: [], cancelled: 1, executions: 0 },
  { title: 'show 忽略多选选项并保持原打开动作', action: 'show', canPickMany: true, labels: undefined, cancelled: 0, executions: 1 },
]) test(`原 Controller ${entry.title}`, async () => {
  const result = await exercise('quickAccessControllerContract', entry);
  expect(result.labels).toEqual(entry.labels);
  expect(result.canSelectMany).toBe(entry.canPickMany && entry.action !== 'show');
  expect(result.readerCancelled).toBe(entry.cancelled);
  expect(result.executions).toBe(entry.executions);
  expect(result.providerDisposed).toBe(1);
});

for (const mode of ['success', 'failure', 'cancel', 'unsupported']) test(`Composer 多选 ${mode} 经原命令只写捕获 writer，失败或取消零追加`, async () => {
  const result = await exercise('composerResourceSelectionContract', mode);
  expect(result.canPickMany).toBe(true);
  expect(result.events.slice(0, 2)).toEqual(['capture:0', 'pick']);
  expect(result.writes[1]).toEqual([]);
  if (mode === 'success' || mode === 'failure') {
    expect(result.requests).toHaveLength(2);
    expect(result.beforeSecondWrites).toEqual([[], []]);
    expect(result.requests.map(item => item.uri.split('/').at(-1))).toEqual(['alpha.txt', 'beta.txt']);
  } else expect(result.requests).toEqual([]);
  if (mode === 'success') {
    expect(result.writes[0]).toEqual([['alpha', 'beta'].map(name => `boxteam://gateway_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/workspace/019a0000-0000-7000-8000-000000000001/resources/files/root/${name}.txt`).join('\n')]);
    expect(result.events.filter(event => event.startsWith('append:'))).toEqual(['append:0']);
    expect(result.outcome).toEqual({});
  } else {
    expect(result.writes[0]).toEqual([]);
    expect(result.events.filter(event => event.startsWith('append:'))).toEqual([]);
    if (mode !== 'cancel') expect(result.outcome.failure).toBeTruthy();
  }
});

for (const action of ['reference', 'paste']) for (const outcome of ['success', 'cancel', 'gateway-returned']) {
  const mode = `recipient-${action}-${outcome}`;
  test(`Composer ${mode} 在接收者等待期间固定发起身份与原内容`, async () => {
    const result = await exercise('composerResourceSelectionContract', mode);
    expect(result.beforeSecondWrites).toEqual([[], []]); expect(result.writes[1]).toEqual([]);
    if (outcome === 'success') {
      expect(result.outcome).toEqual({}); expect(result.writes[0]).toHaveLength(1);
      expect(result.requests).toHaveLength(action === 'reference' ? 1 : 0);
      if (action === 'paste') expect(result.writes[0]).toEqual(['发起时选定的正文']);
      else expect(result.writes[0][0]).toContain('/alpha.txt');
    } else {
      expect(result.requests).toEqual([]); expect(result.writes).toEqual([[], []]);
      expect(result.events).toEqual(['recipient']);
      if (outcome === 'cancel') expect(result.outcome).toEqual({});
      else expect(result.outcome.failure).toBeTruthy();
    }
  });
}
test('Composer files-gateway-returned 在来源选择期间离开再返回也零解析和追加', async () => {
  const result = await exercise('composerResourceSelectionContract', 'files-gateway-returned');
  expect(result.canPickMany).toBe(true); expect(result.beforeSecondWrites).toEqual([[], []]);
  expect(result.requests).toEqual([]); expect(result.writes).toEqual([[], []]);
  expect(result.events).toEqual(['capture:0', 'pick']); expect(result.outcome.failure).toBeTruthy();
});

test('正文类别集合确认后同时查询消息和工具，未选类别及工作区零请求', async () => {
  const result = await exercise('contentSelectionContract', { mode: 'confirm' });
  expect(result.beforeConfirm).toBe(0); expect(result.canPickMany).toBe(true);
  expect(result.preset).toEqual(['会话消息']);
  expect(result.choices).toEqual(['会话消息', '工具结果', 'LLM 请求记录', 'Trace', '运行日志', 'Gateway 日志']);
  expect(result.requests).toHaveLength(1);
  expect(result.requests[0].scope.owners).toEqual([federatedOwners[0]]);
  expect(result.requests[0].queries.map(({ category, operation, query, match_mode }) => ({ category, operation, query, match_mode }))).toEqual(
    ['message', 'tool_result'].map(category => ({ category, operation: 'content', query: '原始😀查询', match_mode: 'literal' })));
  expect(result.shows[0]).toMatchObject({ value: 'content 原始😀查询', preserveValue: true, contentCategories: ['会话消息', '工具结果'],
    searchOwners: [{ gatewayId: federatedOwners[0].gateway_id, workspaceId: sharedWorkspaceId }], searchGroup: 7 });
  expect(result.placeholder).toContain('会话消息、工具结果');
  expect(result.labels).toContain('会话消息 · 没有匹配的资源'); expect(result.labels).toContain('工具结果 · 没有匹配的资源');
  expect(result.errors).toEqual([]);
});

test('英文正文类别选择使用译文展示并保留原wire类别和恢复参数', async () => {
  const result = await exercise('contentSelectionContract', { mode: 'confirm', locale: 'en' });
  expect(result.errors).toEqual([]); expect(result.beforeConfirm).toBe(0);
  expect(result.choiceTitle).toBe('Choose content categories');
  expect(result.choiceHint).toBe('Search only selected categories after confirmation; use % for file content');
  expect(result.choices).toEqual(['Session messages', 'Tool results', 'LLM request logs', 'Trace', 'Runtime logs', 'Gateway logs']);
  expect(result.preset).toEqual(['Session messages']);
  expect(result.requests).toHaveLength(1);
  expect(result.requests[0].queries.map(query => query.category)).toEqual(['message', 'tool_result']);
  expect(result.shows[0]).toMatchObject({ contentCategories: ['会话消息', '工具结果'], searchGroup: 7,
    searchOwners: [{ gatewayId: federatedOwners[0].gateway_id, workspaceId: sharedWorkspaceId }] });
  expect(result.placeholder).toContain('Search content · Session messages, Tool results');
  expect(result.labels).toContain('Session messages · No matching resources');
  expect(result.labels).toContain('Tool results · No matching resources');
});

test('正文类别集合取消恢复原查询、类别、范围和发起组', async () => {
  const result = await exercise('contentSelectionContract', { mode: 'cancel' });
  expect(result.beforeConfirm).toBe(1); expect(result.preset).toEqual(['会话消息', '工具结果']);
  expect(result.query).toBe('content 原始😀查询'); expect(result.requests).toHaveLength(2);
  expect(result.requests[1].queries).toEqual(result.requests[0].queries);
  expect(result.requests[1].scope).toEqual(result.requests[0].scope);
  expect(result.shows[0]).toMatchObject({ contentCategories: ['会话消息', '工具结果'], searchGroup: 7,
    searchOwners: [{ gatewayId: federatedOwners[0].gateway_id, workspaceId: sharedWorkspaceId }], searchRestore: { query: 'content 原始😀查询' } });
  expect(result.errors).toEqual([]);
});

test('正文类别集合同时选择两类日志只发一个 wire query，来源和工作区筛选独立且只重试失败来源', async () => {
  const result = await exercise('contentSelectionContract', { mode: 'logs' });
  const gateway_id = federatedOwners[0].gateway_id, other = '019a0000-0000-7000-8000-000000000002';
  const workspace = { gateway_id, scope: { source: 'workspace', workspace_ids: [sharedWorkspaceId, other] } };
  const gateway = { gateway_id, scope: { source: 'gateway', workspace_ids: [] } };
  expect(result.beforeConfirm).toBe(0); expect(result.preset).toEqual(['运行日志']);
  expect(result.requests).toHaveLength(2);
  const [first, retry] = result.requests;
  expect(first.scope.owners).toEqual([federatedOwners[0], { gateway_id, workspace_id: other }]);
  expect(first.queries).toHaveLength(1);
  expect(first.queries[0]).toMatchObject({ category: 'runtime_log', operation: 'content', match_mode: 'literal', query: '原始😀查询', diagnostic_targets: [workspace, gateway] });
  expect(retry.scope.mode).toBe('resume'); expect(retry.scope.scope.owners).toEqual(first.scope.owners);
  expect(retry.queries).toEqual([{ ...first.queries[0], diagnostic_targets: [gateway] }]);
  expect(retry.continuations).toEqual([{ branch: { diagnostics: gateway, category: 'runtime_log', operation: 'content' } }]);
  expect(result.retryLabel).toContain('Gateway 原日志读取失败'); expect(result.errors).toEqual([]);
});

test('远端运行日志搜索、续查和原行接受保持明确连接', async () => {
  const result = await exercise('contentSelectionContract', { mode: 'remote-logs' });
  const targets = result.requests[0].queries[0].diagnostic_targets;
  expect(targets.map(target => target.gateway_connection_id)).toEqual(['remote-log-connection', 'remote-log-connection']);
  expect(result.requests[1].continuations[0].branch.diagnostics.gateway_connection_id).toBe('remote-log-connection');
  expect(result.commands).toEqual([['boxteam.output.openLog', {
    gateway_id: targets[0].gateway_id, gateway_connection_id: 'remote-log-connection',
    workspace_id: undefined, log_id: 'gateway:file:plugin.log',
    location: { scope: { source: 'gateway', workspace_ids: [] }, reference: {
      gateway_id: targets[0].gateway_id, log_id: 'gateway:file:plugin.log', device: '1', inode: '2',
      line_start: 1, line_end: 2, content_version: 'a'.repeat(64), start_codepoint: 0, end_codepoint: 1,
    } },
  }]]);
  expect(result.errors).toEqual([]);
});

test('远端日志多连接无唯一管理路由时显示失败且不发查询', async () => {
  const result = await exercise('contentSelectionContract', { mode: 'remote-ambiguous' });
  expect(result.requests).toEqual([]);
  expect(result.labels.filter(label => label.includes('不能唯一确定'))).toHaveLength(2);
  expect(result.errors).toEqual([]);
});

for (const mode of ['user', 'gateway']) test(`正文类别集合在 ${mode} 身份失效时取消旧选择且零查询`, async () => {
  const result = await exercise('contentSelectionContract', { mode });
  expect(result.cancelled).toBe(true); expect(result.beforeConfirm).toBe(0);
  expect(result.requests).toEqual([]); expect(result.shows).toEqual([]);
  expect(result.errors).toHaveLength(1); expect(result.errors[0]).toContain('Gateway 或访问凭据已改变');
});

test('正文类别集合保留文件独立转 %，不发送正文类别请求', async () => {
  const result = await exercise('contentSelectionContract', { mode: 'file' });
  expect(result.requests).toEqual([]); expect(result.choices).toBe(0);
  expect(result.shows).toHaveLength(1);
  expect(result.shows[0]).toMatchObject({ value: '%原始😀查询', searchGroup: 7,
    searchOwners: [{ gatewayId: federatedOwners[0].gateway_id, workspaceId: sharedWorkspaceId }] });
  expect(result.shows[0].contentCategories).toBeUndefined();
});

test('原非文本大纲前插保持身份，空匹配仍可续页，续页后取消恢复而接受保留位置', async () => {
  for (const accept of [false, true]) {
    const result = await exercise('outlineContract', accept);
    expect(result.retained).toBe('second');
    expect(result.controlVisibleWithoutMatches).toEqual(['more']);
    expect(result.continuedWithoutHide).toBe(true);
    expect(result.labels).toEqual(['first', 'second']);
    expect(result.reveals).toEqual(accept ? ['more', 'first'] : ['more']);
    expect(result.restored).toBe(accept ? 0 : 1);
    expect(result.disposed).toBe(1);
    expect(result.previews).toEqual(['second', 'cancel:second']);
    expect(result.busy).toBe(false);
  }
});

test('正文同 UUID 跨 Gateway 只请求中继，快分支续页不被初始流收尾取消，失败分支独立重试', async () => {
  const result = await exercise('contentBranchContract', 'retry');
  expect(result.requests).toHaveLength(3);
  expect(result.paths.every(path => ['/api/gateway/auth/local-credential', '/api/gateway/search'].includes(path))).toBe(true);
  expect(result.requests[0].scope.owners).toEqual(federatedOwners);
  for (const request of result.requests.slice(1)) {
    expect(request.scope.mode).toBe('resume');
    expect(request.scope.scope.owners).toEqual(result.requests[0].scope.owners);
    expect(request.continuations).toHaveLength(1);
  }
  expect(result.requests[1].continuations[0].branch.owner).toEqual(federatedOwners[0]);
  expect(result.requests[1].continuations[0].cursor).toBe('A-original-cursor');
  expect(result.requests[2].continuations[0].branch.owner).toEqual(federatedOwners[1]);
  expect(result.requests[2].continuations[0].cursor).toBeUndefined();
  expect(result.pendingPreserved).toBe(true);
  expect(result.partialFailure).toContain('B 原始读取失败');
  expect(result.originalPreserved).toBe(true);
  expect(result.picks).toEqual([[`${ownerAKey}/2`, `${ownerAKey}/4`], [`${ownerBKey}/2`]]);
  expect(result.busy).toBe(false);
  expect(result.label).toBe('名称 A、名称 B');
  expect(result.status.every(detail => !detail.includes('request_id:'))).toBe(true);
});

for (const rejection of [
  { category: 'LLM 请求记录', code: 'invalid_request_log_cursor', owner: 'llm_request_log', reason: 'malformed', status: 400 },
  { category: 'LLM 请求记录', code: 'stale_request_log_cursor', owner: 'llm_request_log', reason: 'record_changed', status: 409 },
  { category: 'Trace', code: 'invalid_trace_search_cursor', owner: 'trace_event_store', reason: 'malformed', status: 400 },
  { category: 'Trace', code: 'stale_trace_search_cursor', owner: 'trace_event_store', reason: 'file_changed', status: 409 },
]) test(`${rejection.code} 重新查询保持原分支并清除被拒绝的 cursor`, async () => {
  const result = await exercise('cursorRejectionContract', rejection);
  const category = rejection.category === 'Trace' ? 'trace' : 'llm_request';
  expect(result.requests).toHaveLength(3);
  expect(result.requests[0].scope.owners).toEqual(federatedOwners);
  expect(result.requests[0].queries[0]).toMatchObject({ category, operation: 'content' });
  const continuation = result.requests[1], retry = result.requests[2];
  expect(continuation.continuations).toEqual([{ branch: { owner: federatedOwners[1], category, operation: 'content' }, cursor: 'B-rejected-cursor' }]);
  expect(retry.scope).toEqual(continuation.scope);
  expect(retry.queries).toEqual(continuation.queries);
  expect(retry.continuations).toEqual([{ branch: { owner: federatedOwners[1], category, operation: 'content' } }]);
  expect(result.failureLabel).toContain('原 owner 拒绝此续点');
  for (const field of [`code: ${rejection.code}`, `error_owner: ${rejection.owner}`, `reason: ${rejection.reason}`,
    `http_status: ${rejection.status}`, 'request_id: request-1']) expect(result.failureDiagnostic).toContain(field);
  expect(result.failureDetail).not.toContain('request_id:');
  expect(result.diagnosticText).toContain('[执行](command:workbench.action.closeWindow) <b>诊断</b>');
  expect(result.diagnosticLinks).toBe(0); expect(result.diagnosticMarkup).toBe(0);
  expect(result.retryTooltip).toBe('重新查询此类别');
  expect(result.pending).toEqual([false, false]); expect(result.busy).toBe(false);
  expect(result.diagnostics[1]).toContain('request_id: request-2');
});

test('文件使用原 QueryBuilder 选项，一次联邦 A+B，快分支续原 cursor 且独立重试', async () => {
  const result = await exercise('fileBranchContract', 'retry');
  expect(result.requests).toHaveLength(3);
  const first = result.requests[0], second = result.requests[1], retry = result.requests[2];
  expect(first.scope.owners).toEqual(federatedOwners);
  expect(first.queries[0].file_targets.map(item => item.owner)).toEqual(federatedOwners);
  expect(first.queries[0].file_targets.map(item => item.target.uri)).toEqual(federatedOwners
    .map(owner => `boxteam://${owner.gateway_id}/workspace/${sharedWorkspaceId}/resources/files/root`));
  expect(first.queries[0].file_targets.every(item => item.target.search_options.ignore_mode === 'none')).toBe(true);
  expect(second.continuations).toEqual([{ branch: { owner: first.scope.owners[0], category: 'file', operation: 'name',
    resource_uri: first.queries[0].file_targets[0].target.uri }, cursor: 'A-original-cursor' }]);
  expect(second.queries[0].limit).toBe(first.queries[0].limit);
  expect(retry.continuations[0].branch.owner).toEqual(federatedOwners[1]); expect(retry.continuations[0].cursor).toBeUndefined();
  expect(retry.queries[0].file_targets).toHaveLength(1);
  expect(result.pendingPreserved).toBe(true); expect(result.originalPreserved).toBe(true);
  expect(result.picks).toEqual([[`${ownerAKey}/resource/needle-2.txt`, `${ownerAKey}/resource/needle-4.txt`], [`${ownerBKey}/resource/needle-2.txt`]]);
  expect(result.paths.filter(path => path === '/api/v1/workspace/files/roots')).toHaveLength(2);
  expect(result.rootRequests).toEqual(federatedOwners);
  expect(result.busy).toBe(false);
});

test('文件多选 Provider 同查询续页按身份保留勾选，新项未勾选且身份失效清空选择', async () => {
  const result = await exercise('fileBranchContract', 'multi-selection');
  expect(result.initiallyPickable).toBe(true);
  expect(result.statusPickable).toEqual([false, false]);
  expect(result.currentStatusPickable).toEqual([false, false]);
  expect(result.resourceIds).toHaveLength(2); expect(result.resourcePickable).toEqual([true, true]);
  expect(result.selectedIds).toEqual([result.firstId]);
  expect(result.currentSelectedObject).toBe(true); expect(result.replacedFirstObject).toBe(true);
  expect(result.query).toBe('needle');
  expect(result.requests).toHaveLength(2);
  expect(result.requests.map(request => request.queries[0].query)).toEqual(['needle', 'needle']);
  expect(result.requests[1].continuations[0].cursor).toBe('A-original-cursor');
  expect(result.invalidatedSelectedIds).toEqual([]); expect(result.invalidatedActiveIds).toEqual([]);
});

for (const mode of ['cancel', 'late']) test(`文件 ${mode} 取消同一联邦流且不发布迟到页`, async () => {
  const result = await exercise('fileBranchContract', mode);
  expect(result.cancelled).toBe(true); expect(result.unchanged).toBe(true); expect(result.calls).toBe(1);
});

test('正文 picker 取消释放同一 Gateway reader，之后没有迟到展示更新', async () => {
  const result = await exercise('contentBranchContract', 'cancel');
  expect(result.cancelled).toBe(true);
  expect(result.unchanged).toBe(true);
  expect(result.calls).toBe(1);
});

test('正文流断开保留已完成 A，B 明确失败并显示原 request_id', async () => {
  const result = await exercise('contentBranchContract', 'disconnect');
  expect(result.labels[0]).toContain('本次查询完成');
  expect(result.labels[1]).toContain('未收到完整终态');
  expect(result.diagnostics[1]).toContain('request_id: request-0');
  expect(result.picks).toEqual([[`${ownerAKey}/2`], []]);
});

for (const diagnostic of [
  { state: 'cancelled', code: 'cancelled', message: '原领域读取已停止', reason: 'original_cancel_reason', retryable: true },
  { state: 'cancelled', code: 'cancelled', message: '原领域读取已停止', reason: 'original_cancel_reason', retryable: false },
  { state: 'failed', code: 'deadline', message: '原领域查询期限已到', error_owner: 'gateway_search', reason: 'deadline', retryable: true },
  { state: 'failed', code: 'upstream_error', message: '取消只是原始错误正文，不代表业务状态', error_owner: 'canonical_history', reason: 'read_failed', http_status: 500, retryable: false },
  { state: 'failed', code: 'read_failed', message: '目标文件读取失败', error_owner: 'workspace_files', reason: 'permission_denied', http_status: null, retryable: false },
]) test(`原分支 ${diagnostic.state}/${diagnostic.code}/retryable=${diagnostic.retryable} 保留状态和独立重试语义`, async () => {
  const result = await exercise('branchStateContract', diagnostic), cancelled = diagnostic.state === 'cancelled';
  expect(result.statuses[0].label).toContain(cancelled ? '查询已取消：' : '查询失败：');
  expect(result.statuses[0].label).toContain(diagnostic.message);
  expect(result.statuses[0].diagnostic).toContain('request_id: request-1');
  expect(result.statuses[0].detail).not.toContain('request_id:');
  for (const field of ['code', 'error_owner', 'reason', 'http_status']) {
    if (diagnostic[field] == null) expect(result.statuses[0].diagnostic).not.toContain(`${field}:`);
    else expect(result.statuses[0].diagnostic).toContain(`${field}: ${diagnostic[field]}`);
  }
  expect(result.statuses[1].label).toContain('本次查询完成');
  expect(result.statuses[1].diagnostic).toContain('request_id: request-0');
  if (cancelled) {
    expect(result.statuses[0].diagnostic).toContain(`code: ${diagnostic.code}`);
    expect(result.statuses[0].diagnostic).toContain(`reason: ${diagnostic.reason}`);
    expect(result.statuses[0].label).not.toContain('查询失败：');
  } else {
    expect(result.statuses[0].label).not.toContain('查询已取消：');
    if (diagnostic.code === 'deadline') expect(result.statuses[0].label).toContain('查询失败：查询超时：');
  }
  expect(result.retainedPicks).toEqual([cancelled ? [`${ownerAKey}/2`] : [], [`${ownerBKey}/2`]]);
  expect(result.originalRetained).toEqual([cancelled, true]);
  expect(result.statuses[0].buttons).toEqual(diagnostic.retryable ? ['重新查询此类别', '复制安全诊断'] : ['复制安全诊断']);
  expect(result.statuses[0].hasTrigger).toBe(true);
  expect(result.callsBeforeRetry).toBe(2);
  expect(result.requests).toHaveLength(diagnostic.retryable ? 3 : 2);
  const continuation = result.requests[1];
  expect(continuation.scope.mode).toBe('resume');
  expect(continuation.scope.scope.owners).toEqual(federatedOwners);
  expect(continuation.continuations).toEqual([{ branch: { owner: federatedOwners[0], category: 'message', operation: 'content' }, cursor: 'A-original-cursor' }]);
  if (diagnostic.retryable) {
    const retry = result.requests[2];
    expect(retry.scope).toEqual(continuation.scope);
    expect(retry.queries).toEqual(continuation.queries);
    expect(retry.continuations).toEqual([{ branch: continuation.continuations[0].branch }]);
    expect(result.picks).toEqual([[`${ownerAKey}/4`], [`${ownerBKey}/2`]]);
  }
  expect(result.otherRetainedAfterRetry).toBe(true);
  expect(result.pending).toEqual([false, false]); expect(result.busy).toBe(false);
});

test('受限分支保留原原因与续点，正常完成不伪造诊断或 HTTP 状态', async () => {
  const diagnostic = { state: 'limited', code: 'scan_limit', message: '本次仅检查一部分记录，原目录仍有未读取内容',
    error_owner: 'canonical_history', reason: 'item_budget', retryable: false };
  const result = await exercise('branchStateContract', diagnostic);
  expect(result.statuses[0].label).toContain('本次查询覆盖受限（scan_limit）');
  for (const field of [diagnostic.message, 'code: scan_limit', 'error_owner: canonical_history', 'reason: item_budget', 'request_id: request-1']) {
    expect(result.statuses[0].diagnostic).toContain(field);
  }
  expect(result.statuses[0].diagnostic).not.toContain('http_status:');
  expect(result.statuses[0].buttons).toEqual(['继续查询此类别', '复制安全诊断']);
  expect(result.statuses[0].hasTrigger).toBe(true);
  expect(result.retainedPicks).toEqual([[`${ownerAKey}/2`], [`${ownerBKey}/2`]]);
  expect(result.originalRetained).toEqual([true, true]);
  expect(result.statuses[1].label).toContain('本次查询完成');
  expect(result.statuses[1].detail).toBe('本次读取 1 项，不代表全局快照');
  expect(result.requests).toHaveLength(2);
  expect(result.pending).toEqual([false, false]); expect(result.busy).toBe(false);
});

test('首个范围事件前断流保留原响应请求身份，明确重查原查询且不自动重试', async () => {
  const result = await exercise('contentBranchContract', 'disconnect-before-scope');
  expect(result.labels.every(label => label.includes('未收到完整终态'))).toBe(true);
  expect(result.details).toEqual([undefined, undefined]);
  expect(result.diagnostics.every(text => text.includes('request_id: request-0'))).toBe(true);
  expect(result.requests).toHaveLength(2);
  const [first, retry] = result.requests;
  expect(first.scope).toEqual({ mode: 'selected', owners: federatedOwners });
  expect(retry.scope).toEqual(first.scope);
  expect(retry.queries).toEqual(first.queries);
  expect(retry.continuations).toEqual([]);
  expect(result.paths.filter(path => path === '/api/gateway/search')).toHaveLength(2);
  expect(result.pending).toEqual([false, false]); expect(result.busy).toBe(false);
  expect(result.picks).toEqual([[`${ownerAKey}/2`], [`${ownerBKey}/2`]]);
});

test('URI 原样交原 Opener 并冻结发起组，校验前不创建侧边组，后台接受保持原选项', async () => {
  const calls = await exercise('uriContract');
  expect(calls.map(call => call.uri)).toEqual([
    'boxteam://workspace/workspace-a/resources/files/root/name%40target.txt',
    'boxteam://workspace/workspace-a/resources/files/root/name%40target.txt#L2',
    'https://example.test/file',
    'https://example.test:8042',
    'https://example.test/file:2:3',
    'https://example.test/#7',
    'https://example.test/report,12',
  ]);
  expect(calls.every(call => call.group === 1 && call.side && call.pinned && call.preserveFocus)).toBe(true);
});

test('文件符号退出恢复原活动文件且不重查，无匹配不丢来源，模型引用随原生命周期释放', async () => {
  const result = await exercise('fileSymbolContract');
  expect(result.patterns).toEqual(['', 'symbol']);
  expect(result.restoredIds).toEqual(result.ids);
  expect(result.active).toBe(result.target);
  expect(result.symbols.map(item => item.filter)).toEqual(['beta', 'missing', 'beta']);
  expect(result.symbols.every(item => item.resource === result.target)).toBe(true);
  expect(result.previews).toHaveLength(3);
  expect(result.previews.every(item => item.resource === result.target && item.group === -1
    && item.options.sourceGroup === 1 && item.options.transient && item.options.preserveFocus)).toBe(true);
  expect(result.opened).toHaveLength(1);
  expect(result.opened[0].resource).toBe(result.target);
  expect(result.opened[0].options.selection.startLineNumber).toBe(3);
  expect(result.opened[0].options.selection.startColumn).toBe(3);
  expect(result.releases.filter(item => item === result.target)).toHaveLength(3);
});

for (const kind of ['credential', 'gateway', 'gateway-returned']) test(`最近历史 ${kind} 失效后旧候选不可移除、接受或创建分屏`, async () => {
  const result = await exercise('historyIdentityContract', kind);
  expect(result.labels).toEqual(['Gateway 或访问凭据已改变，请重新打开快速访问']);
  expect(result.selected).toBe(0); expect(result.historyCount).toBe(1);
  expect(result.opened).toEqual([]); expect(result.addedGroups).toBe(0);
  expect(result.errors).toHaveLength(3);
  expect(result.errors.every(error => error.includes('Gateway 或访问凭据已改变'))).toBe(true);
});

for (const kind of ['credential', 'gateway']) test(`最近历史范围选择 ${kind} 失效后取消Picker且不重开旧列表`, async () => {
  const result = await exercise('historyScopeIdentityContract', kind);
  expect(result.reopened).toEqual([]); expect(result.cancelled).toBe(true);
  expect(result.errors).toHaveLength(1); expect(result.errors[0]).toContain('Gateway 或访问凭据已改变');
});

test('quickNavigate 只按所选 owner 的历史顺序导航，不混入补充项或帮助且不发查询', async () => {
  const result = await exercise('quickNavigateContract');
  expect(result.empty.map(item => item.id)).toEqual(result.resources);
  expect(result.filtered.map(item => item.id).sort()).toEqual([...result.resources].sort());
  expect(result.empty.map(item => item.label)).toEqual(['needle recent', 'needle Thread']);
  expect(result.calls).toEqual([]);
  expect(result.buttons).toBe(0);
  expect(result.opened).toHaveLength(1);
  expect(result.opened[0]).toMatchObject({ resource: result.resources[0], group: -1,
    options: { pinned: false, preserveFocus: false } });
});

test('普通 delegate 保留行列定位且修改位置不重查，filesOnly 沿上游保留字面查询', async () => {
  const positioned = await exercise('fileRangeContract', false);
  expect(positioned.patterns).toEqual(['', 'target.txt']);
  expect(positioned.samePick).toBe(true);
  expect(positioned.opened).toHaveLength(1);
  expect(positioned.opened[0]).toMatchObject({ group: -1,
    selection: { startLineNumber: 9, startColumn: 2, endLineNumber: 9, endColumn: 2 } });
  expect(positioned.opened[0].resource).toEndWith('/target.txt');
  const literal = await exercise('fileRangeContract', true);
  expect(literal.patterns).toEqual(['', 'target.txt:7:3', 'target.txt:9:2']);
  expect(literal.samePick).toBe(false);
  expect(literal.opened).toHaveLength(1);
  expect(literal.opened[0].selection).toBeNull();
});

test('delegate 接收空查询及 URI 查询，换查询和释放取消旧调用且忽略迟到结果', async () => {
  const result = await exercise('delegateContract');
  expect(result.initial).toEqual(['']);
  expect(result.patterns).toEqual(['', 'https://example.test/file']);
  expect(result.cancelledPrevious).toBe(true);
  expect(result.picks).toEqual([{ label: 'delegate.txt', description: '调用方目录', buttons: [] }]);
  expect(result.afterLate).toEqual(['delegate.txt']);
  expect(result.cancelledOnDispose).toBe(true);
});

test('身份屏障或 Gateway 改变立即取消、撤下旧结果，继续输入也不沿旧身份查询', async () => {
  const results = await exercise('identityContract');
  expect(results.map(item => item.kind)).toEqual(['user', 'gateway', 'gateway-null-returned']);
  for (const item of results) {
    expect(item.cancelled).toBe(true);
    expect(item.calls).toBe(1);
    expect(item.busy).toBe(false);
    expect(item.selected).toBe(0);
    expect(item.labels).toEqual(['Gateway 或访问凭据已改变，请重新打开快速访问']);
  }
});

for (const category of ['会话', 'Thread']) {
  test(`${category} 名称复用同一联邦请求、fuzzy 高亮与独立续页重试，不产生路由预检`, async () => {
    const result = await exercise('nameBranchContract', { mode: 'retry', category });
    expect(result.requests).toHaveLength(3);
    expect(result.paths.every(path => ['/api/gateway/auth/local-credential', '/api/gateway/search'].includes(path))).toBe(true);
    expect(result.requests[0].scope.owners).toEqual(federatedOwners);
    expect(result.requests[0].queries).toEqual([{ category: category === '会话' ? 'catalog' : 'thread', operation: 'name',
      contract_version: 1, query: 'needle', match_mode: 'fuzzy', limit: 50, file_targets: [], diagnostic_targets: [] }]);
    for (const request of result.requests.slice(1)) {
      expect(request.scope.mode).toBe('resume');
      expect(request.scope.scope.owners).toEqual(result.requests[0].scope.owners);
      expect(request.continuations).toHaveLength(1);
    }
    expect(result.requests[1].continuations[0].cursor).toBe('A-original-cursor');
    expect(result.requests[1].continuations[0].branch.owner).toEqual(federatedOwners[0]);
    expect(result.requests[2].continuations[0].branch.owner).toEqual(federatedOwners[1]);
    expect(result.requests[2].continuations[0].cursor).toBeUndefined();
    expect(result.pendingPreserved).toBe(true);
    expect(result.partialFailure).toContain('B 原始读取失败');
    // 同名跨 owner 保留，folder 不被误作 Session，child 不改投 main；同分排序沿原 scorer。
    expect(result.picks).toEqual(category === '会话' ? [
      [`${ownerAKey}/folder/needle Folder 2`, `${ownerAKey}/folder/needle Folder 4`, `${ownerAKey}/session/needle Session 2`, `${ownerAKey}/session/needle Session 4`],
      [`${ownerBKey}/folder/needle Folder 2`, `${ownerBKey}/session/needle Session 2`],
    ] : [[`${ownerAKey}/resource/needle Child 2`, `${ownerAKey}/resource/needle Child 4`], [`${ownerBKey}/resource/needle Child 2`]]);
    expect(result.highlights.flat().every(highlights => highlights.label.length > 0)).toBe(true);
    expect(result.busy).toBe(false);
    expect(result.status.every(detail => !detail.includes('request_id:'))).toBe(true);
  });
  test(`${category} 名称取消释放 Gateway reader，迟到展示被撤销`, async () => {
    const result = await exercise('nameBranchContract', { mode: 'cancel', category });
    expect(result.cancelled).toBe(true);
    expect(result.unchanged).toBe(true);
    expect(result.calls).toBe(1);
  });
  test(`${category} 不合作传输在取消后才交付正文，原结果仍不发布`, async () => {
    const result = await exercise('nameBranchContract', { mode: 'late', category });
    expect(result.cancelled).toBe(true);
    expect(result.unchanged).toBe(true);
    expect(result.calls).toBe(1);
  });
}

test('补充项仍经 filter，handleAccept 保留原接收者与后台标志，历史不混入 Thread', async () => {
  const result = await exercise('selectContract');
  expect(result.labels).toEqual(['附件来源', 'history.txt']);
  expect(result.backgroundKeptOpen).toBe(true);
  expect(result.foregroundClosed).toBe(true);
  expect(result.received).toEqual([{ label: '附件来源', kind: 'contextPick', background: true },
    { label: '附件来源', kind: 'contextPick', background: false }]);
});

test('原帮助的确认和精确前缀转接都保留归属与接收者，返回默认时只关闭本次递归帮助', async () => {
  const result = await exercise('helpContract');
  expect(result.values).toEqual(['', '_contract ']);
  expect(result.preserve).toBe(true);
  expect(result.sameOwner).toBe(true);
  expect(result.sameReceiver).toBe(true);
  expect(result.includeHelp).toEqual([false, true]);
  expect(result.sourceHelpUnchanged).toBe(true);
});

test('部分窗口范围零符号查询，身份错误明确显示，原生调用保留原符号服务', async () => {
  const result = await exercise('symbolContract');
  expect(result.scopedLabel).toContain('内置符号 Provider 使用完整窗口工作区');
  expect(result.errorLabel).toContain('原 Gateway 身份失效');
  expect(result.multiLabel).toContain('内置符号 Provider 使用完整窗口工作区');
  expect(result.multiPlaceholder).toBe('工作区符号 · workspace-a、workspace-c');
  expect(result.scopedCalls).toEqual([]);
  expect(result.nativeCalls).toEqual(['needle']);
}, 10000);

for (const mode of ['partial-sync', 'partial-async']) test(`明确完整窗口的 # 保留成功符号并呈现原 Provider 部分失败（${mode}）`, async () => {
  const result = await exercise('symbolProviderFailuresContract', mode);
  expect(result.busy).toBe(false);
  expect(result.calls).toHaveLength(2);
  expect(result.calls.every(call => call.query === 'needle')).toBe(true);
  expect(result.items).toHaveLength(2);
  expect(result.items.some(item => item.label === 'NeedleSymbol')).toBe(true);
  const failure = result.items.find(item => item.label.includes('工作区符号不可用'));
  expect(failure?.label).toContain(mode === 'partial-sync' ? 'provider-sync-owner-A' : 'provider-async-owner-A');
  expect(failure?.description).toContain('["gateway-a","workspace-a"]、["gateway-a","workspace-b"]');
  expect(failure?.description).toContain('Provider 2');
});

for (const mode of ['full-sync', 'full-async']) test(`明确完整窗口的 # 全部 Provider 失败仍显示逐 owner 原因且列表非空（${mode}）`, async () => {
  const result = await exercise('symbolProviderFailuresContract', mode);
  expect(result.busy).toBe(false);
  expect(result.calls).toHaveLength(2);
  expect(result.items).toHaveLength(2);
  expect(result.items.every(item => item.label.includes('工作区符号不可用'))).toBe(true);
  const reasonPrefix = mode === 'full-sync' ? 'provider-sync-owner-' : 'provider-async-owner-';
  expect(result.items.map(item => item.label).sort()).toEqual([0, 1].map(index => `工作区符号不可用：${reasonPrefix}${index}`));
  expect(result.items.every(item => item.description.includes('["gateway-a","workspace-a"]、["gateway-a","workspace-b"]'))).toBe(true);
});

test('完整窗口 # Provider 在取消后迟到 reject 不发布候选', async () => {
  const result = await exercise('symbolProviderFailuresContract', 'cancel-late');
  expect(result.calls).toEqual([{ provider: 'late', query: 'needle' }]);
  expect(result.beforeDispose).toEqual(['正在查询工作区符号…']);
  expect(result.afterLate).toEqual(result.afterDispose);
  expect(result.afterLate.some(label => label.includes('late-provider-rejection'))).toBe(false);
});


for (const [mode, target] of [['open', 9], ['side', 10], ['background-side', 8], ['unscoped-open', 7], ['unscoped-side', 10]]) test(`符号 ${mode} 异步解析后仍由原生规则处理来源、锁定和焦点`, async () => {
  const result = await exercise('symbolGroupContract', mode);
  expect(result).toEqual([{ target, sourceGroup: 7 }]);
});

test('文件符号侧开等待原预览恢复，再沿原生侧开意图进入目标', async () => {
  const result = await exercise('fileSymbolContract', 'side');
  expect(result.opened).toHaveLength(1);
  expect(result.opened[0]).toMatchObject({ group: -2, options: { sourceGroup: 1, selection: { startLineNumber: 3 } } });
  expect(result.sequence.slice(-2)).toEqual(['restore-end', 'open']);
  expect(result.restoredStates).toEqual([{ position: 'before-preview' }]);
});


test('符号行内侧开等异步解析完成才关闭 Picker，Esc 仍取消迟到打开', async () => {
  const accepted = await exercise('symbolGroupContract', 'button');
  expect(accepted.opened).toEqual([{ target: 8, sourceGroup: 7 }]);
  expect(accepted.sequence).toEqual(['open', 'hide']);
  const cancelled = await exercise('symbolGroupContract', 'button-cancel');
  expect(cancelled.opened).toEqual([]);
  expect(cancelled.sequence).toEqual(['hide']);
});


test('Quick Access 侧开图标和文案遵循原上下分屏设置', async () => {
  expect(await exercise('sideDirectionContract', 'right')).toMatchObject({ iconClass: 'codicon codicon-split-horizontal', tooltip: '在侧边打开' });
  expect(await exercise('sideDirectionContract', 'down')).toMatchObject({ iconClass: 'codicon codicon-split-vertical', tooltip: '在下方打开' });
});
