// Integration：真实 Gateway／Backend、正式 Workbench、目录 Provider 和 fork writer。
import { writeFile } from 'node:fs/promises';
import { chromium } from 'playwright';
import { expect } from 'playwright/test';
import { TestRunContext } from '../../../../../harness/js/run-context.mjs';
import { testControl } from '../../../../../support/e2e_control/browser_client.mjs';

const required = name => {
  if (!process.env[name]) throw new Error(`Session 分支验收缺少 ${name}`);
  return process.env[name];
};
const base = required('BOXTEAM_BROWSER_BASE_URL');
const output = required('BOXTEAM_BROWSER_RESULT_PATH');
const screenshot = required('BOXTEAM_BROWSER_SCREENSHOT_PATH');
const run = TestRunContext.fromTestFile('tests/integration/clients/web/quick_access/resources/test_session_fork_actions.py');
const result = { classification: 'Integration', phase: 'initialize', pageErrors: [], requests: [], sourcePointReceipts: [],
  forkWrites: [], messageWrites: [], threadCandidateCancel: false, sessionCandidateCancel: false, navigationCancel: false,
  fullRolloutCopy: false, childThreadF1UsesExactSource: false,
  boundary: '真实 Gateway／Backend／Workbench 和目录 writer；搜索候选、Session 与 child Thread 通过产品入口发现；仅由 model_stream 回放一次创建来源历史，不执行其它模型调用' };
const pendingReceipts = [], receiptErrors = [], requestRecords = new WeakMap();
let page, browser, failure;

