import { afterAll, beforeAll, expect, test } from 'bun:test';
import { join, resolve } from 'node:path';
import { chromium } from 'playwright';
import { build } from '../../../../../src/clients/workbench/node_modules/esbuild/lib/main.js';
import { buildSourcePlugin, pinnedUpstreamCommit, sourceTransform } from '../../../../../src/clients/workbench/scripts/sourceTransform.mjs';
import { TestRunContext } from '../../../../harness/js/run-context.mjs';

const run = await TestRunContext.fromTestFile('tests/integration/clients/web/quick_access/default_contract.test.mjs').prepare();
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

test('正文跨目标只请求 Gateway，快分支续页不被初始流收尾取消，失败分支独立重试', async () => {
  const result = await exercise('contentBranchContract', 'retry');
  expect(result.requests).toHaveLength(3);
  expect(result.paths.every(path => ['/api/gateway/auth/local-credential', '/api/gateway/search'].includes(path))).toBe(true);
  expect(result.requests[0].scope.owners.map(owner => owner.workspace_id)).toEqual(['A', 'B']);
  for (const request of result.requests.slice(1)) {
    expect(request.scope.mode).toBe('resume');
    expect(request.scope.scope.owners).toEqual(result.requests[0].scope.owners);
    expect(request.continuations).toHaveLength(1);
  }
  expect(result.requests[1].continuations[0].branch.owner.workspace_id).toBe('A');
  expect(result.requests[1].continuations[0].cursor).toBe('A-original-cursor');
  expect(result.requests[2].continuations[0].branch.owner.workspace_id).toBe('B');
  expect(result.requests[2].continuations[0].cursor).toBeUndefined();
  expect(result.pendingPreserved).toBe(true);
  expect(result.partialFailure).toContain('B 原始读取失败');
  expect(result.originalPreserved).toBe(true);
  expect(result.picks).toEqual([['A/2', 'A/4'], ['B/2']]);
  expect(result.busy).toBe(false);
  expect(result.label).toBe('名称 A、名称 B');
  expect(result.status.every(detail => detail.includes('request_id: request-'))).toBe(true);
});

test('文件使用原 QueryBuilder 选项，一次联邦 A+B，快分支续原 cursor 且独立重试', async () => {
  const result = await exercise('fileBranchContract', 'retry');
  expect(result.requests).toHaveLength(3);
  const first = result.requests[0], second = result.requests[1], retry = result.requests[2];
  expect(first.scope.owners.map(owner => owner.workspace_id)).toEqual(['A', 'B']);
  expect(first.queries[0].file_targets.map(item => item.target.uri)).toEqual(['A', 'B'].map(id => `boxteam://workspace/${id}/resources/files/root`));
  expect(first.queries[0].file_targets.every(item => item.target.search_options.ignore_mode === 'none')).toBe(true);
  expect(second.continuations).toEqual([{ branch: { owner: first.scope.owners[0], category: 'file', operation: 'name',
    resource_uri: first.queries[0].file_targets[0].target.uri }, cursor: 'A-original-cursor' }]);
  expect(second.queries[0].limit).toBe(first.queries[0].limit);
  expect(retry.continuations[0].branch.owner.workspace_id).toBe('B'); expect(retry.continuations[0].cursor).toBeUndefined();
  expect(retry.queries[0].file_targets).toHaveLength(1);
  expect(result.pendingPreserved).toBe(true); expect(result.originalPreserved).toBe(true);
  expect(result.picks).toEqual([['A/resource/needle-2.txt', 'A/resource/needle-4.txt'], ['B/resource/needle-2.txt']]);
  expect(result.paths.filter(path => path === '/api/v1/workspace/files/roots')).toHaveLength(2);
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
  expect(result.picks).toEqual([['A/2'], []]);
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
    expect(result.requests[0].scope.owners.map(owner => owner.workspace_id)).toEqual(['A', 'B']);
    expect(result.requests[0].queries).toEqual([{ category: category === '会话' ? 'catalog' : 'thread', operation: 'name',
      contract_version: 1, query: 'needle', match_mode: 'fuzzy', limit: 50, file_targets: [] }]);
    for (const request of result.requests.slice(1)) {
      expect(request.scope.mode).toBe('resume');
      expect(request.scope.scope.owners).toEqual(result.requests[0].scope.owners);
      expect(request.continuations).toHaveLength(1);
    }
    expect(result.requests[1].continuations[0].cursor).toBe('A-original-cursor');
    expect(result.requests[1].continuations[0].branch.owner.workspace_id).toBe('A');
    expect(result.requests[2].continuations[0].branch.owner.workspace_id).toBe('B');
    expect(result.requests[2].continuations[0].cursor).toBeUndefined();
    expect(result.pendingPreserved).toBe(true);
    expect(result.partialFailure).toContain('B 原始读取失败');
    // 同名跨 owner 保留，folder 不被误作 Session，child 不改投 main；同分排序沿原 scorer。
    expect(result.picks).toEqual(category === '会话' ? [
      ['A/folder/needle Folder 2', 'A/folder/needle Folder 4', 'A/session/needle Session 2', 'A/session/needle Session 4'],
      ['B/folder/needle Folder 2', 'B/session/needle Session 2'],
    ] : [['A/resource/needle Child 2', 'A/resource/needle Child 4'], ['B/resource/needle Child 2']]);
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
