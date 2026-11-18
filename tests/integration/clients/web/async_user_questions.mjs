import { writeFile } from 'node:fs/promises';
import { join } from 'node:path';
import { chromium } from 'playwright';
import { expect } from 'playwright/test';
import { planChatCalls, testControl } from '../../../support/e2e_control/browser_client.mjs';

const artifacts = process.env.BOXTEAM_BROWSER_ARTIFACT_ROOT;
if (!artifacts) throw new Error('必须设置浏览器产物目录');
const result = { phase: 'bootstrap', pageErrors: [], receipts: [], reads: [], boundary: '真实 Workbench/Gateway/Backend；仅上游模型使用精确回放与响应 barrier' };
const browser = await chromium.launch({ executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH,
  headless: true, args: ['--no-sandbox', '--disable-dev-shm-usage'] });
const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
page.on('pageerror', error => result.pageErrors.push(String(error)));
page.on('request', request => { if (request.method() === 'GET' && /\/questions$/.test(new URL(request.url()).pathname)) result.reads.push(request.url()); });
let gate;
try {
  await page.goto(process.env.BOXTEAM_BROWSER_BASE_URL, { waitUntil: 'domcontentloaded' });
  await expect(page.locator('.native-session-navigation')).toBeVisible({ timeout: 30_000 });
  const api = async (path, init = {}) => {
    const response = await page.evaluate(async ({ path, init }) => {
      const token = (await (await fetch('/api/gateway/auth/local-credential')).json()).data.token;
      const response = await fetch(path, { ...init, headers: { 'Content-Type': 'application/json', 'X-Local-Token': token, ...init.headers } });
      const body = await response.json(), header = response.headers.get('X-Request-ID');
      if (!response.ok || body.code !== 0 || !body.request_id || body.request_id !== header) throw new Error(JSON.stringify({ path, status: response.status, body }));
      return { data: body.data, receipt: { path, status: response.status, requestId: body.request_id, header } };
    }, { path, init });
    result.receipts.push(response.receipt); return response.data;
  };
  const health = await api('/api/gateway/health'), workspaces = await api('/api/gateway/workspaces');
  const owner = workspaces.active_workspace;
  expect(owner.gateway_id).toBe(health.gateway_id);
  const headers = { 'X-BoxTeam-Target-Gateway-Id': owner.gateway_id, 'X-BoxTeam-Workspace-Id': owner.workspace_id };
  expect((await api('/api/v1/workspace', { headers })).workspace_id).toBe(owner.workspace_id);
  const session = await api('/api/v1/sessions', { method: 'POST', headers, body: JSON.stringify({ title: '异步留言与用户消息顺序' }) });
  const thread = await api(`/api/v1/sessions/${session.session_id}/main-thread`, { headers });
  const base = `/api/v1/sessions/${session.session_id}`, questions = `${base}/threads/${thread.thread_id}/questions`;
  result.owner = { ...owner, session_id: session.session_id, thread_id: thread.thread_id };
  const uri = `boxteam://${owner.gateway_id}/workspace/${owner.workspace_id}/resources/session/${session.session_id}/threads/${thread.thread_id}`;
  await page.evaluate(uri => globalThis.workbench.commands.executeCommand('_workbench.open', uri), uri);
  const surface = page.locator(`section.boxteam-thread[data-session-id="${session.session_id}"][data-thread-id="${thread.thread_id}"]`);
  const composer = surface.locator('[data-testid="composer-input"]');
  const send = async text => {
    await expect(composer).toBeEnabled({ timeout: 30_000 });
    await composer.fill(text);
    const accepted = page.waitForResponse(response => response.request().method() === 'POST'
      && new URL(response.url()).pathname.endsWith(`/threads/${thread.thread_id}/messages`));
    await surface.locator('[data-testid="composer-send"]').click();
    const response = await accepted;
    expect(response.ok()).toBe(true);
    const body = await response.json();
    result.receipts.push({ path: new URL(response.url()).pathname, body });
    return body.data;
  };
  await planChatCalls(session.session_id, thread.thread_id, [
    { label: 'questions', calls: [{ id: 'q1', name: 'ask_user_async', arguments: { markdown: '**部署环境**\n\n请确认测试或生产环境。' } },
      { id: 'q2', name: 'ask_user_async', arguments: { markdown: '**日志保留**\n\n请确认保留周期。' } }] },
    { label: 'continue', text: '我已留下问题，继续独立工作。' },
  ], 'browser-chat-tool-loop-stub', process.env.BOXTEAM_BROWSER_MODEL_ENDPOINT);
  result.phase = 'ask';
  await send('继续工作，有问题请留言');
  const trigger = surface.locator('[data-status-section="questions"]');
  await expect(trigger).toContainText('2', { timeout: 30_000 });
  await expect(trigger).toHaveAttribute('aria-expanded', 'false');
  result.collapsedBodyReads = result.reads.length;
  expect(result.collapsedBodyReads).toBe(0);
  await trigger.click();
  await expect(surface.locator('.composer-question')).toHaveCount(2);
  await expect(surface.locator('.composer-question strong')).toHaveCount(2);
  await page.screenshot({ path: join(artifacts, 'questions.png') });
  await page.reload({ waitUntil: 'domcontentloaded' });
  await expect(trigger).toContainText('2', { timeout: 30_000 });
  await expect(trigger).toHaveAttribute('aria-expanded', 'false');
  await trigger.click();
  await expect(surface.locator('.composer-question')).toHaveCount(2);
  await surface.locator('.composer-question button').first().click();
  await expect(surface.locator('.composer-question')).toHaveCount(1);
  result.questionsAfterDelete = (await api(questions, { headers })).count;
  await trigger.click();
  result.phase = 'queue';
  const planned = await planChatCalls(session.session_id, thread.thread_id, [
    { label: 'holding', text: '当前执行完成。' }, { label: 'b', text: 'B处理完成。' }, { label: 'a', text: 'A处理完成。' },
  ], 'browser-chat-tool-loop-stub', process.env.BOXTEAM_BROWSER_MODEL_ENDPOINT);
  gate = planned[0];
  await testControl({ action: 'arm', operation_id: gate, phase: 'model.response_ready' });
  await send('先完成当前执行');
  await testControl({ action: 'acknowledged', operation_id: gate, phase: 'model.response_ready' });
  const acceptedA = await send('A 普通消息'), acceptedB = await send('B 用户回答');
  const rows = surface.locator('.chat-pending-composer-item');
  await expect(rows).toHaveCount(2);
  const order = async () => (await api(`${base}/pending-requests?thread_id=${thread.thread_id}`, { headers })).requests.map(item => item.content);
  await rows.nth(1).locator('.chat-pending-drag-handle').press('Alt+ArrowUp');
  await expect(rows.nth(0)).toContainText('B 用户回答');
  await expect.poll(order).toEqual(['B 用户回答', 'A 普通消息']);
  await rows.nth(1).locator('.chat-pending-drag-handle').dragTo(rows.nth(0));
  await expect.poll(order).toEqual(['A 普通消息', 'B 用户回答']);
  await rows.nth(1).locator('.chat-pending-drag-handle').press('Alt+ArrowUp');
  await expect.poll(order).toEqual(['B 用户回答', 'A 普通消息']);
  result.order = await order();
  await page.screenshot({ path: join(artifacts, 'queue.png') });
  await testControl({ action: 'release', operation_id: gate, phase: 'model.response_ready' }); gate = undefined;
  await expect.poll(order, { timeout: 30_000 }).toEqual([]);
  await expect(surface.locator('.chat-stream-shell')).toContainText('A处理完成。', { timeout: 30_000 });
  await expect.poll(async () => Promise.all([acceptedA, acceptedB].map(async accepted =>
    (await api(`/api/v1/jobs/${accepted.job_id}`, { headers })).status)), { timeout: 30_000 })
    .toEqual(['completed', 'completed']);
  const history = await api(`${base}/threads/${thread.thread_id}/history`, {
    method: 'POST', headers, body: JSON.stringify({ direction: 'tail', turns: 8, include: ['user'] }),
  });
  result.consumedOrder = history.items.filter(turn => [acceptedA.turn_id, acceptedB.turn_id].includes(turn.turn_id))
    .sort((left, right) => left.ordinal - right.ordinal).map(turn => turn.user_messages[0].content);
  expect(result.consumedOrder).toEqual(['B 用户回答', 'A 普通消息']);
  await expect(rows).toHaveCount(0);
  expect((await api(questions, { headers })).count).toBe(1);
  await page.setViewportSize({ width: 720, height: 600 });
  await trigger.click();
  await expect(surface.locator('.composer-question')).toHaveCount(1);
  await page.screenshot({ path: join(artifacts, 'questions-narrow.png') });
  result.phase = 'complete';
} catch (error) {
  result.error = String(error.stack ?? error);
  await page.screenshot({ path: join(artifacts, 'failure.png') }).catch(() => {});
  throw error;
} finally {
  if (gate) await testControl({ action: 'release', operation_id: gate, phase: 'model.response_ready' });
  await writeFile(process.env.BOXTEAM_BROWSER_RESULT_PATH, JSON.stringify(result, null, 2));
  await browser.close();
}
