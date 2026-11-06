// Integration：真实本地文件、HTTP accepted 快照、Gateway 和正式 Workbench；模型回放只供启动。
import { access, writeFile } from 'node:fs/promises';
import { chromium } from 'playwright';
import { expect } from 'playwright/test';
import { TestRunContext } from '../../../../../harness/js/run-context.mjs';

const required = name => { const value = process.env[name]; if (!value) throw new Error(`缺少 ${name}`); return value; };
const output = required('BOXTEAM_BROWSER_RESULT_PATH'), screenshot = required('BOXTEAM_BROWSER_SCREENSHOT_PATH');
const run = TestRunContext.fromTestFile('tests/integration/clients/web/quick_access/resources/test_skill_sources.py');
const result = { classification: 'Integration', phase: 'initialize', pageErrors: [], discoveries: [], routes: [], parses: [], reads: [], receipts: [], forbiddenWrites: [],
  boundary: '真实 Gateway／Backend、正式 Workbench、本地文件和 manual HTTP 来源；从宿主删除本地入口、添加新包，并在显式 refresh 前将一项HTTP来源改为非法 UTF-8，不替换产品响应、不预写 registry／存储、不创建 Session 或编译 runtime' };
const pending = [], responseErrors = [];
const phase = async value => { result.phase = value; await writeFile(output, JSON.stringify(result, null, 2) + '\n'); };
let page, failure;
try {
  const browser = await chromium.launch({ executablePath: required('PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH'), headless: true,
    args: ['--no-sandbox', '--disable-dev-shm-usage'] });
  run.addCleanup('Skill 来源 Chromium', () => browser.close());
  page = await browser.newPage({ viewport: { width: 1700, height: 1100 } }); page.setDefaultTimeout(15_000);
  page.on('pageerror', cause => result.pageErrors.push(String(cause)));
  page.on('request', request => {
    const path = new URL(request.url()).pathname, method = request.method(), headers = request.headers();
    const observed = { phase: result.phase, path, method, gatewayId: headers['x-boxteam-target-gateway-id'], workspaceId: headers['x-boxteam-workspace-id'],
      body: method === 'GET' ? undefined : request.postDataJSON() };
    if (['/api/v1/resources/agent-spec', '/api/v1/resources/skill-sources', '/api/v1/resources/local-skills'].includes(path)) result.discoveries.push(observed);
    if (path === '/api/gateway/workspaces') result.routes.push(observed);
    if (path === '/api/gateway/resource-addresses/parse') result.parses.push(observed);
    if (path === '/api/v1/resources/navigation') result.reads.push(observed);
    if (method !== 'GET' && (/\/(messages|jobs|persistent-tasks|text-mutations|untrack|sessions)(\/|$)/.test(path)
      || /\/api\/gateway\/workspaces\/[^/]+\/(start|activate|connect|stop)$/.test(path))) result.forbiddenWrites.push(observed);
  });
  page.on('requestfinished', request => {
    const path = new URL(request.url()).pathname;
    if (!['/api/v1/resources/agent-spec', '/api/v1/resources/skill-sources', '/api/v1/resources/local-skills', '/api/v1/resources/navigation', '/api/gateway/resource-addresses/parse'].includes(path)) return;
    const observedPhase = result.phase;
    pending.push((async () => {
      const response = await request.response(), body = await response.json();
      result.receipts.push({ phase: observedPhase, path, status: response.status(), header: response.headers()['x-request-id'],
        request_id: body.request_id, data: body.data, detail: body.detail });
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
  const a = registered.active_workspace, b = registered.items.find(item => item.name === 'Skill B');
  expect(b?.workspace_id).toBeTruthy(); expect(a.gateway_id).toBe(health.gateway_id);
  expect((await api('/api/v1/workspace', a)).workspace_id).toBe(a.workspace_id);
  // 容器先挂载；等原导航完成身份和目录初始化，再冻结本次来源选择。
  await expect(page.locator('.native-session-navigation .workspace-label').filter({ hasText: 'Skill B' })).toBeVisible();
  await command('workbench.action.closeAllEditors');
  const quick = page.locator('.quick-input-widget'), input = quick.locator('input').first(), groups = page.locator('.editor-group-container');
  const text = index => groups.nth(index).locator('.editor-instance .monaco-editor').filter({ visible: true }).locator('.view-lines');
  const owner = item => ({ gatewayId: item.gateway_id, workspaceId: item.workspace_id });
  const candidates = item => quick.locator('.monaco-list-row').filter({ hasText: 'network-demo' }).filter({ hasText: '配置 Skill' }).filter({ hasText: item.workspace_id });
  const local = item => quick.locator('.monaco-list-row').filter({ hasText: '本地 Skill' }).filter({ hasText: item.workspace_id });
  const source = (item, priority) => candidates(item).filter({ hasText: `优先级 ${priority}` });
  const show = async item => {
    await command('boxteam.quickAccess.resources', ...(item ? [owner(item)] : []));
    await expect(input).toHaveValue('resource '); await expect(candidates(item ?? b)).toHaveCount(2);
  };
  const select = async (item, priority) => {
    await input.fill('resource network-demo'); await expect(candidates(item)).toHaveCount(2); await input.focus();
    for (let i = 0; i < 3 && !(await source(item, priority).evaluate(element => element.classList.contains('focused'))); i++) await page.keyboard.press('ArrowDown');
    await expect(source(item, priority)).toHaveClass(/focused/);
  };
  const currentReads = () => result.reads.filter(item => item.phase === result.phase);
  await phase('browse'); await show(a);
  const readsBefore = result.discoveries.length, routesBefore = result.routes.length;
  for (const filter of ['n', 'network', 'network-demo']) { await input.fill(`resource ${filter}`); await expect(candidates(a)).toHaveCount(2); }
  expect(result.discoveries).toHaveLength(readsBefore); expect(result.routes).toHaveLength(routesBefore); result.filterReuse = true;
  await page.keyboard.press('ArrowDown'); await page.keyboard.press('ArrowUp'); await page.keyboard.press('Escape');
  expect(currentReads()).toEqual([]); expect(result.parses).toEqual([]); await expect(page.locator('.tab').filter({ hasText: 'SKILL.md' })).toHaveCount(0);
  result.metadataOnly = true;
  await phase('scope-cancel'); await show(a); await quick.getByRole('button', { name: '选择来源所属工作区', exact: true }).click();
  await expect(quick).toContainText('选择快速访问搜索范围'); await page.keyboard.press('Escape');
  await expect(candidates(a)).toHaveCount(2); await page.keyboard.press('Escape'); expect(currentReads()).toEqual([]); result.scopeCancel = true;
  await phase('open-B'); await show(a); await quick.getByRole('button', { name: '选择来源所属工作区', exact: true }).click();
  await quick.locator('.monaco-list-row').filter({ hasText: a.workspace_id }).getByRole('checkbox').uncheck();
  await quick.locator('.monaco-list-row').filter({ hasText: b.workspace_id }).getByRole('checkbox').check();
  await quick.getByRole('button', { name: 'OK', exact: true }).click();
  await expect(candidates(b)).toHaveCount(2); result.sameNameSources = true;
  await select(b, 7); await page.keyboard.press('Enter'); await expect(text(0)).toContainText('HTTP-SKILL-B-HIGHER');
  expect(currentReads()).toHaveLength(1); expect(currentReads()[0]).toMatchObject({ workspaceId: b.workspace_id, body: { operation: 'read' } });
  expect(result.parses.find(item => item.phase === result.phase).body.resource_gateway_id).toBe(b.gateway_id); result.originalOpen = true;
  await phase('read-only'); await text(0).click(); await page.keyboard.type('MUST-NOT-CHANGE-SKILL');
  await expect(text(0)).toContainText('HTTP-SKILL-B-HIGHER'); await expect(text(0)).not.toContainText('MUST-NOT-CHANGE-SKILL'); result.readOnly = true;
  await phase('side-owner'); await show(); await select(b, 7); await page.keyboard.press('Alt+Enter');
  await expect(groups).toHaveCount(2); await expect(text(1)).toContainText('HTTP-SKILL-B-HIGHER');
  expect(currentReads()).toEqual([]); expect((await api('/api/gateway/workspaces?check_health=false')).active_workspace).toEqual(a); result.sideAndOwner = true;
  await phase('explicit-A'); await show(a); await select(a, 0); await page.keyboard.press('Enter');
  await expect(text(1)).toContainText('HTTP-SKILL-A-FIRST'); expect(currentReads()).toHaveLength(1);
  expect(currentReads()[0].workspaceId).toBe(a.workspace_id); result.explicitOwner = true;
  await phase('local-open'); await command('workbench.action.closeAllEditors'); await show(b);
  await expect(local(b)).toHaveCount(1); await expect(local(b)).toContainText('network-demo');
  await expect(quick.locator('.monaco-list-row').filter({ hasText: 'network-demo' })).toHaveCount(3);
  await expect(quick).not.toContainText('different-runtime-name'); await expect(quick).not.toContainText('missing-entry'); result.localDiscovery = true;
  const selectLocal = async () => {
    await input.fill('resource network-demo'); await input.focus();
    for (let i = 0; i < 4 && !(await local(b).evaluate(element => element.classList.contains('focused'))); i++) await page.keyboard.press('ArrowDown');
    await expect(local(b)).toHaveClass(/focused/);
  };
  await selectLocal(); await page.keyboard.press('Enter'); await expect(text(0)).toContainText('LOCAL-SKILL-B');
  expect(currentReads()).toHaveLength(1); expect(currentReads()[0]).toMatchObject({ workspaceId: b.workspace_id,
    body: { uri: expect.stringContaining('/skills/network-demo/SKILL.md'), operation: 'read' } }); result.localOpen = true;
  await phase('local-read-only'); await text(0).click(); await page.keyboard.type('MUST-NOT-CHANGE-LOCAL-SKILL');
  await expect(text(0)).toContainText('LOCAL-SKILL-B'); await expect(text(0)).not.toContainText('MUST-NOT-CHANGE-LOCAL-SKILL'); result.localReadOnly = true;
  await command('workbench.action.closeAllEditors'); await show(b); await selectLocal();
  await phase('local-delete-ready'); await writeFile(`${required('BOXTEAM_LOCAL_SKILL_FAULT_READY')}.request`, 'ready\n');
  await expect.poll(async () => {
    try { await access(required('BOXTEAM_LOCAL_SKILL_FAULT_READY')); return true; }
    catch (cause) { if (cause.code === 'ENOENT') return false; throw cause; }
  }).toBe(true);
  await phase('local-stale'); const localGroupCount = await groups.count(); await input.focus(); await page.keyboard.press('Alt+Enter');
  await expect.poll(() => result.receipts.some(item => item.phase === result.phase && item.path === '/api/v1/resources/navigation' && item.status === 404)).toBe(true);
  await expect(page.locator('.notification-list-item-message').filter({ hasText: '资源不存在' })).toBeVisible();
  await expect(groups).toHaveCount(localGroupCount); await expect(page.locator('.tab').filter({ hasText: 'SKILL.md' })).toHaveCount(0); result.localStale = true;
  await phase('local-fresh'); await show(b); await expect(local(b)).toHaveCount(1); await expect(local(b)).toContainText('local-new');
  await expect(local(b)).not.toContainText('network-demo'); await page.keyboard.press('Escape'); expect(currentReads()).toEqual([]); result.localFresh = true;
  await command('workbench.action.closeAllEditors'); await show(b); await select(b, 7);
  await phase('invalidate-ready');
  await writeFile(`${required('BOXTEAM_SKILL_FAULT_READY')}.request`, 'ready\n');
  await expect.poll(async () => {
    try { await access(required('BOXTEAM_SKILL_FAULT_READY')); return true; }
    catch (cause) { if (cause.code === 'ENOENT') return false; throw cause; }
  }).toBe(true);
  await phase('explicit-refresh'); await api('/api/v1/resources/skill-sources/refresh', b, {});
  await phase('stale-candidate'); const countBefore = await groups.count(); await input.focus(); await page.keyboard.press('Alt+Enter');
  await expect.poll(() => result.receipts.some(item => item.phase === result.phase && item.path === '/api/v1/resources/navigation' && item.status === 404)).toBe(true);
  expect(result.receipts.find(item => item.phase === result.phase && item.path === '/api/v1/resources/navigation')).toMatchObject({
    status: 404, detail: { owner: 'resource_uri_resolver', code: 'unknown_resource', message: '会话产物不存在或已不可用' },
  });
  await expect(page.locator('.notification-list-item-message').filter({ hasText: '会话产物不存在或已不可用' })).toBeVisible();
  await expect(groups).toHaveCount(countBefore); await expect(page.locator('.tab').filter({ hasText: 'SKILL.md' })).toHaveCount(0); result.originalFailure = true;
  await phase('unavailable'); await show(b); await expect(source(b, 7)).toContainText('来源不可用'); await expect(source(b, 7)).toContainText('source-unavailable');
  await page.keyboard.press('Escape'); expect(currentReads()).toEqual([]); result.unavailable = true;
  await Promise.all(pending); expect(responseErrors).toEqual([]);
  expect(result.receipts.every(item => item.header && item.header === item.request_id)).toBe(true);
  const metadata = result.receipts.filter(item => item.path === '/api/v1/resources/skill-sources');
  expect(metadata.length).toBeGreaterThan(0);
  expect(metadata.every(item => item.data.entries.every(entry => !('content' in entry) && !('fetch' in entry) && !('url' in entry)))).toBe(true);
  const localMetadata = result.receipts.filter(item => item.path === '/api/v1/resources/local-skills');
  expect(localMetadata.length).toBeGreaterThan(0);
  expect(localMetadata.every(item => item.data.entries.every(entry => entry.node.resource_shape === 'file' && entry.node.operations.includes('read')
    && !('content' in entry) && !('revision' in entry)))).toBe(true);
  expect(result.forbiddenWrites).toEqual([]); expect(result.pageErrors).toEqual([]); await phase('complete');
} catch (cause) { failure = cause; result.error = String(cause.stack ?? cause); }
finally {
  if (page) await page.screenshot({ path: screenshot, fullPage: false });
  await writeFile(output, JSON.stringify(result, null, 2) + '\n'); await run.close();
}
if (failure) throw failure;
