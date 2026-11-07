// Integration：正式 Workbench／真实 Gateway 和来源 writer，无业务响应替身或浏览器存储预写。
import { writeFile } from 'node:fs/promises';
import { chromium } from 'playwright';
import { expect } from 'playwright/test';
import { TestRunContext } from '../../../../harness/js/run-context.mjs';

const required = name => {
  const value = process.env[name];
  if (!value) throw new Error(`目标设置验收缺少 ${name}`);
  return value;
};
const output = required('BOXTEAM_BROWSER_RESULT_PATH');
const run = TestRunContext.fromTestFile('tests/integration/clients/web/quick_access/test_target_settings.py');
const sourceKey = 'workspace_root_mutable_override', sourcePath = `/api/v1/config/sources/${sourceKey}`;
const result = { classification: 'Integration', phase: 'prepare', pageErrors: [], requests: [], receipts: [], apiReceipts: [], forbiddenWrites: [],
  managementRequests: [],
  boundary: '真实Gateway／Backend及原Command／QuickInput；注释预备也经原来源API，模型回放仅供启动且要求零调用' };
const responseReads = [], responseErrors = [];
const checkpoint = async phase => { result.phase = phase; await writeFile(output, JSON.stringify(result, null, 2) + '\n'); };
let page, failure;
try {
  const browser = await chromium.launch({ executablePath: required('PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH'),
    headless: true, args: ['--no-sandbox', '--disable-dev-shm-usage'] });
  run.addCleanup('target-settings-browser', () => browser.close());
  const context = await browser.newContext({ viewport: { width: 1800, height: 1100 } });
  run.addCleanup('target-settings-context', () => context.close());
  page = await context.newPage();
  page.on('pageerror', error => result.pageErrors.push(String(error)));
  page.on('request', request => {
    const url = new URL(request.url()), headers = request.headers();
    const item = { phase: result.phase, path: url.pathname, method: request.method(), params: Object.fromEntries(url.searchParams),
      owner: { gateway_id: headers['x-boxteam-target-gateway-id'], workspace_id: headers['x-boxteam-workspace-id'] },
      body: request.postData() ? JSON.parse(request.postData()) : undefined };
    if (url.pathname.startsWith('/api/v1/config/')) result.requests.push(item);
    if (url.pathname === '/api/gateway/management/gateways' || url.pathname === '/api/gateway/extension-targets') result.managementRequests.push(item);
    if (request.method() !== 'GET' && /\/(messages|jobs)(\/|$)/.test(url.pathname)) result.forbiddenWrites.push(item);
  });
  page.on('requestfinished', request => {
    if (!new URL(request.url()).pathname.startsWith('/api/v1/config/')) return;
    const phase = result.phase;
    responseReads.push((async () => {
      const response = await request.response(), body = await response.json();
      result.receipts.push({ phase, path: new URL(request.url()).pathname, method: request.method(), status: response.status(),
        header: response.headers()['x-request-id'], request_id: body.request_id, data: body.data });
    })().catch(error => responseErrors.push(String(error))));
  });
  await page.goto(required('BOXTEAM_BROWSER_BASE_URL'), { waitUntil: 'domcontentloaded' });
  await expect(page.locator('.native-session-navigation')).toBeVisible({ timeout: 30_000 });
  const api = async (path, owner, method = 'GET', data) => {
    const receipt = await page.evaluate(async ({ path, owner, method, data }) => {
      const credential = await (await fetch('/api/gateway/auth/local-credential')).json();
      const response = await fetch(path, { method, body: data === undefined ? undefined : JSON.stringify(data), headers: {
        'X-Local-Token': credential.data.token, 'Content-Type': 'application/json',
        ...(owner ? { 'X-BoxTeam-Workspace-Id': owner.workspace_id, 'X-BoxTeam-Target-Gateway-Id': owner.gateway_id } : {}),
      } });
      return { path, method, status: response.status, header: response.headers.get('X-Request-ID'), body: await response.json() };
    }, { path, owner, method, data });
    result.apiReceipts.push({ phase: result.phase, ...receipt });
    expect(receipt.status, JSON.stringify(receipt)).toBe(200);
    expect(receipt.body.request_id).toBeTruthy(); expect(receipt.body.request_id).toBe(receipt.header);
    return receipt.body.data;
  };
  const command = (id, ...args) => page.evaluate(({ id, args }) => globalThis.workbench.commands.executeCommand(id, ...args), { id, args });
  const health = await api('/api/gateway/health'), initial = await api('/api/gateway/workspaces');
  const ownerA = initial.active_workspace, metadata = await api('/api/v1/workspace');
  expect(ownerA.gateway_id).toBe(health.gateway_id); expect(metadata.workspace_id).toBe(ownerA.workspace_id);
  await api('/api/gateway/managed-workspaces', undefined, 'POST', { root_path: required('BOXTEAM_SETTINGS_OTHER_WORKSPACE'), name: 'Target Settings B' });
  const registered = await api('/api/gateway/workspaces?check_health=false');
  const b = registered.items.find(item => item.name === 'Target Settings B');
  expect(b?.status).toBe('ready');
  const ownerB = { gateway_id: b.gateway_id, workspace_id: b.workspace_id };
  result.owners = { a: ownerA, b: ownerB };
  await api(`/api/gateway/workspaces/${ownerA.workspace_id}/activate`, ownerA, 'POST', {});
  await checkpoint('prepare-source');
  const before = await api(sourcePath, ownerA);
  const preparation = { ...ownerA, source_key: sourceKey, jsonc: `// 原来源注释：经来源 API 保存\n${before.jsonc}`,
    base_file_digest: before.file_digest, base_layer_revision: before.layer_revision, base_layer_digest: before.layer_digest,
    expected_active_revision: before.active_revision, expected_active_digest: before.active_digest, idempotency_key: crypto.randomUUID() };
  await api(`${sourcePath}/preflight`, ownerA, 'POST', preparation);
  result.before = await api(sourcePath, ownerA, 'PUT', preparation);
  expect(result.before.effective.agents.reviewer).toBeTruthy();
  await page.reload({ waitUntil: 'domcontentloaded' });
  await expect(page.getByRole('button', { name: '刷新资源树', exact: true })).toBeEnabled({ timeout: 30_000 });
  const quick = page.locator('.quick-input-widget'), input = quick.locator('input').first();
  const choose = async text => { const row = quick.locator('.monaco-list-row').filter({ hasText: text }); await expect(row).toHaveCount(1); await row.click(); };
  await checkpoint('extensions-title-target');
  await command('workbench.view.extensions');
  const targetAction = page.locator('.monaco-action-bar .action-label').filter({ hasText: '目标：本地' });
  await expect(targetAction).toHaveCount(1);
  const directoryResponse = page.waitForResponse(response => new URL(response.url()).pathname === '/api/gateway/management/gateways');
  await targetAction.click();
  await expect(quick).toContainText('扩展管理目标');
  const domainsResponse = page.waitForResponse(response => new URL(response.url()).pathname === '/api/gateway/extension-targets' && response.request().method() === 'POST');
  await choose('本地');
  for (const response of await Promise.all([directoryResponse, domainsResponse])) {
    const body = await response.json();
    expect(response.status()).toBe(200); expect(body.request_id).toBe(response.headers()['x-request-id']);
    result.apiReceipts.push({ phase: result.phase, path: new URL(response.url()).pathname, body });
  }
  await expect(quick).toBeHidden();
  await expect(targetAction).toBeVisible();
  expect(result.managementRequests.map(item => item.path)).toEqual(['/api/gateway/management/gateways', '/api/gateway/extension-targets']);
  expect(result.managementRequests[1].body).toEqual({ protocol_version: 1, gateway_id: ownerA.gateway_id, gateway_connection_id: null });
  expect(result.requests.filter(item => item.phase === 'extensions-title-target')).toEqual([]);
  result.extensionTargetSelectedFromTitle = true;
  const open = async (source = sourceKey, palette = false) => {
    if (palette) {
      await command('workbench.action.showCommands');
      await input.fill('>编辑目标工作区默认 Agent 来源');
      await choose('编辑目标工作区默认 Agent 来源');
    } else {
      await page.evaluate(() => {
        globalThis.targetSettingsRun = { state: 'running' };
        globalThis.workbench.commands.executeCommand('boxteam.settings.defaultAgentSource').then(
          () => { globalThis.targetSettingsRun.state = 'complete'; }, error => { globalThis.targetSettingsRun = { state: 'error', error: String(error) }; });
      });
    }
    await expect(quick).toContainText('选择业务配置目标');
    await choose(ownerA.workspace_id);
    await expect(quick).toContainText('选择默认 Agent 配置来源');
    await choose(source);
    await expect(quick).toContainText('目标工作区默认 Agent 来源');
    await expect(quick).toContainText(ownerA.workspace_id);
    await expect(quick).toContainText(ownerA.gateway_id);
    await expect(quick).toContainText(source);
  };
  const close = async (palette = false) => {
    await page.keyboard.press('Escape');
    await expect(quick).toBeHidden();
    if (!palette) await expect.poll(() => page.evaluate(() => globalThis.targetSettingsRun)).toEqual({ state: 'complete' });
  };
  const phaseWrites = phase => result.requests.filter(item => item.phase === phase && item.method !== 'GET');

  await checkpoint('cancel');
  await open(sourceKey, true);
  await choose('设置本层 default_agent');
  await expect(quick).toContainText('设置本层 default_agent');
  await input.fill('reviewer'); await page.keyboard.press('Escape');
  await expect(quick).toContainText('目标工作区默认 Agent 来源');
  await close(true);
  expect(phaseWrites('cancel')).toEqual([]);
  expect((await api(sourcePath, ownerA)).jsonc).toBe(result.before.jsonc);
  result.cancelledWithoutWrite = true;

  await checkpoint('set');
  await open();
  await choose('设置本层 default_agent');
  await input.fill('reviewer'); await page.keyboard.press('Enter');
  await expect(quick).toContainText('确认默认 Agent 来源修改');
  await expect(quick).toContainText('候选 default_agent="reviewer"');
  expect(phaseWrites('set').map(item => item.method)).toEqual(['POST']);
  await api(`/api/gateway/workspaces/${ownerB.workspace_id}/activate`, ownerB, 'POST', {});
  await expect(quick).toContainText(ownerA.workspace_id);
  await choose('确认保存本层覆盖');
  await expect(quick).toContainText('目标工作区默认 Agent 来源');
  await expect(quick).toContainText('存在未生效变更，待重启');
  await choose('查看默认 Agent 来源与配置状态');
  await expect(quick.locator('.monaco-list-row').filter({ hasText: '运行中 default_agent' })).toContainText('"default"');
  await expect(quick.locator('.monaco-list-row').filter({ hasText: '来源合并 default_agent' })).toContainText('"reviewer"');
  await expect(quick).toContainText('source_saved=true · layer_imported=true');
  await page.keyboard.press('Escape');
  await expect(quick).toContainText('目标工作区默认 Agent 来源');
  await close();
  const setWrites = phaseWrites('set');
  expect(setWrites.map(item => item.method)).toEqual(['POST', 'PUT']);
  expect(setWrites[0].body).toEqual(setWrites[1].body);
  expect(setWrites.every(item => JSON.stringify(item.owner) === JSON.stringify(ownerA))).toBe(true);
  result.afterSet = await api(`${sourcePath}?${new URLSearchParams({ idempotency_key: setWrites[1].body.idempotency_key })}`, ownerA);
  expect(result.afterSet.effective.default_agent).toBe('default');
  expect(result.afterSet.source_effective.default_agent).toBe('reviewer');
  expect(result.afterSet.active_revision).toBe(result.before.active_revision);
  expect(result.afterSet.status).toMatchObject({ state: 'pending_restart', restart_required: true, applied_paths: [], deferred_paths: ['/default_agent'] });
  expect(result.afterSet.edit).toMatchObject({ source_saved: true, layer_imported: true, idempotency_key: setWrites[1].body.idempotency_key });
  expect(result.afterSet.jsonc).toBe(result.before.jsonc.replace('"default_agent": "default"', '"default_agent": "reviewer"'));
  expect((await api(sourcePath, ownerB)).effective.default_agent).toBe('default');
  expect((await api('/api/gateway/workspaces?check_health=false')).active_workspace).toEqual(ownerB);
  result.savedToFrozenOwner = true;
  result.savedPendingShown = true;

  await checkpoint('readonly');
  const sources = await api('/api/v1/config/sources', ownerA), inline = sources.sources.find(item => item.layer === 'inline');
  expect(inline?.source_key).toBeTruthy();
  await open(inline.source_key);
  await expect(quick).toContainText('只读');
  await expect(quick.locator('.monaco-list-row').filter({ hasText: '设置本层 default_agent' })).toHaveCount(0);
  await expect(quick.locator('.monaco-list-row').filter({ hasText: '删除本层 default_agent 覆盖' })).toHaveCount(0);
  await choose('查看默认 Agent 来源与配置状态');
  await expect(quick).toContainText('运行中 default_agent');
  await expect(quick).toContainText('所选来源覆盖');
  await page.keyboard.press('Escape');
  await expect(quick).toContainText('目标工作区默认 Agent 来源');
  await close();
  expect(phaseWrites('readonly')).toEqual([]);
  result.readonlyWithoutWrite = true;

  await checkpoint('discard');
  await open();
  await choose('丢弃原 pending 候选');
  await expect(quick).toContainText('确认原 pending 操作');
  await expect(quick).toContainText(result.afterSet.status.candidate_ref);
  await choose('丢弃原 pending 候选');
  await expect(quick).toContainText('目标工作区默认 Agent 来源');
  await close();
  const [discard] = phaseWrites('discard');
  expect(phaseWrites('discard')).toHaveLength(1);
  expect(discard).toMatchObject({ path: '/api/v1/config/pending/discard', owner: ownerA,
    params: { candidate_ref: result.afterSet.status.candidate_ref },
    body: { expected_active_revision: result.afterSet.active_revision, expected_active_digest: result.afterSet.active_digest } });
  result.afterDiscard = await api(sourcePath, ownerA);
  expect(result.afterDiscard.status.state).toBe('discarded');
  expect(result.afterDiscard.effective.default_agent).toBe('default');
  result.pendingDiscarded = true;

  await checkpoint('delete');
  await open();
  await choose('删除本层 default_agent 覆盖');
  await expect(quick).toContainText('确认默认 Agent 来源修改');
  await expect(quick).toContainText('删除所选来源的 default_agent 覆盖');
  await choose('确认保存本层覆盖');
  await expect(quick).toContainText('目标工作区默认 Agent 来源');
  await close();
  const deleteWrites = phaseWrites('delete');
  expect(deleteWrites.map(item => item.method)).toEqual(['POST', 'PUT']);
  expect(deleteWrites[0].body).toEqual(deleteWrites[1].body);
  result.afterDelete = await api(sourcePath, ownerA);
  expect(result.afterDelete.effective.default_agent).toBe('default');
  expect(result.afterDelete.layers.find(item => item.source_key === sourceKey).payload.default_agent).toBeUndefined();
  expect(result.afterDelete.origins.find(item => item.path === '/default_agent').source_key).not.toBe(sourceKey);
  expect(result.afterDelete.jsonc).toContain('// 原来源注释：经来源 API 保存\n');
  expect((await api('/api/gateway/workspaces?check_health=false')).active_workspace).toEqual(ownerB);
  result.deletedSourceOverride = true;
  await Promise.all(responseReads);
  expect(responseErrors).toEqual([]);
  for (const receipt of result.receipts) {
    expect(receipt.status).toBe(200); expect(receipt.request_id).toBeTruthy(); expect(receipt.request_id).toBe(receipt.header);
    if (receipt.method === 'PUT' && ['set', 'delete'].includes(receipt.phase)) {
      expect(receipt.data.edit.source_saved).toBe(true); expect(receipt.data.edit.layer_imported).toBe(true);
      expect(receipt.data.status.restart_required).toBe(receipt.phase === 'set');
    }
  }
  expect(result.forbiddenWrites).toEqual([]); expect(result.pageErrors).toEqual([]);
  result.phase = 'complete';
} catch (error) { failure = error; }
finally {
  try { if (failure && page) await page.screenshot({ path: output.replace(/\.json$/, '.failure.png'), fullPage: true }); }
  catch (error) { failure = new AggregateError([failure, error], '场景与截图均失败'); }
  if (failure) result.error = String(failure.stack ?? failure);
  await writeFile(output, JSON.stringify(result, null, 2) + '\n');
  await run.close();
}
if (failure) throw failure;
console.log(JSON.stringify({ phase: result.phase, output }));
