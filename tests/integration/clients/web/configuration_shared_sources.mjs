import assert from 'node:assert/strict';
import { writeFile } from 'node:fs/promises';
import path from 'node:path';
import { applyEdits, modify, parse } from 'jsonc-parser';
import { chromium } from 'playwright';
import { expect } from 'playwright/test';

const fixture = JSON.parse(process.env.BOXTEAM_SHARED_SOURCE_FIXTURE);
const artifacts = process.env.BOXTEAM_BROWSER_ARTIFACTS;
assert(artifacts, '必须设置正式测试产物目录');
const result = { phase: 'initialization', pageErrors: [], modelRequests: [], writes: [], initialization: [] };
let browser, page;
try {
  browser = await chromium.launch({ executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH, headless: true });
  page = await browser.newPage({ viewport: { width: 1600, height: 1000 } });
  page.setDefaultTimeout(15000);
  await page.context().grantPermissions(['clipboard-read', 'clipboard-write']);
  page.on('pageerror', error => result.pageErrors.push(error.message));
  page.on('request', request => {
    const url = new URL(request.url());
    if (/\/messages(?:$|\?)|\/chat\/completions|\/responses(?:$|\?)/.test(url.pathname)) result.modelRequests.push(url.pathname);
    if (request.method() !== 'PUT' || !url.pathname.includes('/config/sources/')) return;
    const body = request.postDataJSON(), headers = request.headers();
    result.writes.push({ path: url.pathname, gatewayId: headers['x-boxteam-target-gateway-id'],
      workspaceId: headers['x-boxteam-workspace-id'], sourceKey: body.source_key, key: body.idempotency_key });
  });
  const command = (id, ...args) => page.evaluate(({ id, args }) => globalThis.workbench.commands.executeCommand(id, ...args), { id, args });
  const api = (url, owner, init = {}) => page.evaluate(async ({ url, owner, init }) => {
    const credential = (await (await fetch('/api/gateway/auth/local-credential')).json()).data;
    const response = await fetch(url, { ...init, headers: { 'Content-Type': 'application/json', 'X-Local-Token': credential.token,
      'X-BoxTeam-Target-Gateway-Id': owner.gatewayId, 'X-BoxTeam-Workspace-Id': owner.workspaceId } });
    const body = await response.json();
    if (!response.ok || body.request_id !== response.headers.get('X-Request-ID')) throw new Error(JSON.stringify(body));
    return { data: body.data, requestId: body.request_id };
  }, { url, owner, init });
  const sourceUrl = key => '/api/v1/config/sources/' + key;
  const sharedKey = 'workspace_mutable_override', privateKey = 'workspace_root_mutable_override';
  const source = async (owner, key = sharedKey) => (await api(sourceUrl(key), owner)).data;
  const location = (owner, key) => ({ version: 2, gateway_id: owner.gatewayId, workspace_id: owner.workspaceId,
    source_key: key, name: owner.workspaceId });
  const modal = page.locator('.monaco-dialog-box[role="dialog"]');
  const groups = page.locator('.editor-group-container');
  const text = index => groups.nth(index).locator('.editor-instance:visible .monaco-editor .view-lines').last();
  const tab = index => groups.nth(index).locator('.tabs-container .tab.active');
  const focus = index => command(index === 0 ? 'workbench.action.focusFirstEditorGroup' : 'workbench.action.focusSecondEditorGroup');
  const open = async (owner, key, side = false) => {
    await command('boxteam.settings.openSource', { location: location(owner, key), openToSide: side });
    await expect(page.locator('.editor-group-container.active .tab.active')).toContainText(key + '.jsonc');
  };
  const replace = async (index, value) => {
    await focus(index); await text(index).click(); await page.keyboard.press('Control+A');
    await page.evaluate(value => navigator.clipboard.writeText(value), value); await page.keyboard.press('Control+V');
    await expect(tab(index)).toHaveClass(/\bdirty\b/);
  };
  const save = async (index, owner, key) => {
    await focus(index);
    const saved = command('workbench.action.files.save');
    await expect(modal).toContainText('保存目标配置来源');
    await expect(modal).toContainText(owner.workspaceId);
    if (key === sharedKey) {
      await expect(modal).toContainText('同一 Home');
      await expect(modal).toContainText('并非完整名单');
      await expect(modal).toContainText('未知');
    }
    const response = page.waitForResponse(response => response.request().method() === 'PUT'
      && new URL(response.url()).pathname === sourceUrl(key)
      && response.request().headers()['x-boxteam-workspace-id'] === owner.workspaceId);
    await modal.getByRole('button', { name: '保存来源', exact: true }).click();
    const received = await response, body = await received.json();
    assert.equal(received.status(), 200, JSON.stringify(body));
    assert.equal(body.request_id, received.headers()['x-request-id']);
    assert(body.data.edit.source_saved);
    assert.equal(body.data.workspace_id, owner.workspaceId);
    await saved; await expect(tab(index)).not.toHaveClass(/\bdirty\b/);
    return body.data;
  };
  const update = (jsonc, field, value) => applyEdits(jsonc, modify(jsonc, field, value,
    { formattingOptions: { insertSpaces: true, tabSize: 2 } }));
  await page.goto(fixture.baseUrl, { waitUntil: 'domcontentloaded' });
  await expect(page.locator('#workbench .monaco-workbench')).toBeVisible({ timeout: 30000 });
  await page.waitForFunction(() => !!globalThis.workbench?.commands?.executeCommand);
  for (const url of ['/api/gateway/health', '/api/gateway/workspaces', '/api/v1/workspace']) result.initialization.push(await api(url, fixture));
  assert.equal(result.initialization[0].data.gateway_id, fixture.gatewayId);
  assert.equal(result.initialization[1].data.active_workspace.workspace_id, fixture.workspaceId);
  assert.equal(result.initialization[2].data.workspace_id, fixture.workspaceId);
  await expect(page.getByTestId(`workspace-node-${JSON.stringify([fixture.gatewayId, fixture.workspaceId])}`)).toBeVisible();
  const registered = await api('/api/gateway/managed-workspaces', fixture, { method: 'POST', body: JSON.stringify({
    root_path: fixture.siblingWorkspace, name: '共享来源消费者 B', create_directory: false,
  }) });
  const sibling = registered.data.items.find(item => item.name === '共享来源消费者 B');
  assert(sibling && sibling.workspace_id !== fixture.workspaceId);
  const other = { gatewayId: fixture.gatewayId, workspaceId: sibling.workspace_id };
  assert.equal((await api('/api/v1/workspace', other)).data.workspace_id, other.workspaceId);
  result.owners = { a: { gatewayId: fixture.gatewayId, workspaceId: fixture.workspaceId }, b: other };

  result.phase = 'private-override-via-product';
  const privateBefore = await source(fixture, privateKey);
  await open(fixture, privateKey);
  await replace(0, update(privateBefore.jsonc, ['ui', 'auto_summarize'], true));
  await save(0, fixture, privateKey);
  assert.equal((await source(fixture, privateKey)).effective.ui.auto_summarize, true);

  result.phase = 'two-shared-source-drafts';
  const beforeA = await source(fixture), beforeB = await source(other);
  assert.equal(beforeA.file_digest, beforeB.file_digest);
  await open(fixture, sharedKey);
  await open(other, sharedKey, true);
  await expect(groups).toHaveCount(2);
  const marker = '// B 独立的尚未保存草稿';
  await replace(1, beforeB.jsonc + '\n' + marker + '\n');
  await replace(0, update(beforeA.jsonc, ['ui', 'auto_summarize'], false));
  const submitted = await save(0, fixture, sharedKey);
  await expect(tab(1)).toHaveClass(/\bdirty\b/);
  await expect(text(1)).toContainText(marker);
  // 只观察原 owner 发布的结果；不由测试替 B 导入配置或向其写 effective。
  let confirmed;
  await expect.poll(async () => {
    const a = await source(fixture), b = await source(other);
    confirmed = { a, b };
    return a.fanout.workspaces.find(item => item.workspace_id === other.workspaceId)?.result;
  }, { timeout: 30000 }).toBe('applied');
  assert.equal(confirmed.a.effective.ui.auto_summarize, true);
  assert.equal(confirmed.b.effective.ui.auto_summarize, false);
  assert.equal(confirmed.a.file_digest, confirmed.b.file_digest);
  assert.equal(confirmed.a.source_generation, submitted.source_generation);
  assert.equal(confirmed.b.source_generation, submitted.source_generation);
  assert.equal(confirmed.a.fanout.coverage, 'observed_only');
  assert.equal(confirmed.a.fanout.workspaces.find(item => item.workspace_id === fixture.workspaceId).result, 'unchanged');
  result.consumerResults = { coverage: confirmed.a.fanout.coverage, generation: submitted.source_generation,
    fanout: confirmed.a.fanout, aValue: confirmed.a.effective.ui.auto_summarize, bValue: confirmed.b.effective.ui.auto_summarize };

  result.phase = 'stale-draft-conflict';
  await focus(1);
  const beforeConflict = result.writes.length;
  const conflict = page.waitForResponse(response => response.request().method() === 'POST'
    && new URL(response.url()).pathname === sourceUrl(sharedKey) + '/preflight'
    && response.request().headers()['x-boxteam-workspace-id'] === other.workspaceId && response.status() === 409);
  await command('workbench.action.files.save');
  const failed = await conflict;
  await expect(page.locator('.notification-list-item-message').filter({ hasText: '草稿已保留' })).toBeVisible();
  await expect(tab(1)).toHaveClass(/\bdirty\b/);
  await expect(text(1)).toContainText(marker);
  assert.equal(result.writes.length, beforeConflict, '旧基线冲突必须停在预检，不发送PUT');
  const afterConflict = await source(other);
  assert.equal(afterConflict.file_digest, submitted.file_digest);
  assert(!afterConflict.jsonc.includes(marker));
  result.sharedDraftConflict = { status: failed.status(), requestId: failed.headers()['x-request-id'],
    putCountUnchanged: result.writes.length === beforeConflict };
  assert(result.sharedDraftConflict.requestId);

  result.phase = 'read-latest-with-original-editor';
  await command('workbench.action.files.revert');
  await expect(tab(1)).not.toHaveClass(/\bdirty\b/);
  await replace(1, afterConflict.jsonc + '\n// B 显式重读后保存\n');
  const bSaved = await save(1, other, sharedKey);
  assert.equal(parse(bSaved.jsonc).ui.auto_summarize, false);
  assert.equal((await api('/api/gateway/workspaces', fixture)).data.active_workspace.workspace_id, fixture.workspaceId,
    '打开另一工作区设置不能激活它');
  assert(result.writes.every(write => write.gatewayId === fixture.gatewayId
    && write.workspaceId === (write.sourceKey === privateKey ? fixture.workspaceId
      : write.key === submitted.edit.idempotency_key ? fixture.workspaceId : other.workspaceId)));
  assert.deepEqual(result.pageErrors, []);
  assert.deepEqual(result.modelRequests, []);
  result.phase = 'completed';
} catch (error) {
  result.error = error.stack ?? String(error);
  if (page) await page.screenshot({ path: path.join(artifacts, 'shared-source-failure.png'), fullPage: true });
  throw error;
} finally {
  await writeFile(path.join(artifacts, 'result.json'), JSON.stringify(result, null, 2) + '\n');
  await browser?.close();
}