try {
  browser = await chromium.launch({ executablePath: required('PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH'), headless: true,
    args: ['--no-sandbox', '--disable-dev-shm-usage'] });
  run.addCleanup('Session 分支 Chromium', () => browser.close());
  page = await browser.newPage({ viewport: { width: 1700, height: 1080 } });
  page.setDefaultTimeout(20_000);
  page.on('pageerror', cause => result.pageErrors.push(String(cause)));
  page.on('request', request => {
    const url = new URL(request.url()), path = url.pathname, method = request.method();
    if (!path.startsWith('/api/')) return;
    const record = { phase: result.phase, path, query: url.search, method,
      body: method === 'GET' ? undefined : request.postDataJSON() };
    result.requests.push(record);
    if (/\/fork-source-point$/.test(path) || /\/fork-context$/.test(path) && method === 'POST') {
      requestRecords.set(request, record);
    }
    if (/\/fork-context$/.test(path) && method === 'POST') result.forkWrites.push(record);
    if (/\/threads\/[^/]+\/messages$/.test(path) && method === 'POST') result.messageWrites.push(record);
  });
  page.on('requestfinished', request => {
    const record = requestRecords.get(request);
    if (!record) return;
    pendingReceipts.push((async () => {
      const response = await request.response(), body = await response.json();
      record.receipt = { status: response.status(), requestId: body.request_id,
        header: response.headers()['x-request-id'], data: body.data };
      if (/\/fork-source-point$/.test(record.path)) result.sourcePointReceipts.push(record);
    })().catch(cause => receiptErrors.push(String(cause))));
  });

  await page.goto(base, { waitUntil: 'domcontentloaded' });
  await expect(page.locator('.native-session-navigation')).toBeVisible({ timeout: 30_000 });
  const api = (path, owner, body) => page.evaluate(async ({ path, owner, body }) => {
    const credential = await (await fetch('/api/gateway/auth/local-credential')).json();
    const response = await fetch(path, { method: body === undefined ? 'GET' : 'POST',
      body: body === undefined ? undefined : JSON.stringify(body),
      headers: { 'X-Local-Token': credential.data.token, 'Content-Type': 'application/json', ...(owner ? {
        'X-BoxTeam-Target-Gateway-Id': owner.gateway_id, 'X-BoxTeam-Workspace-Id': owner.workspace_id,
      } : {}) } });
    const payload = await response.json(), header = response.headers.get('X-Request-ID');
    if (!response.ok || !payload.request_id || payload.request_id !== header) {
      throw new Error(JSON.stringify({ path, status: response.status, payload, header }));
    }
    return payload.data;
  }, { path, owner, body });
  const command = (id, ...args) => page.evaluate(({ id, args }) =>
    globalThis.workbench.commands.executeCommand(id, ...args), { id, args });
  const health = await api('/api/gateway/health');
  const registered = await api('/api/gateway/workspaces?check_health=false');
  const owner = registered.active_workspace;
  expect(owner.gateway_id).toBe(health.gateway_id);
  expect((await api('/api/v1/workspace', owner)).workspace_id).toBe(owner.workspace_id);
  result.owner = owner;
  const interactions = JSON.parse(required('BOXTEAM_THREAD_FORK_INTERACTIONS'));

  result.phase = 'create-sessions';
  const alpha = await api('/api/v1/sessions', owner, { title: 'Quick Session Alpha' });
  const beta = await api('/api/v1/sessions', owner, { title: 'Quick Session Beta' });
  const alphaMain = await api(`/api/v1/sessions/${alpha.session_id}/main-thread`, owner);
  const betaMain = await api(`/api/v1/sessions/${beta.session_id}/main-thread`, owner);
  result.alphaSessionId = alpha.session_id; result.betaSessionId = beta.session_id;
  result.alphaMainThreadId = alphaMain.thread_id; result.mainThreadId = betaMain.thread_id;
  const publicThreadUri = (sessionId, threadId) =>
    `boxteam://${owner.gateway_id}/workspace/${owner.workspace_id}/resources/session/${sessionId}/threads/${threadId}`;
  result.phase = 'create-history';
  await testControl({ action: 'plan_model', session_id: beta.session_id,
    thread_id: betaMain.thread_id, interactions });
  const accepted = await api(`/api/v1/sessions/${beta.session_id}/threads/${betaMain.thread_id}/messages`, owner,
    { message: { content: '为 Session fork 验收创建真实来源消息。' }, run: {} });
  await expect.poll(async () => {
    const job = await api(`/api/v1/jobs/${accepted.job_id}`, owner);
    if (['failed', 'cancelled', 'timed_out', 'execution_lost'].includes(job.status)) throw new Error(JSON.stringify(job));
    return job.status;
  }, { timeout: 45_000 }).toBe('completed');
  result.modelJobId = accepted.job_id;
  await command('_workbench.open', publicThreadUri(alpha.session_id, alphaMain.thread_id));
  await expect(page.locator(`section.boxteam-thread[data-session-id="${alpha.session_id}"][data-thread-id="${alphaMain.thread_id}"] textarea[data-testid="composer-input"]`))
    .toBeEnabled({ timeout: 30_000 });

  const quick = page.locator('.quick-input-widget'), input = quick.locator('input').first();
  const dialog = page.getByRole('dialog', { name: '创建上下文分支', exact: true });
  const treeRow = page.getByTestId(`catalog-node-${JSON.stringify([owner.gateway_id, owner.workspace_id])}-${beta.session_id}`);
  const forkReads = () => result.sourcePointReceipts.filter(item => item.phase === result.phase);
  const forkWrites = () => result.forkWrites.filter(item => item.phase === result.phase);
  const sourceUriIn = items => items.at(-1)?.receipt?.data?.source_uri;
  const sessionPick = async () => {
    await command('workbench.action.quickOpen', 'session Quick Session Beta');
    await expect(input).toHaveValue('session Quick Session Beta');
    const row = quick.locator('.monaco-list-row').filter({ hasText: 'Quick Session Beta' }).filter({ hasText: beta.session_id });
    await expect(row).toBeVisible();
    await row.hover();
    return row;
  };
  const cancelDialog = async () => {
    await expect(dialog).toBeVisible();
    await dialog.getByRole('button', { name: '取消', exact: true }).click();
    await expect(dialog).toBeHidden();
  };

  result.phase = 'thread-candidate-cancel';
  await command('workbench.action.quickOpen', 'thread Quick Session Beta');
  await expect(input).toHaveValue('thread Quick Session Beta');
  const unopenedMain = quick.locator('.monaco-list-row').filter({ hasText: betaMain.thread_id });
  await expect(unopenedMain).toBeVisible();
  await unopenedMain.hover();
  await expect(unopenedMain.getByRole('button', { name: '从最近打开中移除', exact: true })).toHaveCount(0);
  await unopenedMain.getByRole('button', { name: '创建上下文分支', exact: true }).click();
  await expect(dialog).toBeVisible();
  await expect.poll(() => forkReads().length).toBe(1);
  expect(forkReads()[0].query).toContain(`thread_id=${betaMain.thread_id}`);
  expect(sourceUriIn(forkReads())).toContain(`/threads/${betaMain.thread_id}`);
  await cancelDialog();
  expect(forkWrites()).toEqual([]); result.threadCandidateCancel = true;

  result.phase = 'session-candidate-cancel';
  let row = await sessionPick();
  await row.getByRole('button', { name: '创建上下文分支', exact: true }).click();
  await expect(dialog).toBeVisible();
  await expect.poll(() => forkReads().length).toBe(1);
  expect(forkReads()[0].path).toContain(beta.session_id);
  expect(new URLSearchParams(forkReads()[0].query).has('thread_id')).toBe(false);
  expect(sourceUriIn(forkReads())).toContain(`/resources/session/${beta.session_id}/threads/${betaMain.thread_id}`);
  await cancelDialog();
  expect(forkWrites()).toEqual([]); result.sessionCandidateCancel = true;

  result.phase = 'navigation-cancel';
  await treeRow.click({ button: 'right' });
  await page.getByRole('menuitem', { name: '从上下文创建分支', exact: true }).click();
  await expect(dialog).toBeVisible();
  await expect.poll(() => forkReads().length).toBe(1);
  expect(forkReads()[0].path).toContain(beta.session_id);
  expect(new URLSearchParams(forkReads()[0].query).has('thread_id')).toBe(false);
  await cancelDialog();
  expect(forkWrites()).toEqual([]); result.navigationCancel = true;

  result.phase = 'full-rollout-copy';
  row = await sessionPick();
  await row.getByRole('button', { name: '创建上下文分支', exact: true }).click();
  await expect(dialog).toBeVisible();
  await expect(dialog.getByLabel('复制范围', { exact: true })).toBeEnabled();
  const sourcePointCount = result.sourcePointReceipts.length;
  await dialog.getByLabel('复制范围', { exact: true }).selectOption('full_rollout_copy');
  await expect.poll(() => result.sourcePointReceipts.filter(item => item.phase === result.phase
    && new URLSearchParams(item.query).get('mode') === 'full_rollout_copy').length).toBe(1);
  await expect(dialog.getByRole('button', { name: '创建分支', exact: true })).toBeEnabled();
  await dialog.getByRole('button', { name: '创建分支', exact: true }).click();
  await expect(dialog).toBeHidden({ timeout: 60_000 });
  await expect.poll(() => forkWrites().length).toBe(1, { timeout: 60_000 });
  if (!result.forkWrites[0].receipt) {
    await expect.poll(() => result.requests.some(item => item === result.forkWrites[0] && item.receipt)).toBe(true);
  }
  const child = result.forkWrites[0].receipt?.data;
  expect(result.forkWrites[0].body).toMatchObject({ mode: 'full_rollout_copy', anchor_item_id: null,
    creation_kind: 'new_child_thread', source_view_id: expect.any(String), source_view_revision: expect.any(Number) });
  expect(result.forkWrites[0].body.source_uri).toContain(`/resources/session/${beta.session_id}/threads/${betaMain.thread_id}`);
  expect(result.forkWrites[0].receipt).toMatchObject({ status: 200,
    requestId: result.forkWrites[0].receipt?.header });
  expect(child.session.session_id).toBe(beta.session_id);
  expect(child.thread_id).toBeTruthy();
  result.childThreadId = child.thread_id;
  await expect(page.locator(`section.boxteam-thread[data-session-id="${beta.session_id}"][data-thread-id="${child.thread_id}"] textarea[data-testid="composer-input"]`))
    .toBeEnabled({ timeout: 30_000 });
  expect(sourcePointCount).toBeGreaterThan(0);
  result.fullRolloutCopy = true;

  result.phase = 'child-thread-f1';
  await command('_workbench.open', publicThreadUri(beta.session_id, child.thread_id));
  await expect(page.locator(`section.boxteam-thread[data-session-id="${beta.session_id}"][data-thread-id="${child.thread_id}"] textarea[data-testid="composer-input"]`))
    .toBeEnabled({ timeout: 30_000 });
  await command('workbench.action.showCommands');
  await input.fill('>创建上下文分支');
  const f1Action = quick.getByRole('option').filter({ hasText: '创建上下文分支…' });
  await expect(f1Action).toHaveCount(1);
  await f1Action.click();
  await expect(dialog).toBeVisible();
  await expect.poll(() => forkReads().length).toBe(1);
  expect(new URLSearchParams(forkReads()[0].query).get('thread_id')).toBe(child.thread_id);
  expect(sourceUriIn(forkReads())).toContain(`/threads/${child.thread_id}`);
  await cancelDialog();
  expect(forkWrites()).toEqual([]);
  result.childThreadF1UsesExactSource = true;

  await Promise.all(pendingReceipts);
  expect(receiptErrors).toEqual([]);
  expect(result.sourcePointReceipts.every(item => item.receipt?.status === 200
    && item.receipt.requestId === item.receipt.header)).toBe(true);
  expect(result.forkWrites).toHaveLength(1);
  expect(result.messageWrites).toHaveLength(1);
  expect(result.messageWrites[0].phase).toBe('create-history');
  expect(result.pageErrors).toEqual([]);
  result.phase = 'complete';
} catch (cause) {
  failure = cause;
  result.error = String(cause.stack ?? cause);
} finally {
  if (failure && page) await page.screenshot({ path: screenshot, fullPage: true }).catch(cause => {
    result.screenshotError = String(cause);
  });
  try { await writeFile(output, JSON.stringify(result, null, 2) + '\n'); }
  catch (cause) { failure = failure ? new AggregateError([failure, cause], '页面验收与结果保存均失败') : cause; }
  try { await run.close(); }
  catch (cause) { failure = failure ? new AggregateError([failure, cause], '页面验收与资源清理均失败') : cause; }
}
if (failure) throw failure;
console.log(JSON.stringify({ phase: result.phase, output }));
