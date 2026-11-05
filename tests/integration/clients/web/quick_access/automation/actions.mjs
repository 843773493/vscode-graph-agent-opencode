// Integration：正式 Workbench、真实 Gateway／Backend，所有任务均由产品 API 创建。
import { writeFile } from 'node:fs/promises';
import { chromium } from 'playwright';
import { expect } from 'playwright/test';
import { TestRunContext } from '../../../../../harness/js/run-context.mjs';

const required = name => { const value = process.env[name]; if (!value) throw new Error(`缺少 ${name}`); return value; };
const output = required('BOXTEAM_BROWSER_RESULT_PATH');
const run = TestRunContext.fromTestFile('tests/integration/clients/web/quick_access/automation/test_actions.py');
const result = { classification: 'Integration', phase: 'initialize', pageErrors: [], writes: [], reads: [], receipts: [], forbiddenWrites: [],
  boundary: '真实Gateway／托管Backend、正式Workbench和原任务Hook；产品API创建任务及并发revision冲突，无响应替身、存储或历史预写；model_stream仅用于启动' };
const pendingReceipts = [], responseErrors = [];
const phase = async value => { result.phase = value; await writeFile(output, JSON.stringify(result, null, 2) + '\n'); };
let page, failure;
try {
  const browser = await chromium.launch({ executablePath: required('PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH'),
    headless: true, args: ['--no-sandbox', '--disable-dev-shm-usage'] });
  run.addCleanup('automation-browser', () => browser.close());
  page = await browser.newPage({ viewport: { width: 1600, height: 1150 } });
  page.on('pageerror', cause => result.pageErrors.push(String(cause)));
  page.on('request', request => {
    const path = new URL(request.url()).pathname, headers = request.headers();
    const observed = { phase: result.phase, path, method: request.method(),
      owner: { gateway_id: headers['x-boxteam-target-gateway-id'], workspace_id: headers['x-boxteam-workspace-id'] },
      body: request.method() === 'GET' ? undefined : request.postDataJSON() };
    if (path.startsWith('/api/v1/persistent-tasks')) (request.method() === 'GET' ? result.reads : result.writes).push(observed);
    if (result.phase !== 'prepare' && request.method() !== 'GET' && /\/(messages|jobs)(\/|$)/.test(path)) result.forbiddenWrites.push(observed);
  });
  page.on('requestfinished', request => {
    if (!new URL(request.url()).pathname.startsWith('/api/v1/persistent-tasks') || request.method() === 'GET') return;
    const observedPhase = result.phase;
    pendingReceipts.push((async () => {
      const response = await request.response(), body = await response.json();
      result.receipts.push({ phase: observedPhase, path: new URL(request.url()).pathname, status: response.status(),
        header: response.headers()['x-request-id'], request_id: body.request_id, data: body.data });
    })().catch(cause => responseErrors.push(String(cause))));
  });
  await page.goto(required('BOXTEAM_BROWSER_BASE_URL'), { waitUntil: 'domcontentloaded' });
  await expect(page.locator('.native-session-navigation')).toBeVisible({ timeout: 30_000 });
  const api = async (path, owner, body, method = body === undefined ? 'GET' : 'POST') => page.evaluate(async ({ path, owner, body, method }) => {
    const credential = await (await fetch('/api/gateway/auth/local-credential')).json();
    const response = await fetch(path, { method, body: body === undefined ? undefined : JSON.stringify(body),
      headers: { 'X-Local-Token': credential.data.token, 'Content-Type': 'application/json',
        ...(owner ? { 'X-BoxTeam-Target-Gateway-Id': owner.gateway_id, 'X-BoxTeam-Workspace-Id': owner.workspace_id } : {}) } });
    const payload = await response.json();
    if (!response.ok || payload.request_id !== response.headers.get('X-Request-ID')) throw new Error(JSON.stringify({ path, status: response.status, payload }));
    return payload.data;
  }, { path, owner, body, method });
  const command = (id, ...args) => page.evaluate(({ id, args }) => globalThis.workbench.commands.executeCommand(id, ...args), { id, args });
  const health = await api('/api/gateway/health'), registered = await api('/api/gateway/workspaces');
  const a = registered.active_workspace;
  expect(a.gateway_id).toBe(health.gateway_id);
  expect((await api('/api/v1/workspace', a)).workspace_id).toBe(a.workspace_id);
  await phase('prepare');
  const list = await api('/api/gateway/managed-workspaces', undefined, { root_path: required('BOXTEAM_AUTOMATION_OTHER_WORKSPACE'), name: 'Automation B' });
  const b = list.items.find(item => item.name === 'Automation B');
  expect(b?.workspace_id).toBeTruthy();
  const configuration = uri => ({
    name: 'Needle 自动化', trigger: { type_id: 'builtin.manual', version: '1', timezone: 'UTC' },
    condition: { type_id: 'builtin.always', version: '1' }, context: { type_id: 'builtin.literal', version: '1', mode: 'event', content: '仅用于入口验收' },
    action: { type_id: 'builtin.runtime_source', version: '1', target_uri: uri, delivery_mode: 'context_only', delivery_policy: 'after_turn' },
    policies: { completion: 'continuous', misfire: 'skip', match_mode: 'each_match' },
  });
  const tasks = [];
  for (const owner of [a, b]) {
    const session = await api('/api/v1/sessions', owner, { title: '自动化目标' });
    const main = await api(`/api/v1/sessions/${session.session_id}/main-thread`, owner);
    const uri = `boxteam://${owner.gateway_id}/workspace/${owner.workspace_id}/resources/session/${session.session_id}/threads/${main.thread_id}`;
    tasks.push(await api('/api/v1/persistent-tasks', owner, { idempotency_key: crypto.randomUUID(), configuration: configuration(uri), enabled: true }));
  }
  await api(`/api/gateway/workspaces/${a.workspace_id}/activate`, a, {});
  await page.reload({ waitUntil: 'domcontentloaded' });
  await expect(page.getByRole('button', { name: '刷新资源树', exact: true })).toBeVisible({ timeout: 30_000 });
  const quick = page.locator('.quick-input-widget'), input = quick.locator('input').first();
  const writes = value => result.writes.filter(item => item.phase === value);
  const current = async (owner, id) => (await api('/api/v1/persistent-tasks', owner)).items.find(item => item.task_id === id);
  const show = async owner => {
    await command('workbench.action.quickOpen', 'automation Needle');
    await expect(input).toHaveValue('automation Needle');
    if (owner) {
      await quick.getByRole('button', { name: '选择任务所属工作区', exact: true }).click();
      await expect(quick).toContainText('选择快速访问搜索范围');
      await quick.locator('.monaco-list-row').filter({ hasText: a.workspace_id }).getByRole('checkbox').uncheck();
      await quick.locator('.monaco-list-row').filter({ hasText: owner.workspace_id }).getByRole('checkbox').check();
      await quick.getByRole('button', { name: 'OK', exact: true }).click();
      await expect(input).toHaveValue('automation Needle');
    }
    await expect(quick.locator('.monaco-list-row').filter({ hasText: owner?.workspace_id ?? a.workspace_id }).filter({ hasText: 'Needle 自动化' })).toBeVisible();
  };
  const choose = async label => {
    await page.keyboard.press('Enter');
    await expect(quick).toContainText('任务动作 · Needle 自动化');
    await quick.locator('.monaco-list-row').filter({ hasText: label }).click();
  };

  await phase('browse'); await show();
  await page.keyboard.press('ArrowDown'); await page.keyboard.press('ArrowUp'); await page.keyboard.press('Escape');
  expect(writes(result.phase)).toEqual([]); expect(await current(a, tasks[0].task_id)).toMatchObject({ lifecycle: 'active', revision: tasks[0].revision });
  result.browseOnly = true;
  await phase('cancel'); await show(); await page.keyboard.press('Enter');
  await expect(quick).toContainText('任务动作 · Needle 自动化'); await page.keyboard.press('Escape');
  await expect(input).toHaveValue('automation Needle'); await page.keyboard.press('Escape');
  expect(writes(result.phase)).toEqual([]); result.cancelOnly = true;

  await phase('direct-pause'); await show(); await choose('暂停任务');
  await expect.poll(async () => (await current(a, tasks[0].task_id)).lifecycle).toBe('paused');
  expect(writes(result.phase)).toHaveLength(1); expect(writes(result.phase)[0].body).toEqual({ expected_revision: tasks[0].revision, action: 'pause' });
  expect(await page.locator('.persistent-tasks').count()).toBe(0);
  await phase('direct-resume'); await show(); await choose('恢复任务');
  await expect.poll(async () => (await current(a, tasks[0].task_id)).lifecycle).toBe('active');
  expect(writes(result.phase)).toHaveLength(1); result.directControl = true;

  await phase('trial'); await show(); await choose('检查试运行');
  await expect.poll(async () => (await api(`/api/v1/persistent-tasks/${tasks[0].task_id}/runs`, a)).items[0]?.phase).toBe('checked');
  const trial = (await api(`/api/v1/persistent-tasks/${tasks[0].task_id}/runs`, a)).items[0];
  expect(trial.check_only).toBe(true); expect(trial.delivery).toBeNull();
  expect(writes(result.phase)).toHaveLength(1); expect(writes(result.phase)[0].body.check_only).toBe(true); result.trialOnly = true;

  await phase('history'); await show(); await choose('查看运行历史');
  const detail = page.locator('.persistent-task-detail').filter({ visible: true });
  await expect(detail).toContainText('运行记录'); await expect(detail).toContainText('检查试运行'); await expect(detail).toContainText('检查已结束');
  expect(writes(result.phase)).toEqual([]); result.originalHistory = true;
  await phase('react-pause'); await detail.getByRole('button', { name: '暂停任务', exact: true }).click();
  await expect(detail.getByRole('button', { name: '恢复任务', exact: true })).toBeVisible();
  expect(writes(result.phase)).toHaveLength(1);
  await phase('react-resume'); await detail.getByRole('button', { name: '恢复任务', exact: true }).click();
  await expect(detail.getByRole('button', { name: '暂停任务', exact: true })).toBeVisible(); result.originalReact = true;

  await phase('edit'); const beforeEdit = await current(a, tasks[0].task_id);
  await show(); await choose('编辑任务');
  await expect(detail.getByRole('textbox', { name: '提示内容', exact: true })).toBeVisible();
  await detail.getByRole('textbox', { name: '提示内容', exact: true }).fill('产品入口保存后的正文');
  await detail.getByRole('button', { name: '保存任务', exact: true }).click();
  await expect.poll(async () => (await current(a, tasks[0].task_id)).configuration.context.content).toBe('产品入口保存后的正文');
  expect(writes(result.phase)).toHaveLength(1);
  expect(writes(result.phase)[0].body.expected_revision).toBe(beforeEdit.revision); result.originalEdit = true;

  await phase('conflict'); await show(); await page.keyboard.press('Enter');
  await expect(quick).toContainText('任务动作 · Needle 自动化');
  const before = await current(a, tasks[0].task_id);
  await api(`/api/v1/persistent-tasks/${before.task_id}/control`, a, { expected_revision: before.revision, action: 'pause' });
  await quick.locator('.monaco-list-row').filter({ hasText: '暂停任务' }).click();
  await expect.poll(() => result.receipts.some(item => item.phase === 'conflict' && item.status === 409)).toBe(true);
  await expect(page.locator('.notification-list-item-message').filter({ hasText: 'revision' })).toBeVisible();
  expect(writes(result.phase)).toHaveLength(2);
  expect(writes(result.phase)[1].body.expected_revision).toBe(before.revision);
  expect((await current(a, before.task_id)).lifecycle).toBe('paused'); result.revisionConflict = true;

  await phase('owner-B'); await show(b); await choose('暂停任务');
  await expect.poll(async () => (await current(b, tasks[1].task_id)).lifecycle).toBe('paused');
  expect(writes(result.phase)).toHaveLength(1); expect(writes(result.phase)[0].owner).toEqual({ gateway_id: b.gateway_id, workspace_id: b.workspace_id });
  expect((await api('/api/gateway/workspaces?check_health=false')).active_workspace).toEqual(a); result.ownerB = true;

  await phase('terminate-cancel');
  await detail.getByRole('button', { name: '终止任务', exact: true }).click();
  const dialog = page.getByRole('dialog').filter({ hasText: '终止持久任务？' });
  await expect(dialog).toContainText('已经接受的工作执行继续由执行入口管理');
  await dialog.getByRole('button', { name: /Cancel|取消/ }).click();
  expect(writes(result.phase)).toEqual([]);
  await phase('terminate-confirm');
  await detail.getByRole('button', { name: '终止任务', exact: true }).click();
  await expect(dialog).toBeVisible();
  await dialog.getByRole('button', { name: '终止任务', exact: true }).click();
  await expect.poll(async () => (await current(a, tasks[0].task_id)).lifecycle).toBe('terminated');
  expect(writes(result.phase)).toHaveLength(1); result.confirmedTermination = true;
  await Promise.all(pendingReceipts); expect(responseErrors).toEqual([]);
  for (const receipt of result.receipts) { expect(receipt.request_id).toBeTruthy(); expect(receipt.header).toBe(receipt.request_id); }
  expect(result.pageErrors).toEqual([]); expect(result.forbiddenWrites).toEqual([]);
  await page.screenshot({ path: output.replace(/\.json$/, '.png'), fullPage: true });
  await phase('complete');
} catch (cause) { failure = cause; }
finally {
  if (failure) {
    result.error = String(failure.stack ?? failure);
    if (page) await page.screenshot({ path: output.replace(/\.json$/, '.failure.png'), fullPage: true });
  }
  await writeFile(output, JSON.stringify(result, null, 2) + '\n');
  await run.close();
}
if (failure) throw failure;
console.log(JSON.stringify({ phase: result.phase, output }));
