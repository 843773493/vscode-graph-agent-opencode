import assert from 'node:assert/strict';
import { writeFile } from 'node:fs/promises';
import { chromium } from 'playwright';
import { expect } from 'playwright/test';
import { parse as parseJsonc } from 'jsonc-parser';

const input = JSON.parse(process.env.BOXTEAM_TARGET_PLUGIN_UI_FIXTURE);
const result = {
  phase: 'initialization', pageErrors: [], externalRequests: [], inventoryA: [], inventoryB: [],
  detailOwners: [], runtimeStages: [], businessCall: null,
};
let browser;
let page;
let releaseDelayedWrite;
let delayedWriteResponse;
let delayedWriteCommitted;
let holdNextBWrite = false;

const delayedWriteReleased = new Promise(resolve => { releaseDelayedWrite = resolve; });
const delayedWriteSeen = new Promise(resolve => { delayedWriteCommitted = resolve; });

try {
  browser = await chromium.launch({ executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH, headless: true });
  page = await browser.newPage({ viewport: { width: 1500, height: 950 } });
  page.setDefaultTimeout(15_000);
  page.on('pageerror', error => result.pageErrors.push(error.message));
  page.on('request', request => {
    const url = new URL(request.url());
    if (['http:', 'https:'].includes(url.protocol) && url.origin !== new URL(input.baseUrl).origin) {
      result.externalRequests.push(request.url());
    }
  });
  await page.route(/\/api\/gateway\/config\/sources\/gateway_mutable_override(?:\?.*)?$/, async route => {
    const request = route.request();
    if (!holdNextBWrite || request.method() !== 'PUT') return route.continue();
    const body = JSON.parse(request.postData() ?? '{}');
    const headers = await request.allHeaders();
    if (body.gateway_id !== input.gatewayB || headers['x-boxteam-target-gateway-id'] !== input.gatewayB) {
      return route.continue();
    }
    holdNextBWrite = false;
    // route.fetch 让真实 Gateway 先完成配置 CAS；只延迟 Workbench 收到的原始响应。
    const response = await route.fetch();
    delayedWriteResponse = { status: response.status(), body, headers };
    delayedWriteCommitted();
    await delayedWriteReleased;
    await route.fulfill({ response });
  });

  await page.goto(input.baseUrl, { waitUntil: 'domcontentloaded' });
  await expect(page.locator('#workbench .monaco-workbench')).toBeVisible();
  const api = (path, options = {}, targetGatewayId, expectedStatus = 200, raw = false) => page.evaluate(async ({ path, options, targetGatewayId, expectedStatus, raw }) => {
    const credential = await (await fetch('/api/gateway/auth/local-credential')).json();
    const response = await fetch(path, { ...options, headers: {
      'X-Local-Token': credential.data.token,
      'Content-Type': 'application/json',
      ...(targetGatewayId ? { 'X-BoxTeam-Target-Gateway-Id': targetGatewayId } : {}),
      ...(options.headers ?? {}),
    }, body: options.body && JSON.stringify(options.body) });
    const envelope = await response.json();
    if (response.status !== expectedStatus || response.headers.get('X-Request-ID') !== envelope.request_id) {
      throw new Error(`${path}: ${response.status} ${JSON.stringify(envelope)}`);
    }
    return raw ? envelope : envelope.data;
  }, { path, options, targetGatewayId, expectedStatus, raw });

  const health = await api('/api/gateway/health');
  const workspaces = await api('/api/gateway/workspaces');
  const workspace = await api('/api/v1/workspace');
  assert.equal(health.gateway_id, input.gatewayA);
  assert.equal(workspaces.active_workspace.gateway_id, input.gatewayA);
  assert.equal(workspaces.active_workspace.workspace_id, workspace.workspace_id);
  result.initialization = { gateway_id: health.gateway_id, active_workspace: workspaces.active_workspace };
  await page.evaluate(() => globalThis.workbench.commands.executeCommand('workbench.view.extensions'));

  const view = page.locator('.service-plugin-packages-view');
  await expect(view).toBeVisible();
  const quick = page.locator('.quick-input-widget');
  const pick = async label => {
    await expect(quick).toBeVisible();
    await quick.locator('.monaco-list-row').filter({ hasText: label }).first().click();
  };
  const inputQuick = async value => {
    await expect(quick).toBeVisible();
    const field = quick.locator('input').first();
    await field.fill(value);
    await field.press('Enter');
  };
  const titleTarget = () => page.locator(
    '.monaco-workbench .part.sidebar .composite.title .title-actions [aria-label="选择扩展管理目标"], '
    + '.monaco-workbench .part.sidebar .composite.title .title-actions [aria-label="' + input.gatewayA + '"], '
    + '.monaco-workbench .part.sidebar .composite.title .title-actions [aria-label="' + input.gatewayB + '"]',
  ).first();
  const chooseTarget = async label => {
    const control = titleTarget();
    await expect(control).toBeVisible();
    await control.click();
    await pick(label);
  };
  const packageRow = version => view.locator('.service-plugin-package').filter({
    has: page.getByRole('heading', {
      name: `目标归属 UI 验收包 · ${input.packageId}@${version}`, exact: true,
    }),
  });
  const openDetails = async version => {
    await packageRow(version).getByRole('button', { name: `目标归属 Echo · ${input.pluginId}`, exact: true }).click();
    const details = view.locator('.service-plugin-details');
    await expect(details).toBeVisible();
    return details;
  };
  const waitForTargetInventory = async versions => {
    for (const version of versions) await expect(packageRow(version)).toBeVisible();
    await expect(view.locator('.service-plugin-package')).toHaveCount(versions.length);
  };

  result.phase = 'title-target-b';
  await chooseTarget(input.remoteName);
  await waitForTargetInventory([input.versionB1, input.versionB2]);
  result.inventoryB = (await view.locator('.service-plugin-package h3').allTextContents())
    .map(title => title.slice(title.lastIndexOf('@') + 1));
  const bDetails = await openDetails(input.versionB1);
  const bOwner = await bDetails.locator('p').nth(1).textContent();
  assert.match(bOwner, new RegExp(input.gatewayB));
  result.detailOwners.push(input.gatewayB);

  result.phase = 'title-target-a';
  await chooseTarget('本地');
  await waitForTargetInventory([input.versionA]);
  result.inventoryA = (await view.locator('.service-plugin-package h3').allTextContents())
    .map(title => title.slice(title.lastIndexOf('@') + 1));
  await expect(view.locator('.service-plugin-package h3')).not.toContainText(input.versionB1);
  const aDetails = await openDetails(input.versionA);
  const aOwner = await aDetails.locator('p').nth(1).textContent();
  assert.match(aOwner, new RegExp(input.gatewayA));
  result.detailOwners.push(input.gatewayA);

  result.phase = 'delayed-b-write';
  await chooseTarget(input.remoteName);
  await waitForTargetInventory([input.versionB1, input.versionB2]);
  await openDetails(input.versionB1);
  holdNextBWrite = true;
  await view.getByRole('button', { name: '编辑参数 JSON', exact: true }).click();
  await pick('gateway_mutable_override');
  await pick('编辑此来源的参数');
  await inputQuick(JSON.stringify({ marker: 'B-from-Workbench' }));
  const delayedUiResponse = page.waitForResponse(response => response.request().method() === 'PUT'
    && new URL(response.url()).pathname.endsWith('/config/sources/gateway_mutable_override'));
  await pick('确认保存 Gateway 插件参数');
  await Promise.race([delayedWriteSeen, new Promise((_, reject) => {
    setTimeout(() => reject(new Error('等待 Gateway B 原始配置 PUT 返回头超时')), 20_000);
  })]);
  assert.equal(delayedWriteResponse.status, 200);
  assert.equal(delayedWriteResponse.body.gateway_id, input.gatewayB);
  assert.equal(delayedWriteResponse.headers['x-boxteam-target-gateway-id'], input.gatewayB);

  const savedB = await api('/api/gateway/config/sources/gateway_mutable_override', {}, input.gatewayB);
  const savedConfig = parseJsonc(savedB.jsonc);
  const bMarker = savedConfig.plugins.configuration.values[input.packageId][input.pluginId].marker;
  assert.equal(bMarker, 'B-from-Workbench');
  const savedA = await api('/api/gateway/config/sources/gateway_mutable_override', {}, input.gatewayA);
  const aConfig = parseJsonc(savedA.jsonc);
  const aMarker = aConfig.plugins.configuration.values[input.packageId][input.pluginId].marker;
  assert.equal(aMarker, 'A-unchanged');

  await chooseTarget('本地');
  await waitForTargetInventory([input.versionA]);
  await page.evaluate(() => globalThis.workbench.commands.executeCommand('workbench.extensions.action.refreshExtension'));
  await waitForTargetInventory([input.versionA]);
  releaseDelayedWrite();
  const delayedReceipt = await delayedUiResponse;
  assert.equal(delayedReceipt.status(), 200);
  const delayedEnvelope = await delayedReceipt.json();
  assert.equal(delayedEnvelope.data.edit.state, 'active');
  assert.equal(delayedEnvelope.request_id, delayedReceipt.headers()['x-request-id']);
  await expect(quick).not.toBeVisible();
  await expect(view.locator('.service-plugin-package h3').first()).toContainText(input.versionA);
  result.delayedWrite = {
    gateway_id: delayedWriteResponse.body.gateway_id,
    status: delayedWriteResponse.status,
    b_marker: bMarker,
    a_marker: aMarker,
    page_owner_after_release: input.gatewayA,
  };

  result.phase = 'explicit-b-version-switch';
  await chooseTarget(input.remoteName);
  await waitForTargetInventory([input.versionB1, input.versionB2]);
  const v2Details = await openDetails(input.versionB2);
  assert.match(await v2Details.locator('p').nth(1).textContent(), new RegExp(input.gatewayB));
  await view.evaluate(root => {
    const history = [];
    const capture = () => {
      const value = root.querySelector('[aria-label="插件配置与运行版本"]')?.textContent;
      if (value && history.at(-1) !== value) history.push(value);
    };
    const observer = new MutationObserver(capture);
    capture();
    observer.observe(root, { subtree: true, childList: true, characterData: true });
    globalThis.__targetPluginRuntimeHistory = history;
  });
  await view.getByRole('button', { name: '切换到此版本（保留开关）', exact: true }).click();
  await pick('gateway_mutable_override');
  const submitted = page.waitForResponse(response => response.request().method() === 'PUT'
    && new URL(response.url()).pathname.endsWith('/config/sources/gateway_mutable_override'));
  await pick('确认保存 Gateway 插件绑定');
  const response = await submitted;
  assert.equal(response.status(), 200);
  const receipt = await response.json();
  assert.equal(receipt.data.gateway_id, input.gatewayB);
  assert.equal(receipt.data.edit.state, 'active');
  const versionLabel = view.getByLabel('插件配置与运行版本');
  await expect(versionLabel).toContainText(`配置：启用 @${input.versionB2}`);
  const seenStages = new Set();
  const stageDeadline = Date.now() + 15_000;
  while (Date.now() < stageDeadline && !seenStages.has('ready')) {
    const text = await versionLabel.textContent() ?? '';
    if (text.includes('实际：starting')) seenStages.add('starting');
    if (text.includes(`实际：ready @${input.versionB2}`)) seenStages.add('ready');
    await page.waitForTimeout(100);
  }
  result.runtimeLabelHistory = await page.evaluate(() => globalThis.__targetPluginRuntimeHistory);
  const visibleLabels = [...result.runtimeLabelHistory, await versionLabel.textContent() ?? '']
    .filter(text => text.includes(`配置：启用 @${input.versionB2}`));
  if (visibleLabels.some(text => text.includes('实际：starting'))) result.runtimeStages.push('starting');
  if (visibleLabels.some(text => text.includes(`实际：ready @${input.versionB2}`))) result.runtimeStages.push('ready');
  assert(result.runtimeStages.includes('ready'), `Workbench 未显示 ready 阶段：${await versionLabel.textContent()}`);

  const slot = await api('/api/gateway/service-plugins/status', {
    method: 'POST', body: { gateway_id: input.gatewayB },
  });
  const switched = slot.plugins.find(item => item.binding.package_id === input.packageId
    && item.binding.plugin_id === input.pluginId);
  assert.equal(switched.binding.package_version, input.versionB2);
  assert.equal(switched.state, 'ready');
  const query = new URLSearchParams({ package_version: input.versionB2, instance_id: switched.instance.instance_id });
  const callEnvelope = await api(`/api/gateway/service-plugins/${input.packageId}/${input.pluginId}/instances/`
    + `${switched.instance.instance_id}/services/echo/value?${query}`, {}, input.gatewayB, 200, true);
  const call = callEnvelope;
  assert.equal(call.instance_id, switched.instance.instance_id);
  assert.equal(call.version, input.versionB2);
  assert.equal(call.pid, switched.instance.process_id);
  result.businessCall = { gateway_id: input.gatewayB, request_id: callEnvelope.request_id, ...call };
  const retiredQuery = new URLSearchParams({ package_version: input.versionB1, instance_id: input.initialBInstanceId });
  const retiredCall = await api(`/api/gateway/service-plugins/${input.packageId}/${input.pluginId}/instances/`
    + `${input.initialBInstanceId}/services/echo/value?${retiredQuery}`, {}, input.gatewayB, 409, true);
  assert.match(retiredCall.detail, /版本已切换/);
  result.retiredCall = {
    gateway_id: input.gatewayB,
    status: 409,
    request_id: retiredCall.request_id,
    detail: retiredCall.detail,
  };
  result.phase = 'complete';
} catch (error) {
  result.error = error instanceof Error ? error.stack : String(error);
  throw error;
} finally {
  releaseDelayedWrite?.();
  if (page && result.phase !== 'complete') {
    const screenshotPath = input.resultPath.replace(/\.json$/, '-failure.png');
    try { await page.screenshot({ path: screenshotPath, fullPage: true }); result.screenshotPath = screenshotPath; }
    catch (error) { result.screenshotError = error instanceof Error ? error.message : String(error); }
  }
  if (browser) await browser.close();
  await writeFile(input.resultPath, `${JSON.stringify(result, null, 2)}\n`);
}
