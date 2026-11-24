// Integration：真实插件和 Gateway 日志；只为缺失声明入口替换一次库存展示，不替换日志响应。
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { readFile, rename, truncate, unlink, writeFile } from 'node:fs/promises';
import { chromium } from 'playwright';
import { expect } from 'playwright/test';
import { selectPluginScope } from './service_plugin_controls.mjs';

const input = JSON.parse(process.env.BOXTEAM_PLUGIN_LOG_UI_FIXTURE);
assert(input.artifacts && input.resultPath, '必须显式设置正式测试产物目录');
const result = { phase: 'initialization', navigationOnly: process.env.BOXTEAM_PLUGIN_LOG_UI_NAVIGATION_ONLY === '1',
  pageErrors: [], externalRequests: [], requests: [],
  boundaries: ['SSH 执行与隧道使用原 loopback 装具', '管理视图缺失声明场景只删除真实库存响应的 packages；日志与实例保持真实'] };
result.boundaries.push('仅对本场景隔离 Home 的明确日志文件注入截断、替换和删除；diagnostics/query/locate 不使用响应替身');
let browser, page, releaseLocate;
let hideInventory = false, holdLocate = false;
let locateSeen;
const locateHeld = new Promise(resolve => { locateSeen = resolve; });
const locateReleased = new Promise(resolve => { releaseLocate = resolve; });
try {
  browser = await chromium.launch({ executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH, headless: true });
  const context = await browser.newContext({ viewport: { width: 1500, height: 1050 }, permissions: ['clipboard-read', 'clipboard-write'] });
  page = await context.newPage();
  page.setDefaultTimeout(15_000);
  page.on('pageerror', error => result.pageErrors.push(error.message));
  page.on('request', request => {
    const url = new URL(request.url());
    if (['http:', 'https:'].includes(url.protocol) && url.origin !== new URL(input.baseUrl).origin) result.externalRequests.push(request.url());
    if (url.pathname.startsWith('/api/gateway/diagnostics')) result.requests.push({ path: url.pathname,
      connection_id: url.searchParams.get('gateway_connection_id'), log_id: url.searchParams.get('log_id'),
      gateway_id: request.headers()['x-boxteam-target-gateway-id'], include_tail: url.searchParams.get('include_tail') });
  });
  await page.route('**/api/gateway/service-plugins/inventory', async route => {
    if (!hideInventory || route.request().postDataJSON().gateway_id !== input.gatewayB) return route.continue();
    const response = await route.fetch(), body = await response.json();
    body.data.packages = [];
    await route.fulfill({ response, json: body });
  });
  await page.route('**/api/gateway/diagnostics/locate?*', async route => {
    if (!holdLocate) return route.continue();
    holdLocate = false;
    // 原 owner 已产生真实定位响应；只延迟交付浏览器，以验证切目标后的旧结果废弃。
    const response = await route.fetch();
    result.heldLocationStatus = response.status();
    locateSeen();
    await locateReleased;
    await route.fulfill({ response });
    result.heldLocationDelivered = true;
  });
  await page.goto(input.baseUrl, { waitUntil: 'domcontentloaded' });
  await page.waitForFunction(() => !!globalThis.workbench?.commands?.executeCommand);
  await expect(page.locator('.monaco-workbench')).toBeVisible();
  const command = (id, ...args) => page.evaluate(({ id, args }) => globalThis.workbench.commands.executeCommand(id, ...args), { id, args });
  const api = (path, options = {}, target, expectedStatus = 200) => page.evaluate(async ({ path, options, target, expectedStatus }) => {
    const credential = await (await fetch('/api/gateway/auth/local-credential')).json();
    const response = await fetch(path, { ...options, headers: { 'X-Local-Token': credential.data.token,
      'Content-Type': 'application/json', ...(target ? { 'X-BoxTeam-Target-Gateway-Id': target } : {}) },
      body: options.body && JSON.stringify(options.body) });
    const body = await response.json();
    if (response.status !== expectedStatus || response.headers.get('X-Request-ID') !== body.request_id) throw new Error(`${path}: ${response.status} ${JSON.stringify(body)}`);
    return body;
  }, { path, options, target, expectedStatus });
  const health = await api('/api/gateway/health'), workspaces = await api('/api/gateway/workspaces'), workspace = await api('/api/v1/workspace');
  assert.equal(health.data.gateway_id, input.gatewayA);
  assert.equal(workspaces.data.active_workspace.gateway_id, input.gatewayA);
  assert.equal(workspaces.data.active_workspace.workspace_id, input.workspaceId);
  assert.equal(workspace.data.workspace_id, input.workspaceId);
  result.initialization = { gateway_id: input.gatewayA, workspace_id: input.workspaceId,
    request_ids: [health.request_id, workspaces.request_id, workspace.request_id] };
  const targetA = { gateway_id: input.gatewayA, log_id: input.slots[0].log_id };
  const targetB = { gateway_id: input.gatewayB, gateway_connection_id: input.connectionId, log_id: input.slots[1].log_id };
  assert(targetA.log_id && targetB.log_id && targetA.log_id !== targetB.log_id);
  const panel = page.locator('.part.panel'), output = panel.locator('.output-view .monaco-editor');
  const outputChannel = panel.getByRole('toolbar', { name: '输出操作', exact: true }).getByLabel('切换输出', { exact: true });
  const readResponse = target => page.waitForResponse(response => {
    const url = new URL(response.url());
    return url.pathname === '/api/gateway/diagnostics' && url.searchParams.get('log_id') === target.log_id
      && url.searchParams.get('include_tail') !== 'false' && response.request().headers()['x-boxteam-target-gateway-id'] === target.gateway_id;
  });
  const verifyRead = async (response, target) => {
    assert.equal(response.status(), 200);
    const body = await response.json(), url = new URL(response.url());
    assert.equal(response.headers()['x-request-id'], body.request_id);
    assert.equal(body.data.gateway_id, target.gateway_id);
    assert.equal(body.data.selected_log_id, target.log_id);
    if (target.gateway_connection_id) assert.equal(url.searchParams.get('gateway_connection_id'), target.gateway_connection_id);
    const log = body.data.logs.find(item => item.log_id === target.log_id);
    assert.equal(log.source, 'gateway');
    assert.equal(log.status, 'available');
    assert(log.tail.includes('Uvicorn running'));
    await expect(output).toBeVisible();
    await command('boxteam.output.focus');
    await page.keyboard.press('ControlOrMeta+End');
    const sample = log.tail.trimEnd().split('\n').at(-1).slice(0, 120).replace(/\s/g, '');
    await expect.poll(async () => (await output.locator('.view-lines').innerText()).replace(/\s/g, '')).toContain(sample);
    assert.equal(await outputChannel.inputValue(), log.label);
    return { gateway_id: body.data.gateway_id, log_id: log.log_id, request_id: body.request_id,
      label: log.label, connection_id: url.searchParams.get('gateway_connection_id'), source: log.source,
      tail_bytes: new TextEncoder().encode(log.tail).length };
  };

  if (!result.navigationOnly) {
    result.phase = 'plugin-detail-log';
    await command('workbench.view.extensions');
    const view = page.locator('.service-plugin-packages-view');
    await expect(view).toBeVisible();
    await view.getByRole('combobox', { name: '管理 Gateway', exact: true }).selectOption({ label: input.remoteName });
    await selectPluginScope(page, 'gateway');
    await view.getByRole('listitem', { name: '目标归属 Echo', exact: true }).click();
    const details = page.locator('.service-plugin-editor:visible');
    await expect(details).toBeVisible();
    await details.locator('.service-plugin-diagnostics > summary').click();
    const detailRead = readResponse(targetB);
    await details.getByRole('button', { name: '查看实例日志', exact: true }).click();
    result.detailLog = await verifyRead(await detailRead, targetB);
    assert.equal(await details.getByLabel('插件实例日志').count(), 0);

    result.phase = 'management-missing-declaration-log';
    hideInventory = true;
    await command('workbench.extensions.action.refreshExtension');
    const missing = view.locator(`[data-plugin-runtime-row][data-package-id="${input.packageId}"][data-plugin-id="${input.pluginId}"]`);
    await expect(missing.getByRole('button', { name: '查看实例日志', exact: true })).toBeVisible();
    const managementRead = readResponse(targetB);
    await missing.getByRole('button', { name: '查看实例日志', exact: true }).click();
    result.managementLog = await verifyRead(await managementRead, targetB);
    hideInventory = false;

    result.phase = 'settings-log';
    await command('boxteam.gateway.diagnostics', { gateway_id: input.gatewayB, gateway_connection_id: input.connectionId,
      name: input.remoteName, connection_kind: 'remote_gateway' });
    const diagnostics = page.getByTestId('gateway-diagnostics-panel');
    await expect(diagnostics).toBeVisible();
    const metadata = await api(`/api/gateway/diagnostics?gateway_connection_id=${input.connectionId}&include_tail=false`, {}, input.gatewayB);
    const descriptor = metadata.data.logs.find(log => log.log_id === targetB.log_id);
    assert(descriptor);
    const row = diagnostics.locator('.gateway-diagnostics-log-list > div').filter({ has: page.getByText(descriptor.label, { exact: true }) });
    const settingsRead = readResponse(targetB);
    await row.getByRole('button', { name: '在输出中查看日志', exact: true }).click();
    result.settingsLog = await verifyRead(await settingsRead, targetB);
    assert.equal(await diagnostics.locator('.gateway-diagnostics-viewer').count(), 0);

  }

  if (result.navigationOnly) {
    result.phase = 'output-readiness';
    const metadataReady = page.waitForResponse(response => {
      const url = new URL(response.url());
      return url.pathname === '/api/gateway/diagnostics' && !url.searchParams.has('log_id')
        && response.request().headers()['x-boxteam-target-gateway-id'] === input.gatewayA;
    });
    const [ready] = await Promise.all([metadataReady, command('boxteam.output.focus')]);
    const metadata = await ready.json();
    assert.equal(ready.status(), 200);
    assert.equal(ready.headers()['x-request-id'], metadata.request_id);
    assert.equal(metadata.data.gateway_id, input.gatewayA);
    await expect(output).toBeVisible();
    result.outputReadiness = { gateway_id: input.gatewayA, request_id: metadata.request_id };
  }

  result.phase = 'real-query-and-locate';
  const marker = `LOG_NAV_${input.slots[1].instance.instance_id}`;
  await api(`/api/gateway/service-plugins/${input.packageId}/${input.pluginId}/instances/${input.slots[1].instance.instance_id}/services/echo/${marker}?package_version=1.0.0`, {}, input.gatewayB);
  const scope = { source: 'gateway', workspace_ids: [] };
  const query = await api(`/api/gateway/diagnostics/query?gateway_connection_id=${input.connectionId}`,
    { method: 'POST', body: { scope, query: marker, limit: 50 } }, input.gatewayB);
  assert.equal(query.data.gateway_id, input.gatewayB);
  const match = query.data.items.find(item => item.reference.log_id === targetB.log_id);
  assert(match, '真实插件日志必须包含刚完成的业务访问记录');
  const location = { scope, reference: match.reference };
  const locatedResponse = page.waitForResponse(response => new URL(response.url()).pathname === '/api/gateway/diagnostics/locate');
  const [located] = await Promise.all([locatedResponse, command('boxteam.output.openLog', { ...targetB, location })]);
  const envelope = await located.json();
  assert.equal(located.status(), 200);
  assert.equal(located.headers()['x-request-id'], envelope.request_id);
  assert.equal(new URL(located.url()).searchParams.get('gateway_connection_id'), input.connectionId);
  assert.deepEqual(envelope.data.reference, match.reference);
  const expectedSelection = Array.from(envelope.data.text).slice(match.reference.start_codepoint, match.reference.end_codepoint).join('');
  assert.equal(expectedSelection, marker);
  await expect(output.locator('.view-lines')).toContainText(marker);
  await command('boxteam.output.focus');
  await expect(output).toHaveClass(/focused/);
  result.selectionBeforeCopy = await output.locator('textarea').evaluate(element => ({
    focused: element.getRootNode().activeElement === element, value: element.value,
    start: element.selectionStart, end: element.selectionEnd,
  }));
  await page.keyboard.press('ControlOrMeta+c');
  let copiedSelection;
  try {
    await expect.poll(async () => {
      copiedSelection = await page.evaluate(() => navigator.clipboard.readText());
      return copiedSelection;
    }).toBe(marker);
  }
  catch (error) {
    // 复制结果单独失败仍验收独立的迟到导航；整次检查保留失败退出码。
    result.copyFailure = error.message; process.exitCode = 1;
  }
  await expect(page.locator('[id="boxteam.output.status"]')).toContainText('已选中日志记录');
  await page.waitForTimeout(3400);
  await expect.poll(() => output.locator('.view-lines').innerText()).not.toContain('Uvicorn running');
  result.location = { query: marker, query_request_id: query.request_id, locate_request_id: envelope.request_id,
    reference: match.reference, copied_selection: copiedSelection ?? null, preserved_after_poll: true };

  result.phase = 'late-location-target-change';
  holdLocate = true;
  await page.evaluate(target => {
    globalThis.oldLogNavigation = globalThis.workbench.commands.executeCommand('boxteam.output.openLog', target)
      .then(() => ({ ok: true }), error => ({ ok: false, message: error.message }));
  }, { ...targetB, location });
  await locateHeld;
  const aRead = readResponse(targetA);
  const [aResponse] = await Promise.all([aRead, command('boxteam.output.openLog', targetA)]);
  result.localLog = await verifyRead(aResponse, targetA);
  releaseLocate();
  await expect.poll(() => result.heldLocationDelivered).toBe(true);
  const oldOperation = await page.evaluate(() => globalThis.oldLogNavigation);
  assert.equal(oldOperation.ok, false);
  await expect.poll(() => outputChannel.inputValue()).toBe(result.localLog.label);
  await expect.poll(() => output.locator('.view-lines').innerText()).not.toContain(marker);
  result.lateLocation = { old_operation: oldOperation, visible_gateway_id: input.gatewayA };

  result.phase = 'non-bmp-crlf-repeated-match';
  const coordinateQuery = `LOG_COORD_${input.slots[1].instance.instance_id}`;
  const coordinatePrefix = `🧭前缀 ${coordinateQuery} / 🧪 `;
  const coordinateText = coordinatePrefix + coordinateQuery;
  const echo = resource => api(`/api/gateway/service-plugins/${input.packageId}/${input.pluginId}/instances/${input.slots[1].instance.instance_id}/services/echo/${resource}?package_version=1.0.0`, {}, input.gatewayB);
  await echo(coordinateQuery);
  const coordinatePage = await api(`/api/gateway/diagnostics/query?gateway_connection_id=${input.connectionId}`,
    { method: 'POST', body: { scope, query: coordinateQuery, limit: 50 } }, input.gatewayB);
  const logBytes = await readFile(input.remoteLogPath);
  const coordinateMatches = coordinatePage.data.items.filter(item => item.reference.log_id === targetB.log_id
    && logBytes.subarray(item.reference.line_start, item.reference.line_end).toString('utf8') === `${coordinateText}\r\n`);
  assert.equal(coordinateMatches.length, 2);
  const second = coordinateMatches[1].reference;
  assert.equal(second.start_codepoint, Array.from(coordinatePrefix).length);
  assert.equal(second.end_codepoint, Array.from(coordinateText).length);
  assert.notEqual(second.start_codepoint, coordinatePrefix.length, '非 BMP 必须使码点与 UTF-16 列不同');
  assert.equal(second.content_version, createHash('sha256').update(logBytes.subarray(second.line_start, second.line_end)).digest('hex'));
  const coordinateLocation = { scope, reference: second };
  await command('boxteam.output.openLog', { ...targetB, location: coordinateLocation });
  await expect(output.locator('.view-lines')).toContainText(coordinateText);
  const copySelection = async expected => {
    await command('boxteam.output.focus');
    await page.keyboard.press('ControlOrMeta+c');
    await expect.poll(() => page.evaluate(() => navigator.clipboard.readText())).toBe(expected);
    return expected;
  };
  await copySelection(coordinateQuery);
  // 通过真实方向键展开到行首，区分同一行的两个相同命中并验证 UTF-16 选区。
  await page.keyboard.press('ArrowLeft');
  await page.keyboard.press('Shift+Home');
  await copySelection(coordinatePrefix);
  result.coordinates = { query: coordinateQuery, reference: second, physical_crlf: true,
    query_request_id: coordinatePage.request_id, copied_second_match: coordinateQuery, copied_prefix: coordinatePrefix };

  result.phase = 'bounded-query-and-explicit-candidate';
  const pageQuery = `LOG_PAGE_${input.slots[1].instance.instance_id}`;
  await echo(pageQuery);
  const quick = page.locator('.quick-input-widget');
  const queryResponse = () => page.waitForResponse(response => new URL(response.url()).pathname === '/api/gateway/diagnostics/query'
    && response.request().postDataJSON()?.query === pageQuery);
  const firstPage = queryResponse();
  await page.evaluate(target => {
    globalThis.pagedLogNavigation = globalThis.workbench.commands.executeCommand('boxteam.output.openLog', target)
      .then(() => ({ ok: true }), error => ({ ok: false, message: error.message }));
  }, { ...targetB, request_id: pageQuery });
  let response = await firstPage, pages = 0, candidates = [];
  result.queryPages = [];
  while (true) {
    const body = await response.json();
    assert.equal(response.status(), 200);
    assert.equal(response.headers()['x-request-id'], body.request_id);
    assert.equal(new URL(response.url()).searchParams.get('gateway_connection_id'), input.connectionId);
    candidates = body.data.items.filter(item => item.reference.log_id === targetB.log_id);
    result.queryPages.push({ request_id: body.request_id, complete: body.data.complete, reason: body.data.limited_reason,
      scanned_lines: body.data.scanned_lines, candidates: candidates.map(item => item.reference) });
    pages++;
    await expect(quick).toBeVisible();
    if (candidates.length) break;
    assert(pages < 5 && !body.data.complete && body.data.cursor, '空页仍须提供有界续点');
    await expect(page.locator('[id="boxteam.output.status"]')).toContainText('查询尚未完成');
    const count = result.requests.filter(item => item.path === '/api/gateway/diagnostics/query').length;
    await page.waitForTimeout(250);
    assert.equal(result.requests.filter(item => item.path === '/api/gateway/diagnostics/query').length, count, '没有用户选择不能自动续查');
    const next = queryResponse();
    await quick.locator('.monaco-list-row').filter({ hasText: '继续有界查询' }).click();
    response = await next;
  }
  assert.equal(candidates.length, 2);
  const chosen = candidates[1].reference;
  await quick.locator('.monaco-list-row').filter({ hasText: `UTF-8 ${chosen.line_start} · ${chosen.start_codepoint}` }).click();
  assert.deepEqual(await page.evaluate(() => globalThis.pagedLogNavigation), { ok: true });
  await expect(output.locator('.view-lines')).toContainText(`${pageQuery} second`);
  await copySelection(pageQuery);
  await page.keyboard.press('ArrowRight');
  await page.keyboard.press('Shift+End');
  await copySelection(' second');
  result.pagedCandidates = { pages, selected_second: true, reference: chosen };

  result.phase = 'log-body-and-status';
  const savedBytes = await readFile(input.remoteLogPath);
  const status = page.locator('[id="boxteam.output.status"]');
  await command('boxteam.output.openLog', targetB);
  await expect(status).toContainText('仅显示最新尾部');
  await expect(output.locator('.view-lines')).not.toContainText('仅显示最新尾部');
  try {
    await truncate(input.remoteLogPath, 0);
    await command('boxteam.output.openLog', targetB);
    await expect(status).toContainText('日志文件为空');
    await expect.poll(async () => (await output.locator('.view-lines').innerText()).trim()).toBe('');
  } finally { await writeFile(input.remoteLogPath, savedBytes); }
  await command('boxteam.output.openLog', targetB);
  const snapshotText = await output.locator('.view-lines').innerText();
  const movedPath = `${input.remoteLogPath}.navigation-rotation`;
  try {
    await rename(input.remoteLogPath, movedPath);
    await expect(status).toContainText('刷新失败，仍展示上次成功读取的数据', { timeout: 10_000 });
    assert.equal(await output.locator('.view-lines').innerText(), snapshotText);
    await expect(output.locator('.view-lines')).not.toContainText('刷新失败');
  } finally { await rename(movedPath, input.remoteLogPath); }
  result.bodyAndStatus = { truncated: true, empty: true, refresh_kept_body: true };

  result.phase = 'stale-reference-no-tail-fallback';
  result.staleReferences = [];
  const rejectedNavigation = target => page.evaluate(target => globalThis.workbench.commands.executeCommand('boxteam.output.openLog', target)
    .then(() => ({ ok: true }), error => ({ ok: false, message: error.message })), target);
  const checkStale = async (testCase, reason) => {
    const denied = await api(`/api/gateway/diagnostics/locate?gateway_connection_id=${input.connectionId}`,
      { method: 'POST', body: coordinateLocation }, input.gatewayB, 409);
    assert.equal(denied.detail.owner, 'runtime_log');
    assert.equal(denied.detail.code, 'stale_runtime_log_reference');
    assert.equal(denied.detail.reason, reason);
    const requestStart = result.requests.length;
    const operation = await rejectedNavigation({ ...targetB, location: coordinateLocation });
    assert.equal(operation.ok, false);
    if (testCase === 'replace') await page.waitForTimeout(3400);
    assert(!result.requests.slice(requestStart).some(item => item.path === '/api/gateway/diagnostics' && item.include_tail !== 'false'), '旧引用失败不能转为尾读');
    await expect(output.locator('.view-lines')).not.toContainText('REPLACEMENT_ONLY');
    result.staleReferences.push({ case: testCase, reason, request_id: denied.request_id, operation });
  };
  try {
    await truncate(input.remoteLogPath, 0);
    await checkStale('truncate', 'record_changed');
  } finally { await writeFile(input.remoteLogPath, savedBytes); }
  try {
    await rename(input.remoteLogPath, movedPath);
    await writeFile(input.remoteLogPath, 'REPLACEMENT_ONLY\n');
    await checkStale('replace', 'file_replaced');
    await unlink(input.remoteLogPath);
    await checkStale('delete', 'source_unavailable');
  } finally {
    await unlink(input.remoteLogPath).catch(error => { if (error.code !== 'ENOENT') throw error; });
    await rename(movedPath, input.remoteLogPath);
  }

  result.phase = 'exact-multiple-connections';
  const queryBody = { method: 'POST', body: { scope, query: coordinateQuery, limit: 50 } };
  const locateBody = { method: 'POST', body: coordinateLocation };
  await api('/api/gateway/diagnostics/query', queryBody, input.gatewayB);
  await api('/api/gateway/diagnostics/locate', locateBody, input.gatewayB);
  const registered = await api('/api/gateway/remote-gateways', { method: 'POST',
    body: { ...input.remoteRegistration, name: '日志目标 Gateway B 第二连接' } });
  const owner = registered.data.items.find(item => item.gateway_id === input.gatewayB);
  assert.equal(owner.remote_gateways.length, 2);
  const secondConnection = owner.remote_gateways.find(item => item.gateway_connection_id !== input.connectionId).gateway_connection_id;
  result.connections = { first: input.connectionId, second: secondConnection, rejected_request_ids: [] };
  for (const [path, body] of [['query', queryBody], ['locate', locateBody]]) {
    const ambiguous = await api(`/api/gateway/diagnostics/${path}`, body, input.gatewayB, 404);
    const wrongOwner = await api(`/api/gateway/diagnostics/${path}?gateway_connection_id=${input.connectionId}`,
      path === 'query' ? body : { method: 'POST', body: { scope, reference: { ...second, gateway_id: input.gatewayA } } }, input.gatewayA, 404);
    result.connections.rejected_request_ids.push(ambiguous.request_id, wrongOwner.request_id);
    for (const connectionId of [input.connectionId, secondConnection]) {
      const exact = await api(`/api/gateway/diagnostics/${path}?gateway_connection_id=${connectionId}`, body, input.gatewayB);
      if (path === 'locate') assert.deepEqual(exact.data.reference, second);
      else assert.equal(exact.data.gateway_id, input.gatewayB);
    }
  }
  const unboundNavigation = await rejectedNavigation({ gateway_id: input.gatewayB, log_id: targetB.log_id, location: coordinateLocation });
  assert.equal(unboundNavigation.ok, false, 'UI 不能自动选择同 Gateway 的任一连接');
  await api(`/api/gateway/remote-gateways/${input.connectionId}/reconnect`, { method: 'POST' });
  const resumed = await api(`/api/gateway/diagnostics/locate?gateway_connection_id=${input.connectionId}`, locateBody, input.gatewayB);
  assert.deepEqual(resumed.data.reference, second);
  await api(`/api/gateway/remote-gateways/${input.connectionId}`, { method: 'DELETE' });
  for (const [path, body] of [['query', queryBody], ['locate', locateBody]]) {
    const deleted = await api(`/api/gateway/diagnostics/${path}?gateway_connection_id=${input.connectionId}`, body, input.gatewayB, 404);
    result.connections.rejected_request_ids.push(deleted.request_id);
    await api(`/api/gateway/diagnostics/${path}`, body, input.gatewayB);
  }
  assert.equal((await rejectedNavigation({ ...targetB, location: coordinateLocation })).ok, false);
  await command('boxteam.output.openLog', { ...targetB, gateway_connection_id: secondConnection, location: coordinateLocation });
  await copySelection(coordinateQuery);
  result.connections.deleted_connection_rejected = true;

  result.phase = 'real-plugin-instance-replacement';
  const originalDescriptor = (await api(`/api/gateway/diagnostics?gateway_connection_id=${secondConnection}&log_id=${encodeURIComponent(targetB.log_id)}&include_tail=false`, {}, input.gatewayB))
    .data.logs.find(item => item.log_id === targetB.log_id);
  const runtime = await api('/api/gateway/service-plugins/status', { method: 'POST',
    body: { gateway_id: input.gatewayB, gateway_connection_id: secondConnection } });
  const slot = runtime.data.plugins.find(item => item.binding.package_id === input.packageId && item.binding.plugin_id === input.pluginId);
  assert.equal(slot.instance.instance_id, input.slots[1].instance.instance_id);
  const replacementStart = result.requests.length;
  const restarted = await api('/api/gateway/service-plugins/restart', { method: 'POST', body: {
    gateway_id: input.gatewayB, gateway_connection_id: secondConnection, package_id: input.packageId,
    package_version: '1.0.0', plugin_id: input.pluginId, revision: slot.revision,
  } });
  const replacement = restarted.data;
  assert(replacement.log_id && replacement.log_id !== targetB.log_id);
  assert.notEqual(replacement.instance.instance_id, slot.instance.instance_id);
  const replacementDescriptor = (await api(`/api/gateway/diagnostics?gateway_connection_id=${secondConnection}&log_id=${encodeURIComponent(replacement.log_id)}&include_tail=false`, {}, input.gatewayB))
    .data.logs.find(item => item.log_id === replacement.log_id);
  await expect(outputChannel.locator('option').filter({ hasText: originalDescriptor.label })).toHaveCount(0);
  await expect(outputChannel.locator('option').filter({ hasText: replacementDescriptor.label })).toHaveCount(1);
  assert(!result.requests.slice(replacementStart).some(item => item.log_id === replacement.log_id && item.include_tail !== 'false'), '实例替换不能自动尾读同名新实例');
  const historic = await api(`/api/gateway/diagnostics/locate?gateway_connection_id=${secondConnection}`, locateBody, input.gatewayB);
  assert.deepEqual(historic.data.reference, second);
  assert.equal(historic.data.source.log_id, targetB.log_id);
  const replacementTarget = { ...targetB, gateway_connection_id: secondConnection, log_id: replacement.log_id };
  const replacementRead = readResponse(replacementTarget);
  const [replacementResponse] = await Promise.all([replacementRead, command('boxteam.output.openLog', replacementTarget)]);
  result.replacement = { original_log_id: targetB.log_id, replacement: await verifyRead(replacementResponse, replacementTarget),
    restart_request_id: restarted.request_id, historic_request_id: historic.request_id, historic_reference: historic.data.reference };

  result.phase = 'local-window-log';
  await outputChannel.selectOption({ label: '窗口' });
  await expect(outputChannel).toHaveValue('窗口');
  await expect.poll(() => output.locator('.view-lines').innerText()).toMatch(/\S/);
  await expect(panel.locator(':scope > .title .boxteam-panel-scope:visible')).toHaveCount(0);
  const windowReads = result.requests.length;
  await page.waitForTimeout(3400);
  assert.equal(result.requests.length, windowReads, '本地窗口日志不能借用远端 Gateway 诊断读取');
  result.localWindow = { channel: '窗口', gateway_reads: 0 };

  const active = (await api('/api/gateway/workspaces')).data.active_workspace;
  assert.deepEqual(active, workspaces.data.active_workspace);
  result.activeWorkspaceUnchanged = true;
  await page.screenshot({ path: `${input.artifacts}/log-navigation-ui.png`, fullPage: true });
  assert.deepEqual(result.pageErrors, []);
  assert.deepEqual(result.externalRequests, []);

  result.phase = 'startup-failure-local-diagnostic';
  result.boundaries.push('启动故障只阻断该浏览器页面的 /out/main.js；HTML、内联故障入口和后续恢复仍经真实 Gateway，故障期间不允许任何 API 请求');
  const failedStartup = await context.newPage();
  const startupRequests = [], startupErrors = [];
  failedStartup.on('request', request => startupRequests.push(new URL(request.url()).pathname));
  failedStartup.on('pageerror', error => startupErrors.push(error.message));
  const blockMain = async route => route.abort('failed');
  const mainUrl = new URL('/out/main.js', input.baseUrl).href;
  await failedStartup.route(mainUrl, blockMain);
  await failedStartup.goto(input.baseUrl, { waitUntil: 'domcontentloaded' });
  const startup = failedStartup.locator('#startup-error');
  await expect(startup).toBeVisible();
  await expect(failedStartup.locator('.monaco-workbench')).toHaveCount(0);
  await expect(startup.getByRole('button', { name: '查看日志', exact: true })).toHaveCount(0);
  assert.equal(await startup.locator('details').getAttribute('open'), null);
  await startup.getByText('安全诊断', { exact: true }).click();
  await expect(startup.locator('pre')).toContainText('/out/main.js');
  const startupDiagnostic = await startup.locator('pre').textContent();
  await startup.getByRole('button', { name: '复制安全诊断', exact: true }).click();
  await expect(startup.getByRole('status')).toHaveText('已复制');
  assert.equal(await failedStartup.evaluate(() => navigator.clipboard.readText()), startupDiagnostic);
  await failedStartup.evaluate(() => {
    const clipboard = Object.getOwnPropertyDescriptor(navigator, 'clipboard'), exec = document.execCommand;
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText: async () => { throw new Error('fixture-clipboard-denied'); } } });
    document.execCommand = () => false;
    window.restoreStartupClipboard = () => {
      if (clipboard) Object.defineProperty(navigator, 'clipboard', clipboard); else delete navigator.clipboard;
      document.execCommand = exec;
    };
  });
  const copy = startup.getByRole('button', { name: '复制安全诊断', exact: true });
  await copy.click();
  await expect(startup.getByRole('status')).toContainText('浏览器拒绝复制文本');
  await expect(startup.getByRole('status')).not.toHaveText('已复制');
  await expect(copy).toBeFocused();
  await expect(startup.locator('pre')).toHaveText(startupDiagnostic);
  assert.equal(await failedStartup.locator('body > textarea').count(), 0);
  await failedStartup.evaluate(() => window.restoreStartupClipboard());
  const startupSummary = [];
  for (const width of [1500, 360]) {
    await failedStartup.setViewportSize({ width, height: 800 });
    const size = await startup.locator('.startup-failure-summary').evaluate(element => ({
      height: element.getBoundingClientRect().height, lineHeight: Number.parseFloat(getComputedStyle(element).lineHeight),
    }));
    assert.ok(size.height <= size.lineHeight * 2 + 1);
    startupSummary.push({ width, ...size });
  }
  assert.deepEqual(startupRequests.filter(path => path.startsWith('/api/')), []);
  assert.deepEqual(startupErrors, []);
  await failedStartup.screenshot({ path: `${input.artifacts}/startup-local-failure.png`, fullPage: true });
  await failedStartup.unroute(mainUrl, blockMain);
  await Promise.all([failedStartup.waitForNavigation({ waitUntil: 'domcontentloaded' }), startup.getByRole('button', { name: '重试', exact: true }).click()]);
  await expect(failedStartup.locator('.monaco-workbench')).toBeVisible();
  await expect(failedStartup.locator('#startup-error')).toBeEmpty();
  assert.deepEqual(startupErrors, []);
  result.startupFailure = { blocked_main: '/out/main.js', diagnostic: startupDiagnostic, summary: startupSummary,
    copy_success: true, copy_denied: true, temporary_nodes: 0, focus_preserved: true, failure_api_requests: 0,
    reload_recovered: true, stale_memory_discarded: true, page_errors: startupErrors };
  await failedStartup.close();
  result.phase = result.copyFailure ? 'navigation-complete-copy-failed' : 'complete';
} catch (error) {
  result.error = { message: error.message, stack: error.stack };
  process.exitCode = 1;
  if (page) await page.screenshot({ path: `${input.artifacts}/log-navigation-ui-failure.png`, fullPage: true }).catch(cause => { result.screenshotError = cause.message; });
} finally {
  releaseLocate();
  if (page) await page.unrouteAll({ behavior: 'wait' }).catch(error => { result.cleanupError = error.message; process.exitCode = 1; });
  if (browser) await browser.close().catch(error => { result.cleanupError = error.message; process.exitCode = 1; });
  await writeFile(input.resultPath, `${JSON.stringify(result, null, 2)}\n`);
}
