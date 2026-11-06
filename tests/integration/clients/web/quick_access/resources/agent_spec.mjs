// Integration：普通AGENTS文件仅作输入；发现、解析和正文均经真实产品入口。
import { unlink, writeFile } from 'node:fs/promises';
import { dirname, join } from 'node:path';
import { chromium } from 'playwright';
import { expect } from 'playwright/test';
import { TestRunContext } from '../../../../../harness/js/run-context.mjs';

const required = name => { const value = process.env[name]; if (!value) throw new Error(`缺少 ${name}`); return value; };
const output = required('BOXTEAM_BROWSER_RESULT_PATH'), screenshot = required('BOXTEAM_BROWSER_SCREENSHOT_PATH');
const run = TestRunContext.fromTestFile('tests/integration/clients/web/quick_access/resources/test_agent_spec.py');
const result = { classification: 'Integration', phase: 'initialize', pageErrors: [], discoveries: [], parses: [], reads: [], receipts: [], forbiddenWrites: [],
  boundary: '真实Gateway／托管Backend、正式Workbench；只准备普通AGENTS输入，不编译runtime、不预写来源registry／浏览器存储，不替换HTTP；model_stream仅供服务启动' };
const pending = [], responseErrors = [];
const phase = async value => { result.phase = value; await writeFile(output, JSON.stringify(result, null, 2) + '\n'); };
let page, failure;
try {
  const browser = await chromium.launch({ executablePath: required('PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH'), headless: true,
    args: ['--no-sandbox', '--disable-dev-shm-usage'] });
  run.addCleanup('来源页面Chromium', () => browser.close());
  page = await browser.newPage({ viewport: { width: 1700, height: 1100 } });
  page.setDefaultTimeout(15_000);
  page.on('pageerror', cause => result.pageErrors.push(String(cause)));
  page.on('request', request => {
    const path = new URL(request.url()).pathname, headers = request.headers(), method = request.method();
    const observed = { phase: result.phase, path, method, gatewayId: headers['x-boxteam-target-gateway-id'],
      workspaceId: headers['x-boxteam-workspace-id'], body: method === 'GET' ? undefined : request.postDataJSON() };
    if (path === '/api/v1/resources/agent-spec') result.discoveries.push(observed);
    if (path === '/api/gateway/resource-addresses/parse') result.parses.push(observed);
    if (path === '/api/v1/resources/navigation') result.reads.push(observed);
    if (result.phase !== 'prepare' && method !== 'GET' && (/\/(messages|jobs|persistent-tasks|text-mutations|untrack)(\/|$)/.test(path)
      || /\/api\/gateway\/workspaces\/[^/]+\/(start|activate|connect|stop)$/.test(path))) result.forbiddenWrites.push(observed);
  });
  page.on('requestfinished', request => {
    const path = new URL(request.url()).pathname;
    if (!['/api/v1/resources/agent-spec', '/api/v1/resources/navigation', '/api/gateway/resource-addresses/parse'].includes(path)) return;
    const observedPhase = result.phase;
    pending.push((async () => {
      const response = await request.response(), body = await response.json();
      result.receipts.push({ phase: observedPhase, path, status: response.status(),
        header: response.headers()['x-request-id'], request_id: body.request_id, data: body.data, detail: body.detail });
    })().catch(cause => responseErrors.push(String(cause))));
  });
  await page.goto(required('BOXTEAM_BROWSER_BASE_URL'), { waitUntil: 'domcontentloaded' });
  await expect(page.locator('.native-session-navigation')).toBeVisible({ timeout: 30_000 });
  const api = (path, owner, body) => page.evaluate(async ({ path, owner, body }) => {
    const credential = await (await fetch('/api/gateway/auth/local-credential')).json();
    const response = await fetch(path, { method: body === undefined ? 'GET' : 'POST', body: body === undefined ? undefined : JSON.stringify(body),
      headers: { 'X-Local-Token': credential.data.token, 'Content-Type': 'application/json', ...(owner ? {
        'X-BoxTeam-Target-Gateway-Id': owner.gateway_id, 'X-BoxTeam-Workspace-Id': owner.workspace_id } : {}) } });
    const payload = await response.json();
    if (!response.ok || payload.request_id !== response.headers.get('X-Request-ID')) throw new Error(JSON.stringify({ path, status: response.status, payload }));
    return payload.data;
  }, { path, owner, body });
  const command = (id, ...args) => page.evaluate(({ id, args }) => globalThis.workbench.commands.executeCommand(id, ...args), { id, args });
  const health = await api('/api/gateway/health'), registered = await api('/api/gateway/workspaces?check_health=false');
  const a = registered.active_workspace;
  expect(a.gateway_id).toBe(health.gateway_id); expect((await api('/api/v1/workspace', a)).workspace_id).toBe(a.workspace_id);
  await phase('prepare');
  const managed = await api('/api/gateway/managed-workspaces', undefined, { root_path: required('BOXTEAM_SOURCE_OTHER_WORKSPACE'), name: 'Resource B' });
  const b = managed.items.find(item => item.name === 'Resource B'); expect(b?.workspace_id).toBeTruthy();
  await page.reload({ waitUntil: 'domcontentloaded' });
  await expect(page.getByRole('button', { name: '刷新资源树', exact: true })).toBeVisible({ timeout: 30_000 });
  await command('workbench.action.closeAllEditors');
  const quick = page.locator('.quick-input-widget'), input = quick.locator('input').first(), groups = page.locator('.editor-group-container');
  const text = index => groups.nth(index).locator('.editor-instance .monaco-editor').filter({ visible: true }).locator('.view-lines');
  const camelOwner = owner => ({ gatewayId: owner.gateway_id, workspaceId: owner.workspace_id });
  const candidate = owner => quick.locator('.monaco-list-row').filter({ hasText: 'AGENTS.md' }).filter({ hasText: owner.workspace_id });
  const show = async owner => {
    if (owner) await command('boxteam.quickAccess.resources', camelOwner(owner));
    else await command('boxteam.quickAccess.resources');
    await expect(input).toHaveValue('resource ');
    await expect(candidate(owner ?? b)).toBeVisible();
  };
  const currentReads = () => result.reads.filter(item => item.phase === result.phase);
  await phase('browse'); await show(a);
  await page.keyboard.press('ArrowDown'); await page.keyboard.press('ArrowUp'); await page.keyboard.press('Escape');
  expect(currentReads()).toEqual([]); expect(result.parses.filter(item => item.phase === result.phase)).toEqual([]);
  await expect(page.locator('.tab').filter({ hasText: 'AGENTS.md' })).toHaveCount(0); result.browseOnly = true;
  await phase('scope-cancel'); await show(a);
  await quick.getByRole('button', { name: '选择来源所属工作区', exact: true }).click();
  await expect(quick).toContainText('选择快速访问搜索范围'); await page.keyboard.press('Escape');
  await expect(candidate(a)).toBeVisible(); await page.keyboard.press('Escape'); expect(currentReads()).toEqual([]); result.scopeCancel = true;
  await phase('open-B'); await show(a);
  await quick.getByRole('button', { name: '选择来源所属工作区', exact: true }).click();
  await quick.locator('.monaco-list-row').filter({ hasText: a.workspace_id }).getByRole('checkbox').uncheck();
  await quick.locator('.monaco-list-row').filter({ hasText: b.workspace_id }).getByRole('checkbox').check();
  await quick.getByRole('button', { name: 'OK', exact: true }).click();
  await expect(candidate(b)).toBeVisible(); await input.focus(); await page.keyboard.press('Enter');
  await expect(text(0)).toContainText('AGENTS-OWNER-B');
  expect(currentReads()).toHaveLength(1); expect(currentReads()[0]).toMatchObject({ workspaceId: b.workspace_id, body: { operation: 'read' } });
  expect(result.parses.filter(item => item.phase === result.phase)).toHaveLength(1);
  expect(result.parses.find(item => item.phase === result.phase).body.resource_gateway_id).toBe(b.gateway_id); result.originalOpen = true;
  await phase('read-only'); await text(0).click(); await page.keyboard.type('MUST-NOT-CHANGE-AGENTS');
  await expect(text(0)).toContainText('AGENTS-OWNER-B'); await expect(text(0)).not.toContainText('MUST-NOT-CHANGE-AGENTS');
  // 原只读输入的保存转为另存为；取消原对话框，不请求新的写入目标。
  await phase('save-as-cancel');
  await page.evaluate(() => {
    globalThis.agentSpecSave = { done: false };
    void globalThis.workbench.commands.executeCommand('workbench.action.files.save').then(
      () => { globalThis.agentSpecSave = { done: true }; }, error => { globalThis.agentSpecSave = { done: true, error: String(error) }; });
  });
  await expect(quick).toBeVisible(); await page.keyboard.press('Escape');
  await expect.poll(() => page.evaluate(() => globalThis.agentSpecSave)).toEqual({ done: true });
  await expect(text(0)).toContainText('AGENTS-OWNER-B'); await expect(text(0)).not.toContainText('MUST-NOT-CHANGE-AGENTS'); result.readOnly = true;
  await phase('side-owner'); await show(); await input.focus(); await page.keyboard.press('Alt+Enter');
  await expect(groups).toHaveCount(2); await expect(text(1)).toContainText('AGENTS-OWNER-B');
  expect(currentReads()).toEqual([]); expect((await api('/api/gateway/workspaces?check_health=false')).active_workspace).toEqual(a); result.sideAndOwner = true;
  await phase('explicit-owner'); await show(a); await input.focus(); await page.keyboard.press('Enter');
  await expect(text(1)).toContainText('AGENTS-OWNER-A'); expect(currentReads()).toHaveLength(1);
  expect(currentReads()[0].workspaceId).toBe(a.workspace_id); result.explicitOwner = true;
  await page.screenshot({ path: join(dirname(screenshot), 'source-isolation.png'), fullPage: false });
  await command('workbench.action.closeAllEditors');
  await phase('deleted-source'); await show(b);
  await unlink(join(required('BOXTEAM_SOURCE_OTHER_WORKSPACE'), 'AGENTS.md'));
  const countBefore = await groups.count(); await input.focus(); await page.keyboard.press('Alt+Enter');
  await expect.poll(() => result.receipts.some(item => item.phase === result.phase && item.path === '/api/v1/resources/navigation' && item.status === 404)).toBe(true);
  expect(result.receipts.find(item => item.phase === result.phase && item.path === '/api/v1/resources/navigation')).toMatchObject({
    status: 404, detail: { owner: 'resource_uri_resolver', code: 'unknown_resource', message: '资源不存在' },
  });
  expect(currentReads()).toHaveLength(1);
  await expect(page.locator('.notification-list-item-message').filter({ hasText: '资源不存在' })).toBeVisible();
  await expect(groups).toHaveCount(countBefore); await expect(page.locator('.tab').filter({ hasText: 'AGENTS.md' })).toHaveCount(0);
  await command('boxteam.quickAccess.resources', camelOwner(b));
  await expect(quick).toContainText('AGENTS 来源不可用'); await page.keyboard.press('Escape'); result.deletedSource = true;
  await Promise.all(pending); expect(responseErrors).toEqual([]);
  expect(result.receipts.every(item => item.header && item.header === item.request_id)).toBe(true);
  expect(result.reads.every(item => item.body.operation === 'read')).toBe(true);
  expect(result.forbiddenWrites).toEqual([]); expect(result.pageErrors).toEqual([]);
  await phase('complete');
} catch (cause) { failure = cause; result.error = String(cause.stack ?? cause); }
finally {
  if (page) await page.screenshot({ path: screenshot, fullPage: false });
  await writeFile(output, JSON.stringify(result, null, 2) + '\n'); await run.close();
}
if (failure) throw failure;
