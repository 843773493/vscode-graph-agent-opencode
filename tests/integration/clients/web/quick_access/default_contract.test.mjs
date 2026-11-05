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
  const context = await browser.newContext();
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

test('正文类别集合确认后同时查询消息和工具，未选类别及工作区零请求', async () => {
  const result = await exercise('contentSelectionContract', 'confirm');
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

test('正文类别集合取消恢复原查询、类别、范围和发起组', async () => {
  const result = await exercise('contentSelectionContract', 'cancel');
  expect(result.beforeConfirm).toBe(1); expect(result.preset).toEqual(['会话消息', '工具结果']);
  expect(result.query).toBe('content 原始😀查询'); expect(result.requests).toHaveLength(2);
  expect(result.requests[1].queries).toEqual(result.requests[0].queries);
  expect(result.requests[1].scope).toEqual(result.requests[0].scope);
  expect(result.shows[0]).toMatchObject({ contentCategories: ['会话消息', '工具结果'], searchGroup: 7,
    searchOwners: [{ gatewayId: federatedOwners[0].gateway_id, workspaceId: sharedWorkspaceId }], searchRestore: { query: 'content 原始😀查询' } });
  expect(result.errors).toEqual([]);
});

test('正文类别集合同时选择两类日志只发一个 wire query，来源和工作区筛选独立且只重试失败来源', async () => {
  const result = await exercise('contentSelectionContract', 'logs');
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

for (const mode of ['user', 'gateway']) test(`正文类别集合在 ${mode} 身份失效时取消旧选择且零查询`, async () => {
  const result = await exercise('contentSelectionContract', mode);
  expect(result.cancelled).toBe(true); expect(result.beforeConfirm).toBe(0);
  expect(result.requests).toEqual([]); expect(result.shows).toEqual([]);
  expect(result.errors).toHaveLength(1); expect(result.errors[0]).toContain('Gateway 或用户身份已改变');
});

test('正文类别集合保留文件独立转 %，不发送正文类别请求', async () => {
  const result = await exercise('contentSelectionContract', 'file');
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
  expect(result.status.every(detail => detail.includes('request_id: request-'))).toBe(true);
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
    `http_status: ${rejection.status}`, 'request_id: request-1']) expect(result.failureDetail).toContain(field);
  expect(result.diagnosticText).toContain('[执行](command:workbench.action.closeWindow) <b>诊断</b>');
  expect(result.diagnosticLinks).toBe(0); expect(result.diagnosticMarkup).toBe(0);
  expect(result.retryTooltip).toBe('重新查询此类别');
  expect(result.pending).toEqual([false, false]); expect(result.busy).toBe(false);
  expect(result.status[1]).toContain('request_id: request-2');
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
  expect(result.details[1]).toBe('request_id: request-0');
  expect(result.picks).toEqual([[`${ownerAKey}/2`], []]);
});

for (const diagnostic of [
  { state: 'cancelled', code: 'cancelled', message: '原领域读取已停止', reason: 'original_cancel_reason', retryable: true },
  { state: 'cancelled', code: 'cancelled', message: '原领域读取已停止', reason: 'original_cancel_reason', retryable: false },
  { state: 'failed', code: 'deadline', message: '原领域查询期限已到', error_owner: 'gateway_search', reason: 'deadline', retryable: true },
  { state: 'failed', code: 'upstream_error', message: '取消只是原始错误正文，不代表业务状态', error_owner: 'canonical_history', reason: 'read_failed', http_status: 500, retryable: false },
]) test(`原分支 ${diagnostic.state}/${diagnostic.code}/retryable=${diagnostic.retryable} 保留状态和独立重试语义`, async () => {
  const result = await exercise('branchStateContract', diagnostic), cancelled = diagnostic.state === 'cancelled';
  expect(result.statuses[0].label).toContain(cancelled ? '查询已取消：' : '查询失败：');
  expect(result.statuses[0].label).toContain(diagnostic.message);
  expect(result.statuses[0].detail).toContain('request_id: request-1');
  for (const field of ['code', 'error_owner', 'reason', 'http_status']) {
    if (diagnostic[field] === undefined) expect(result.statuses[0].detail).not.toContain(`${field}:`);
    else expect(result.statuses[0].detail).toContain(`${field}: ${diagnostic[field]}`);
  }
  expect(result.statuses[1].label).toContain('本次查询完成');
  expect(result.statuses[1].detail).toContain('request_id: request-0');
  if (cancelled) {
    expect(result.statuses[0].detail).toContain(`code: ${diagnostic.code}`);
    expect(result.statuses[0].detail).toContain(`reason: ${diagnostic.reason}`);
    expect(result.statuses[0].label).not.toContain('查询失败：');
  } else {
    expect(result.statuses[0].label).not.toContain('查询已取消：');
    if (diagnostic.code === 'deadline') expect(result.statuses[0].label).toContain('查询失败：查询超时：');
  }
  expect(result.retainedPicks).toEqual([cancelled ? [`${ownerAKey}/2`] : [], [`${ownerBKey}/2`]]);
  expect(result.originalRetained).toEqual([cancelled, true]);
  expect(result.statuses[0].buttons).toEqual(diagnostic.retryable ? ['重新查询此类别'] : []);
  expect(result.statuses[0].hasTrigger).toBe(diagnostic.retryable);
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
    expect(result.statuses[0].detail).toContain(field);
  }
  expect(result.statuses[0].detail).not.toContain('http_status:');
  expect(result.statuses[0].buttons).toEqual(['继续查询此类别']);
  expect(result.statuses[0].hasTrigger).toBe(true);
  expect(result.retainedPicks).toEqual([[`${ownerAKey}/2`], [`${ownerBKey}/2`]]);
  expect(result.originalRetained).toEqual([true, true]);
  expect(result.statuses[1].label).toContain('本次查询完成');
  expect(result.statuses[1].detail).toBe('本次读取 1 项，不代表全局快照 · request_id: request-0');
  expect(result.requests).toHaveLength(2);
  expect(result.pending).toEqual([false, false]); expect(result.busy).toBe(false);
});

test('首个范围事件前断流保留原响应请求身份，明确重查原查询且不自动重试', async () => {
  const result = await exercise('contentBranchContract', 'disconnect-before-scope');
  expect(result.labels.every(label => label.includes('未收到完整终态'))).toBe(true);
  expect(result.details).toEqual(['request_id: request-0', 'request_id: request-0']);
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
  expect(result.previews.every(item => item.resource === result.target && item.group === 1
    && item.options.transient && item.options.preserveFocus)).toBe(true);
  expect(result.opened).toHaveLength(1);
  expect(result.opened[0].resource).toBe(result.target);
  expect(result.opened[0].options.selection.startLineNumber).toBe(3);
  expect(result.opened[0].options.selection.startColumn).toBe(3);
  expect(result.releases.filter(item => item === result.target)).toHaveLength(3);
});

test('quickNavigate 只按所选 owner 的历史顺序导航，不混入补充项或帮助且不发查询', async () => {
  const result = await exercise('quickNavigateContract');
  expect(result.empty.map(item => item.id)).toEqual(result.resources);
  expect(result.filtered.map(item => item.id).sort()).toEqual([...result.resources].sort());
  expect(result.empty.map(item => item.label)).toEqual(['needle recent', 'needle Thread']);
  expect(result.calls).toEqual([]);
  expect(result.buttons).toBe(0);
  expect(result.opened).toHaveLength(1);
  expect(result.opened[0]).toMatchObject({ resource: result.resources[0], group: 1,
    options: { pinned: false, preserveFocus: false } });
});

test('普通 delegate 保留行列定位且修改位置不重查，filesOnly 沿上游保留字面查询', async () => {
  const positioned = await exercise('fileRangeContract', false);
  expect(positioned.patterns).toEqual(['', 'target.txt']);
  expect(positioned.samePick).toBe(true);
  expect(positioned.opened).toHaveLength(1);
  expect(positioned.opened[0]).toMatchObject({ group: 1,
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
  expect(results.map(item => item.kind)).toEqual(['user', 'gateway']);
  for (const item of results) {
    expect(item.cancelled).toBe(true);
    expect(item.calls).toBe(1);
    expect(item.busy).toBe(false);
    expect(item.selected).toBe(0);
    expect(item.labels).toEqual(['Gateway 或用户身份已改变，请重新打开快速访问']);
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
    expect(result.status.every(detail => detail.includes('request_id: request-'))).toBe(true);
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

test('业务符号在调用全局 Provider 前明确拒绝，身份错误不改投，原生调用保留原符号服务', async () => {
  const result = await exercise('symbolContract');
  expect(result.scopedLabel).toContain('工作区符号未启用');
  expect(result.errorLabel).toContain('原 Gateway 身份失效');
  expect(result.multiLabel).toContain('工作区符号未启用');
  expect(result.multiPlaceholder).toBe('工作区符号 · workspace-a、workspace-b');
  expect(result.scopedCalls).toEqual([]);
  expect(result.nativeCalls).toEqual(['needle']);
}, 10000);
