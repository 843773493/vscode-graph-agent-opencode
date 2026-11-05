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
const workbench = resolve('src/clients/workbench'), bundle = join(run.artifactsDir, 'default-contract.mjs');
await build({ entryPoints: [resolve('tests/integration/clients/web/quick_access/default_contract.fixture.mjs')],
  outfile: bundle, bundle: true, format: 'esm', platform: 'browser', target: 'es2024',
  tsconfig: join(workbench, 'tsconfig.jsonc'),
  plugins: [buildSourcePlugin(workbench, sourceTransform(workbench, { commit: pinnedUpstreamCommit }, []))], logLevel: 'silent' });
// 本地静态装具避免每次经浏览器控制通道传输数 MB 的 base64 源码。
const server = Bun.serve({ hostname: '127.0.0.1', port: 0, fetch(request) {
  const path = new URL(request.url).pathname;
  if (path === '/') return new Response('<!doctype html><title>Quick Access 合同装具</title>', { headers: { 'content-type': 'text/html' } });
  if (path === '/default-contract.mjs') return new Response(Bun.file(bundle), { headers: { 'content-type': 'text/javascript' } });
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
    const result = await page.evaluate(async ({ name, argument }) => (await import('/default-contract.mjs'))[name](argument), { name, argument });
    expect(errors).toEqual([]);
    return result;
  } finally { await context.close(); }
}

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
