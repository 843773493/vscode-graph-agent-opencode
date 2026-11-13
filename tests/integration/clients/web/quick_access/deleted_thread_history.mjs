// Integration：真实执行生成镜像，原目录命令删除后从最近历史重开；只读浏览器存储核对结果。
import { writeFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import { chromium } from 'playwright';
import { expect } from 'playwright/test';
import { TestRunContext } from '../../../../harness/js/run-context.mjs';

const required = name => { const value = process.env[name]; if (!value) throw new Error(`已删历史验收缺少 ${name}`); return value; };
export async function runDeletedThreadHistory({ run, launch }) {
const output = required('BOXTEAM_BROWSER_RESULT_PATH'), identities = JSON.parse(required('BOXTEAM_CACHE_THREADS'));
const result = { classification: 'Integration', phase: 'initialize', pageErrors: [], reads: [], writes: [], receipts: [], executions: [],
  boundary: '真实 Gateway／Backend／正式 Workbench；model_stream 经消息入口生成历史，原 Session Command／确认／outbox 删除；不预写浏览器状态或替换响应。' };
const pending = [], responseErrors = [], records = new WeakMap();
const phase = async value => { result.phase = value; await writeFile(output, JSON.stringify(result, null, 2) + '\n'); };
let page, quick, failure;
try {
  const host = await launch({ run, result, usePage(next) {
  page = next;
  quick = page.locator('.quick-input-widget');
  page.setDefaultTimeout(15_000);
  page.on('pageerror', cause => result.pageErrors.push(String(cause)));
  page.on('request', request => {
    const path = new URL(request.url()).pathname, headers = request.headers();
    const record = { phase: result.phase, path, method: request.method(),
      gatewayId: headers['x-boxteam-target-gateway-id'], workspaceId: headers['x-boxteam-workspace-id'] };
    if (/^\/api\/v1\/sessions\/[^/]+(?:\/|$)/.test(path)) result.reads.push(record);
    if (path === '/api/v1/session-catalog/operations:enqueue') {
      Object.assign(record, { body: request.postDataJSON() }); result.writes.push(record);
    }
    if (request.method() === 'POST' && /\/threads\/[^/]+\/messages$/.test(path)) result.executions.push(record);
    if (/^\/api\/v1\/sessions\/[^/]+$/.test(path) || path === '/api/v1/session-catalog/operations:enqueue') records.set(request, record);
  });
  page.on('requestfinished', request => {
    const record = records.get(request);
    if (!record) return;
    pending.push((async () => {
      const response = await request.response(), body = await response.json();
      Object.assign(record, { status: response.status(), requestId: body.request_id, header: response.headers()['x-request-id'],
        data: body.data, detail: body.detail });
      expect(record.requestId).toBe(record.header);
    })().catch(cause => responseErrors.push(String(cause))));
  });
  } });
  await page.setViewportSize({ width: 1700, height: 1000 });
  const command = (id, ...args) => page.evaluate(({ id, args }) => globalThis.workbench.commands.executeCommand(id, ...args), { id, args });
  const api = async (path, owner, body) => {
    const receipt = await page.evaluate(async ({ path, owner, body }) => {
      const token = (await (await fetch('/api/gateway/auth/local-credential')).json()).data.token;
      const response = await fetch(path, { method: body === undefined ? 'GET' : 'POST',
        headers: { 'Content-Type': 'application/json', 'X-Local-Token': token, ...(owner ? {
          'X-BoxTeam-Target-Gateway-Id': owner.gateway_id, 'X-BoxTeam-Workspace-Id': owner.workspace_id,
        } : {}) }, body: body === undefined ? undefined : JSON.stringify(body) });
      return { status: response.status, header: response.headers.get('X-Request-ID'), body: await response.json() };
    }, { path, owner, body });
    expect(receipt.header).toBeTruthy(); expect(receipt.body.request_id).toBe(receipt.header);
    result.receipts.push({ phase: result.phase, path, ...receipt });
    return receipt;
  };
  const data = async (path, owner, body) => {
    const receipt = await api(path, owner, body); expect(receipt.status).toBe(200); return receipt.body.data;
  };
  const views = () => page.evaluate(async () => {
    const read = request => new Promise((resolve, reject) => {
      request.onsuccess = () => resolve(request.result); request.onerror = () => reject(request.error);
    });
    const db = await read(indexedDB.open('boxteam-browser-views'));
    try {
      const store = db.transaction('views').objectStore('views');
      const [keys, values] = await Promise.all([read(store.getAllKeys()), read(store.getAll())]);
      return Object.fromEntries(keys.map((key, index) => [key, values[index]]));
    } finally { db.close(); }
  });
  const resource = target => `boxteam-thread://${target.gateway_id}/${target.workspace_id}/${target.session_id}/${target.thread_id}`;
  const uri = target => `boxteam://${target.gateway_id}/workspace/${target.workspace_id}/resources/session/${target.session_id}/threads/${target.thread_id}`;
  const cacheKey = target => JSON.stringify([target.gateway_id, 'thread-history', target.workspace_id, target.session_id, target.thread_id]);
  const pane = target => page.locator(`section.boxteam-thread[data-workspace-id="${target.workspace_id}"]`
    + `[data-session-id="${target.session_id}"][data-thread-id="${target.thread_id}"]`);
  const ready = () => expect(page.locator('.native-session-navigation')).toBeVisible({ timeout: 30_000 });
  await ready();
  const [a, b] = identities;
  expect((await data('/api/gateway/health')).gateway_id).toBe(a.gateway_id);
  expect((await data('/api/gateway/workspaces?check_health=false')).items)
    .toContainEqual(expect.objectContaining({ gateway_id: a.gateway_id, workspace_id: a.workspace_id }));
  expect((await data('/api/v1/workspace', a)).workspace_id).toBe(a.workspace_id);
  const pick = target => quick.locator(`[data-quick-input-id="${resource(target)}"]`);
  const open = async target => {
    await command('workbench.action.quickOpen', uri(target));
    await quick.locator(`[data-quick-input-id="link:${uri(target)}"]`).click();
    await expect(pane(target)).toContainText(target.answer);
    await expect(pane(target).getByTestId('composer-input')).toBeEditable();
  };
  await phase('create-real-history-and-drafts');
  for (const [index, target] of identities.entries()) {
    await open(target); await pane(target).getByTestId('composer-input').fill(`已删历史保留草稿 ${index}`);
  }
  // 只读核正常调度落盘；原生关窗前不由测试提前 flush 展示态。
  await expect.poll(async () => {
    const saved = await views();
    return identities.every(target => saved[cacheKey(target)]?.timeline.orderedTurnIds.length > 0);
  }).toBe(true);
  const before = await views();
  for (const target of identities) expect(before[cacheKey(target)].timeline.orderedTurnIds.length).toBeGreaterThan(0);
  result.cachedBeforeDelete = identities.map(target => ({ resource: resource(target), key: cacheKey(target),
    turnIds: before[cacheKey(target)].timeline.orderedTurnIds }));
  await command('workbench.action.closeAllEditors');
  await command('workbench.action.quickOpen', '');
  await expect(pick(a)).toHaveCount(1); await expect(pick(b)).toHaveCount(1); await page.keyboard.press('Escape');
  const original = await data(`/api/v1/sessions/${a.session_id}`, a);

  await phase('delete-closed-session');
  const deletion = command('boxteam.session.delete', {
    owner: { gatewayId: a.gateway_id, workspaceId: a.workspace_id }, sessionId: a.session_id,
  }).then(() => ({ done: true }), cause => ({ error: String(cause) }));
  const confirmation = page.getByRole('dialog', { name: '永久删除会话', exact: true });
  await expect(confirmation).toContainText(original.title);
  await confirmation.getByRole('button', { name: '删除', exact: true }).click();
  expect(await deletion).toEqual({ done: true });
  await expect.poll(async () => (await api(`/api/v1/sessions/${a.session_id}`, a)).status).toBe(404);
  expect(result.writes).toHaveLength(1);
  expect(result.writes[0]).toMatchObject({ gatewayId: a.gateway_id, workspaceId: a.workspace_id });
  expect(result.writes[0].body.intents).toHaveLength(1);
  const intent = result.writes[0].body.intents[0];
  expect(intent).toMatchObject({ kind: 'delete_session', target_node_id: a.session_id });
  let operation;
  await expect.poll(async () => {
    operation = (await data(`/api/v1/session-catalog/operations?operation_id=${intent.client_operation_id}`, a)).items[0];
    return operation?.state === 'committed' && operation.settlement === 'settled';
  }).toBe(true);
  result.deleteReceipt = operation;
  const replacement = await data('/api/v1/sessions', a, { title: original.title });
  const replacementThread = await data(`/api/v1/sessions/${replacement.session_id}/main-thread`, a);
  expect(replacement.session_id).not.toBe(a.session_id);
  result.replacement = { sessionId: replacement.session_id, threadId: replacementThread.thread_id, title: replacement.title };
  // 关闭目标的展示镜像确实仍在，不能靠此前被清理而让重开反例空转。
  expect((await views())[cacheKey(a)]).toBeTruthy();
  await host.reload(); await ready();
  const restored = await views();
  for (const target of identities) {
    expect(restored[cacheKey(target)].timeline.orderedTurnIds).toEqual(before[cacheKey(target)].timeline.orderedTurnIds);
  }
  result.cachedAfterRestart = identities.map(target => ({ key: cacheKey(target),
    turnIds: restored[cacheKey(target)].timeline.orderedTurnIds }));

  await phase('reopen-deleted-history');
  await command('workbench.action.quickOpen', '');
  await quick.getByRole('button', { name: '选择搜索范围', exact: true }).click();
  for (const checkbox of await quick.getByRole('checkbox').all()) await checkbox.uncheck();
  await quick.locator('.monaco-list-row').filter({ hasText: a.gateway_id }).filter({ hasText: a.workspace_id }).getByRole('checkbox').check();
  await quick.getByRole('button', { name: '确定', exact: true }).click();
  await expect(pick(a)).toHaveCount(1); await expect(pick(b)).toHaveCount(1);
  await pick(a).click();
  await expect(pane(a).getByRole('alert')).toContainText('会话初始化失败：请求失败 404');
  await expect(pane(a).getByRole('alert')).toContainText(`Session ${a.session_id} not found`);
  await expect(pane(a)).not.toContainText(a.answer);
  await expect(pane(a).getByTestId('composer-input')).toHaveCount(0);
  await expect.poll(async () => (await views())[cacheKey(a)] === undefined).toBe(true);
  await expect(pane({ ...a, session_id: replacement.session_id, thread_id: replacementThread.thread_id })).toHaveCount(0);
  await expect.poll(() => result.reads.some(item => item.phase === result.phase && item.path === `/api/v1/sessions/${a.session_id}` && item.status === 404)).toBe(true);
  const reopenReads = result.reads.filter(item => item.phase === result.phase);
  for (const read of reopenReads) expect(read).toMatchObject({ gatewayId: a.gateway_id, workspaceId: a.workspace_id });
  expect(reopenReads.some(item => item.path.includes(replacement.session_id))).toBe(false);
  expect(reopenReads.some(item => /\/(bootstrap|history)(?:\/|$)/.test(item.path))).toBe(false);
  result.oldTargetRejected = true; result.oldCacheRemoved = true; result.sameNameNotOpened = true;

  await phase('remove-only-deleted-history');
  await command('workbench.action.quickOpen', '');
  await expect(pick(a)).toHaveCount(1);
  await pick(a).getByRole('button', { name: '从最近打开中移除', exact: true }).click();
  await expect(pick(a)).toHaveCount(0); await expect(pick(b)).toHaveCount(1);
  await page.keyboard.press('Escape');
  await expect(pane(a).getByRole('alert')).toBeVisible();
  expect((await data(`/api/v1/sessions/${replacement.session_id}`, a)).title).toBe(original.title);
  await command('workbench.action.closeAllEditors');
  await command('workbench.action.quickOpen', ''); await pick(b).click();
  await expect(pane(b)).toContainText(b.answer);
  await expect(pane(b).getByTestId('composer-input')).toHaveValue('已删历史保留草稿 1');
  expect((await views())[cacheKey(b)].timeline.orderedTurnIds).toEqual(before[cacheKey(b)].timeline.orderedTurnIds);
  result.historyRemovalOnly = true; result.survivorPreserved = true;
  await host.reload(); await ready();
  await expect(pane(b).getByTestId('composer-input')).toHaveValue('已删历史保留草稿 1');
  await command('workbench.action.quickOpen', '');
  await expect(pick(a)).toHaveCount(0); await expect(pick(b)).toHaveCount(1); await page.keyboard.press('Escape');
  result.removalRestored = true;
  await Promise.all(pending); expect(responseErrors).toEqual([]);
  expect(result.writes).toHaveLength(1); expect(result.executions).toEqual([]); expect(result.pageErrors).toEqual([]);
  await phase('complete');
} catch (cause) { failure = cause; result.error = String(cause.stack ?? cause); }
finally {
  if (failure && page) await page.screenshot({ path: output.replace(/\.json$/, '.failure.png'), fullPage: true })
    .catch(cause => { result.screenshotError = String(cause); });
  try { await run.close(); }
  catch (cause) {
    result.cleanupError = String(cause);
    failure = failure ? new AggregateError([failure, cause], '已删历史验收及关闭均失败') : cause;
  }
  await writeFile(output, JSON.stringify(result, null, 2) + '\n');
}
if (failure) throw failure;
return result;
}

async function launchChromium({ run, usePage }) {
  const browser = await chromium.launch({ executablePath: required('PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH'), headless: true,
    args: ['--no-sandbox', '--disable-dev-shm-usage'] });
  run.addCleanup('已删历史 Chromium', () => browser.close());
  const page = await browser.newPage({ viewport: { width: 1700, height: 1000 } });
  usePage(page);
  await page.goto(required('BOXTEAM_BROWSER_BASE_URL'), { waitUntil: 'domcontentloaded' });
  return { async reload() {
    await page.evaluate(() => globalThis.boxteamWorkbenchLifecycle.prepareToClose());
    await page.reload({ waitUntil: 'domcontentloaded' });
  } };
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  await runDeletedThreadHistory({
    run: TestRunContext.fromTestFile('tests/integration/clients/web/quick_access/test_recent_history.py'), launch: launchChromium,
  });
}
