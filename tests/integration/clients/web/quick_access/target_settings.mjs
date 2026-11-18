// Integration：正式 Workbench／真实 Gateway 和来源 writer，通过 Agent 表单保存默认 Agent。
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
  boundary: '真实 Gateway／Backend、默认 Agent 命令、Agent 表单、来源 CAS writer 与 pending API；模型回放仅供启动且要求零调用' };
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
  const ownerAIdentity = { gateway_id: ownerA.gateway_id, workspace_id: ownerA.workspace_id };
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
  const form = page.locator('.configuration-entities[data-definition-kind="agents"]');
  const modal = page.locator('.monaco-dialog-box[role="dialog"]');
  const choose = async text => {
    const row = quick.locator('.monaco-list-row').filter({ hasText: text });
    await expect(row).toHaveCount(1); await row.click();
  };
  const startCommand = async () => {
    await command('workbench.action.showCommands');
    await input.fill('>编辑目标工作区默认 Agent 来源');
    await choose('编辑目标工作区默认 Agent 来源');
    await expect(quick).toContainText('选择业务配置目标');
  };
  const openAgentSource = async source => {
    await startCommand();
    await choose(ownerA.workspace_id);
    await expect(quick).toContainText('选择默认 Agent 配置来源');
    await choose(source);
    await expect(form).toBeVisible();
    await expect(form).toContainText(source);
  };
  const startStatusCommand = async location => page.evaluate(location => {
    globalThis.targetSettingsStatusRun = { state: 'running' };
    globalThis.workbench.commands.executeCommand('boxteam.settings.defaultAgentSource', location).then(
      () => { globalThis.targetSettingsStatusRun.state = 'complete'; },
      error => { globalThis.targetSettingsStatusRun = { state: 'error', error: String(error) }; });
  }, location);
  const closeStatusCommand = async () => {
    await page.keyboard.press('Escape'); await expect(quick).toBeHidden();
    await expect.poll(() => page.evaluate(() => globalThis.targetSettingsStatusRun)).toEqual({ state: 'complete' });
  };
  const phaseWrites = phase => result.requests.filter(item => item.phase === phase && item.method !== 'GET');

  await checkpoint('cancel-target');
  await startCommand();
  await page.keyboard.press('Escape');
  await expect(quick).toBeHidden();
  expect(phaseWrites('cancel-target')).toEqual([]);
  result.cancelledTargetWithoutWrite = true;

  await checkpoint('cancel-source');
  await startCommand();
  await choose(ownerA.workspace_id);
  await expect(quick).toContainText('选择默认 Agent 配置来源');
  await page.keyboard.press('Escape');
  await expect(quick).toBeHidden();
  expect(phaseWrites('cancel-source')).toEqual([]);
  result.cancelledSourceWithoutWrite = true;

  await checkpoint('open-agent-form');
  await openAgentSource(sourceKey);
  await form.locator('nav button').filter({ hasText: 'reviewer' }).click();
  const reviewer = form.locator('[data-definition-id="reviewer"]');
  await expect(reviewer).toBeVisible();
  await reviewer.getByRole('button', { name: '设为本来源的配置默认 Agent', exact: true }).click();
  await expect(page.locator('.tabs-container .tab.active')).toHaveClass(/\bdirty\b/);
  expect(phaseWrites('open-agent-form')).toEqual([]);
  result.enteredAgentFormAndEditedDraft = true;

  await checkpoint('cancel-save');
  const cancelledPreflight = page.waitForResponse(response => response.request().method() === 'POST'
    && new URL(response.url()).pathname === `${sourcePath}/preflight`);
  await form.getByRole('button', { name: '保存来源', exact: true }).click();
  await expect(modal).toContainText('保存目标配置来源');
  await modal.getByRole('button', { name: '取消', exact: true }).click();
  expect((await cancelledPreflight).status()).toBe(200);
  expect(phaseWrites('cancel-save').map(item => item.method)).toEqual(['POST']);
  result.cancelledSourceSaveWithoutPut = true;

  await checkpoint('set');
  let unknownSave, blockedQueries = 0, allowReceiptQuery = false;
  const sourceRoute = async route => {
    const request = route.request(), url = new URL(request.url());
    if (url.pathname !== sourcePath) return route.continue();
    if (request.method() === 'PUT') {
      const response = await route.fetch(), envelope = await response.json();
      expect(response.status()).toBe(200); expect(envelope.data.edit.source_saved).toBe(true);
      unknownSave = { status: response.status(), request_id: envelope.request_id, edit: envelope.data.edit, state: envelope.data.status };
      return route.abort('failed');
    }
    if (request.method() === 'GET' && url.searchParams.has('idempotency_key') && !allowReceiptQuery) {
      blockedQueries++;
      return route.abort('failed');
    }
    return route.continue();
  };
  await page.route('**/api/v1/config/sources/**', sourceRoute);
  const preflightResponse = page.waitForResponse(response => response.request().method() === 'POST'
    && new URL(response.url()).pathname === `${sourcePath}/preflight`);
  const putRequest = page.waitForRequest(request => request.method() === 'PUT' && new URL(request.url()).pathname === sourcePath);
  await form.getByRole('button', { name: '保存来源', exact: true }).click();
  await expect(modal).toContainText('保存目标配置来源');
  await api(`/api/gateway/workspaces/${ownerB.workspace_id}/activate`, ownerB, 'POST', {});
  await modal.getByRole('button', { name: '保存来源', exact: true }).click();
  expect((await preflightResponse).status()).toBe(200);
  await putRequest;
  await expect.poll(() => blockedQueries).toBeGreaterThan(0);
  await expect(form.getByRole('button', { name: '查询原提交', exact: true })).toBeVisible();
  expect(unknownSave?.edit.source_saved).toBe(true);
  expect(unknownSave?.edit.layer_imported).toBe(true);
  const setWrites = phaseWrites('set');
  expect(setWrites.map(item => item.method)).toEqual(['POST', 'PUT']);
  expect(setWrites[0].body).toEqual(setWrites[1].body);
  expect(setWrites.every(item => item.owner.gateway_id === ownerA.gateway_id && item.owner.workspace_id === ownerA.workspace_id)).toBe(true);
  const savedKey = setWrites[1].body.idempotency_key;
  expect(unknownSave.edit.idempotency_key).toBe(savedKey);

  allowReceiptQuery = true;
  const queryResponse = page.waitForResponse(response => response.request().method() === 'GET'
    && new URL(response.url()).pathname === sourcePath
    && new URL(response.url()).searchParams.get('idempotency_key') === savedKey);
  await form.getByRole('button', { name: '查询原提交', exact: true }).click();
  const queried = await queryResponse, queryEnvelope = await queried.json();
  expect(queried.status()).toBe(200);
  expect(queryEnvelope.request_id).toBe(queried.headers()['x-request-id']);
  expect(queryEnvelope.data.edit).toMatchObject({ idempotency_key: savedKey, source_saved: true, layer_imported: true });
  await expect(form.getByRole('button', { name: '查询原提交', exact: true })).toHaveCount(0);
  expect(phaseWrites('set').map(item => item.method)).toEqual(['POST', 'PUT']);
  await page.unroute('**/api/v1/config/sources/**', sourceRoute);
  result.unknownSaveResolvedByOriginalKey = savedKey;

  result.afterSet = await api(`${sourcePath}?${new URLSearchParams({ idempotency_key: savedKey })}`, ownerA);
  expect(result.afterSet.effective.default_agent).toBe('default');
  expect(result.afterSet.source_effective.default_agent).toBe('reviewer');
  expect(result.afterSet.status).toMatchObject({ state: 'pending_restart', restart_required: true, applied_paths: [], deferred_paths: ['/default_agent'] });
  expect(result.afterSet.edit).toMatchObject({ source_saved: true, layer_imported: true, idempotency_key: savedKey });
  expect(result.afterSet.jsonc).toContain('"default_agent": "reviewer"');
  expect((await api('/api/gateway/workspaces?check_health=false')).active_workspace).toEqual(ownerB);
  result.savedToFrozenOwner = true;
  result.savedPendingShown = true;

  await checkpoint('pending-actions');
  await startStatusCommand({ version: 2, ...ownerA, source_key: sourceKey, name: 'Target Settings A' });
  await expect(quick).toContainText(ownerA.workspace_id);
  await expect(quick.locator('.monaco-list-row').filter({ hasText: '重试原 pending 状态' })).toHaveCount(1);
  const discardResponse = page.waitForResponse(response => response.request().method() === 'POST'
    && new URL(response.url()).pathname === '/api/v1/config/pending/discard');
  await choose('丢弃原 pending 候选');
  await expect(quick).toContainText('确认原 pending 操作');
  await expect(quick).toContainText(result.afterSet.status.candidate_ref);
  await choose('丢弃原 pending 候选');
  expect((await discardResponse).status()).toBe(200);
  await expect(quick).toContainText(ownerA.workspace_id);
  await expect(quick.locator('.monaco-list-row').filter({ hasText: '刷新原来源' })).toHaveCount(1);
  const discardWrites = phaseWrites('pending-actions');
  expect(discardWrites).toHaveLength(1);
  expect(discardWrites[0]).toMatchObject({ path: '/api/v1/config/pending/discard', owner: ownerAIdentity,
    params: { candidate_ref: result.afterSet.status.candidate_ref },
    body: { expected_active_revision: result.afterSet.active_revision, expected_active_digest: result.afterSet.active_digest } });
  await closeStatusCommand();
  result.afterDiscard = await api(sourcePath, ownerA);
  expect(result.afterDiscard.status.state).toBe('discarded');
  expect(result.afterDiscard.effective.default_agent).toBe('default');
  expect(result.afterDiscard.source_effective.default_agent).toBe('reviewer');
  result.pendingDiscarded = true;

  await checkpoint('readonly');
  const sources = await api('/api/v1/config/sources', ownerA), inline = sources.sources.find(item => item.layer === 'inline');
  expect(inline?.source_key).toBeTruthy();
  await startCommand();
  await choose(ownerA.workspace_id);
  await expect(quick).toContainText('选择默认 Agent 配置来源');
  const sourceLabel = quick.getByText(inline.source_key, { exact: true });
  await expect(sourceLabel).toHaveCount(1);
  await sourceLabel.click();
  await expect(form).toBeVisible();
  await expect(form.locator('h2').first()).toHaveAttribute('title', `当前编辑来源：${inline.source_key}。更改通过原来源写入流程显式保存。`);
  await expect(form.getByRole('button', { name: '保存来源', exact: true })).toBeDisabled();
  await expect(form.locator('article').first().getByRole('button', { name: '设为本来源的配置默认 Agent', exact: true })).toBeDisabled();
  expect(phaseWrites('readonly')).toEqual([]);
  result.readonlyWithoutWrite = true;

  await Promise.all(responseReads);
  expect(responseErrors).toEqual([]);
  for (const receipt of result.receipts) {
    expect(receipt.status).toBe(200); expect(receipt.request_id).toBeTruthy(); expect(receipt.request_id).toBe(receipt.header);
    if (receipt.method === 'PUT' && receipt.phase === 'prepare-source') {
      expect(receipt.data.edit.source_saved).toBe(true); expect(receipt.data.edit.layer_imported).toBe(true);
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
