import assert from 'node:assert/strict';
import { writeFile } from 'node:fs/promises';
import { chromium } from 'playwright';
import { expect } from 'playwright/test';

const input = JSON.parse(process.env.BOXTEAM_SOURCE_OWNERSHIP_FIXTURE);
assert(input.artifacts && input.resultPath, '必须显式设置正式产物目录');
const result = { phase: 'initialization', pageErrors: [], requests: [], receipts: [], cleanupErrors: [], responseErrors: [] };
let browser;
const contexts = [];
const pages = [];
const receiptTasks = [];
try {
  browser = await chromium.launch({ executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH, headless: true });
  const command = (page, id, ...args) => page.evaluate(async ({ id, args }) => {
    await globalThis.workbench.commands.executeCommand(id, ...args);
  }, { id, args });
  const api = (page, path, owner) => page.evaluate(async ({ path, owner }) => {
    const credential = (await (await fetch('/api/gateway/auth/local-credential')).json()).data.token;
    const response = await fetch(path, { headers: { 'X-Local-Token': credential,
      'X-BoxTeam-Target-Gateway-Id': owner.gatewayId, 'X-BoxTeam-Workspace-Id': owner.workspaceId,
      ...(owner.connectionId ? { 'X-BoxTeam-Gateway-Connection-Id': owner.connectionId } : {}) } });
    const body = await response.json();
    if (!response.ok || body.code !== 0 || response.headers.get('X-Request-ID') !== body.request_id) throw new Error(JSON.stringify(body));
    return body.data;
  }, { path, owner });
  const ownerA = { gatewayId: input.gatewayA, workspaceId: input.workspaceId };
  const ownerB = { gatewayId: input.gatewayB, workspaceId: input.workspaceId, connectionId: input.connectionId };
  const text = page => page.locator('.editor-instance:visible .monaco-editor .view-lines').last();
  const tab = page => page.locator('.tabs-container .tab.active:visible').last();
  const modal = page => page.locator('.monaco-dialog-box[role="dialog"]');
  const replace = async (page, content) => {
    await page.bringToFront();
    await text(page).click(); await page.keyboard.press('Control+Home'); await page.keyboard.press('Control+A');
    await page.evaluate(value => navigator.clipboard.writeText(value), content);
    await page.keyboard.press('Control+V');
    await expect(tab(page)).toHaveClass(/\bdirty\b/);
  };
  const copyDraft = async page => {
    await page.bringToFront();
    await text(page).click(); await page.keyboard.press('Control+A'); await page.keyboard.press('Control+C');
    return page.evaluate(() => navigator.clipboard.readText());
  };
  const save = async page => {
    const pending = command(page, 'workbench.action.files.save');
    await expect(modal(page)).toContainText('保存目标配置来源');
    await modal(page).getByRole('button', { name: '保存来源', exact: true }).click();
    await pending;
    await expect(tab(page)).not.toHaveClass(/\bdirty\b/);
  };
  const openSource = async (page, sourceKey) => {
    const quick = page.locator('.quick-input-widget');
    const opening = command(page, 'boxteam.settings.openSource');
    await expect(quick).toBeVisible();
    await quick.locator('.monaco-list-row').filter({ hasText: input.workspaceId }).click();
    await expect(quick.locator('.monaco-list-row').filter({ hasText: sourceKey })).toBeVisible();
    await quick.locator('.monaco-list-row').filter({ hasText: sourceKey }).click();
    await opening;
    await expect(text(page)).toBeVisible();
    await expect(tab(page)).toContainText(`${sourceKey}.jsonc`);
  };
  const preparePage = async (label, remote) => {
    const context = await browser.newContext({ viewport: { width: 1500, height: 950 }, permissions: ['clipboard-read', 'clipboard-write'] });
    contexts.push(context);
    const page = await context.newPage(); pages.push(page); page.setDefaultTimeout(20_000);
    page.on('pageerror', error => result.pageErrors.push({ page: label, message: error.message }));
    page.on('request', request => {
      const url = new URL(request.url());
      if (!url.pathname.includes('/config/sources')) return;
      result.requests.push({ page: label, method: request.method(), path: url.pathname,
        headers: { gateway_id: request.headers()['x-boxteam-target-gateway-id'], workspace_id: request.headers()['x-boxteam-workspace-id'],
          connection_id: request.headers()['x-boxteam-gateway-connection-id'] },
        body: request.postData() ? JSON.parse(request.postData()) : null });
    });
    page.on('response', response => {
      if (!new URL(response.url()).pathname.includes('/config/sources')) return;
      receiptTasks.push((async () => {
        const body = await response.json();
        assert.equal(body.request_id, response.headers()['x-request-id']);
        result.receipts.push({ page: label, method: response.request().method(), path: new URL(response.url()).pathname,
          status: response.status(), request_id: response.headers()['x-request-id'], body });
      })().catch(error => result.responseErrors.push(String(error))));
    });
    await page.goto(input.baseUrl, { waitUntil: 'domcontentloaded' });
    await expect(page.locator('#workbench')).toBeVisible();
    await page.waitForFunction(() => !!globalThis.workbench?.commands?.executeCommand);
    await api(page, '/api/gateway/health', ownerA);
    await api(page, '/api/v1/workspace', ownerA);
    await expect(page.getByTestId(`workspace-node-${JSON.stringify([input.gatewayA, input.workspaceId])}`)).toBeVisible({ timeout: 30_000 });
    await command(page, 'boxteam.settings.agents');
    const settings = page.locator('.settings-editor:visible');
    await expect(settings.locator('.settings-context-label')).toContainText(input.gatewayA);
    if (remote) {
      await settings.getByRole('combobox', { name: '选择业务配置目标', exact: true }).selectOption({ label: input.remoteName });
      await expect(settings.locator('.settings-context-label')).toContainText(input.gatewayB);
    }
    return page;
  };
  const pageB = await preparePage('B', true);
  const rootPath = '/api/v1/config/sources/workspace_root_mutable_override';
  const localBefore = await api(pageB, rootPath, ownerA);
  const remoteBefore = await api(pageB, rootPath, ownerB);
  result.phase = 'remote-draft-after-navigation';
  // 先真实激活 B，再编辑绑定 B 的来源；随后导航和设置焦点都回到 A。
  const node = owner => pageB.getByTestId(`workspace-node-${JSON.stringify([owner.gatewayId, owner.workspaceId])}`);
  await node(ownerB).locator('button.workspace-label').click();
  await expect.poll(async () => (await api(pageB, '/api/gateway/workspaces', ownerA)).active_workspace.gateway_id).toBe(input.gatewayB);
  await openSource(pageB, 'workspace_root_mutable_override');
  const remoteDraft = remoteBefore.jsonc + '\n// 远端 B 草稿切换导航后保存\n';
  await replace(pageB, remoteDraft);
  await node(ownerA).locator('button.workspace-label').click();
  await expect.poll(async () => (await api(pageB, '/api/gateway/workspaces', ownerA)).active_workspace.gateway_id).toBe(input.gatewayA);
  await command(pageB, 'boxteam.settings.agents');
  await pageB.locator('.settings-editor:visible').getByRole('combobox', { name: '选择业务配置目标', exact: true }).selectOption({ label: '本地' });
  await expect(pageB.locator('.settings-editor:visible .settings-context-label')).toContainText(input.gatewayA);
  await pageB.locator('.tabs-container .tab:visible').filter({ hasText: 'workspace_root_mutable_override.jsonc' }).click();
  const beforeRemoteSave = result.requests.length;
  await save(pageB);
  const remoteWrite = result.requests.slice(beforeRemoteSave).find(item => item.method === 'PUT');
  assert(remoteWrite, '必须观察真实来源 PUT');
  assert.equal(remoteWrite.body.gateway_id, input.gatewayB); assert.equal(remoteWrite.headers.gateway_id, input.gatewayB);
  assert.equal(remoteWrite.body.workspace_id, input.workspaceId); assert.equal(remoteWrite.headers.workspace_id, input.workspaceId);
  assert.equal(remoteWrite.headers.connection_id, input.connectionId);
  assert.equal((await api(pageB, rootPath, ownerB)).jsonc, remoteDraft);
  assert.equal((await api(pageB, rootPath, ownerA)).jsonc, localBefore.jsonc);
  result.remoteOwner = { gateway_id: remoteWrite.body.gateway_id, workspace_id: remoteWrite.body.workspace_id,
    connection_id: remoteWrite.headers.connection_id,
    navigation_gateway_id: (await api(pageB, '/api/gateway/workspaces', ownerA)).active_workspace.gateway_id,
    saved_jsonc: remoteDraft, local_unchanged: true };

  result.phase = 'two-workspace-shared-source-drafts';
  const pageA = await preparePage('A', false);
  // B 的目标选择刚切回本地；通过原 Settings 选择器重新绑定远端。
  await command(pageB, 'boxteam.settings.agents');
  await pageB.locator('.settings-editor:visible').getByRole('combobox', { name: '选择业务配置目标', exact: true }).selectOption({ label: input.remoteName });
  await expect(pageB.locator('.settings-editor:visible .settings-context-label')).toContainText(input.gatewayB);
  const directory = await api(pageA, '/api/v1/config/sources', ownerA);
  const [shared] = directory.sources.filter(item => item.layer === 'source');
  assert(shared?.source_key, JSON.stringify(directory));
  const sharedPath = `/api/v1/config/sources/${encodeURIComponent(shared.source_key)}`;
  await openSource(pageA, shared.source_key);
  await openSource(pageB, shared.source_key);
  const baselineA = await api(pageA, sharedPath, ownerA);
  const baselineB = await api(pageB, sharedPath, ownerB);
  assert.equal(baselineA.file_digest, baselineB.file_digest);
  assert.equal(baselineA.jsonc, baselineB.jsonc);
  const draftA = baselineA.jsonc + '\n// Workspace A 已提交的共享来源\n';
  const draftB = baselineB.jsonc + '\n// Workspace B 旧基线草稿必须完整保留\n';
  await replace(pageA, draftA); await replace(pageB, draftB);
  await save(pageA);
  assert.equal((await api(pageA, sharedPath, ownerA)).jsonc, draftA);
  result.phase = 'stale-shared-source-save';
  const writesBeforeConflict = result.requests.filter(item => item.page === 'B' && item.method === 'PUT').length;
  const rejection = pageB.waitForResponse(response => response.request().method() === 'POST'
    && new URL(response.url()).pathname === sharedPath + '/preflight' && response.status() === 409);
  await command(pageB, 'workbench.action.files.save');
  const rejected = await rejection;
  const error = await rejected.json();
  assert.equal(error.request_id, rejected.headers()['x-request-id']);
  await expect(pageB.locator('.notification-list-item-message').filter({ hasText: '草稿已保留' })).toBeVisible();
  await expect(tab(pageB)).toHaveClass(/\bdirty\b/);
  assert.equal(await copyDraft(pageB), draftB);
  assert.equal(result.requests.filter(item => item.page === 'B' && item.method === 'PUT').length, writesBeforeConflict);
  const rejectedIntent = rejected.request().postDataJSON();
  assert.equal(rejectedIntent.base_file_digest, baselineB.file_digest);
  assert.equal(rejectedIntent.jsonc, draftB);
  const latest = await api(pageB, sharedPath, ownerB);
  assert.equal(latest.jsonc, draftA); assert.notEqual(latest.file_digest, baselineB.file_digest);
  result.conflict = { status: rejected.status(), request_id: error.request_id, source_key: shared.source_key,
    original_file_digest: baselineB.file_digest, current_file_digest: latest.file_digest,
    draft_preserved: true, draft_jsonc: draftB, committed_jsonc: latest.jsonc, rejected_intent: rejectedIntent };
  await Promise.all(receiptTasks);
  assert.deepEqual(result.responseErrors, []);
  result.phase = 'complete';
} catch (error) {
  result.error = error instanceof Error ? error.stack : String(error);
} finally {
  if (result.phase !== 'complete') for (let i = 0; i < pages.length; i++) {
    try {
      result.notifications ??= [];
      result.notifications.push(await pages[i].locator('.notification-list-item-message').allTextContents());
      await pages[i].screenshot({ path: `${input.artifacts}/source-owner-failure-${i}.png`, fullPage: true });
    }
    catch (error) { result.screenshotError = String(error); }
  }
  await Promise.all(receiptTasks);
  for (const context of contexts.reverse()) {
    try { await context.close(); } catch (error) { result.cleanupErrors.push(String(error)); }
  }
  if (browser) {
    try { await browser.close(); } catch (error) { result.cleanupErrors.push(String(error)); }
  }
  await writeFile(input.resultPath, JSON.stringify(result, null, 2) + '\n');
}
if (result.error || result.cleanupErrors.length) throw new Error(JSON.stringify({ error: result.error, cleanupErrors: result.cleanupErrors }));
