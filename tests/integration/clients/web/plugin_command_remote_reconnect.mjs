import { readFile, writeFile } from 'node:fs/promises';
import { chromium } from 'playwright';
import { expect } from 'playwright/test';
import { TestRunContext } from '../../../harness/js/run-context.mjs';

const input = JSON.parse(process.env.BOXTEAM_REMOTE_PLUGIN_COMMAND_INPUT);
const run = TestRunContext.fromTestFile('tests/integration/clients/web/test_plugin_command_remote_reconnect.py');
const receipt = { phase: 'initialize', pageErrors: [], streams: [], calls: [] };
const streamByRequest = new Map();
let page;
let failure;

async function api(path, options = {}) {
  return page.evaluate(async ({ path, options }) => {
    const credential = await (await fetch('/api/gateway/auth/local-credential')).json();
    const response = await fetch(path, {
      ...options,
      headers: { 'Content-Type': 'application/json', 'X-Local-Token': credential.data.token, ...options.headers },
      body: options.body === undefined ? undefined : JSON.stringify(options.body),
    });
    const body = await response.json();
    if (!response.ok || body.request_id !== response.headers.get('X-Request-ID')) {
      throw new Error(`${path}: ${response.status} ${JSON.stringify(body)}`);
    }
    return body.data;
  }, { path, options });
}

const command = (id, ...args) => page.evaluate(({ id, args }) => globalThis.workbench.commands.executeCommand(id, ...args), { id, args });
const sortScopes = scopes => [...scopes].sort((left, right) =>
  left === null ? (right === null ? 0 : -1) : right === null ? 1 : left.localeCompare(right));
const countExecutions = async () => {
  try { return (await readFile(input.output, 'utf8')).trim().split('\n').filter(Boolean).length; }
  catch (error) { if (error.code === 'ENOENT') return 0; throw error; }
};

try {
  const browser = await chromium.launch({ executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH || undefined,
    headless: true, args: ['--no-sandbox', '--disable-dev-shm-usage'] });
  run.addCleanup('Chromium', () => browser.close());
  const context = await browser.newContext();
  run.addCleanup('BrowserContext', () => context.close());
  page = await context.newPage();
  page.setDefaultTimeout(20_000);
  page.on('pageerror', error => receipt.pageErrors.push(error.message));
  page.on('request', request => {
    const pathname = new URL(request.url()).pathname;
    if (pathname === '/api/gateway/service-plugins/commands/events') {
      const target = JSON.parse(request.postData() ?? '{}');
      if (target.gateway_id === input.gatewayB && target.gateway_connection_id === input.connectionId) {
        const record = { target, phase: receipt.phase, state: 'open' };
        receipt.streams.push(record);
        streamByRequest.set(request, record);
      }
    } else if (pathname === '/api/gateway/service-plugins/commands/call') {
      receipt.calls.push(JSON.parse(request.postData() ?? '{}'));
    }
  });
  page.on('requestfinished', request => {
    const record = streamByRequest.get(request);
    if (record) record.state = 'finished';
  });
  page.on('requestfailed', request => {
    const record = streamByRequest.get(request);
    if (record) { record.state = 'failed'; record.error = request.failure()?.errorText; }
  });

  await page.goto(input.baseUrl, { waitUntil: 'domcontentloaded' });
  await expect(page.locator('#workbench .monaco-workbench')).toBeVisible();
  await expect(page.getByRole('button', { name: '刷新资源树', exact: true })).toBeVisible();
  const health = await api('/api/gateway/health');
  expect(health.gateway_id).toBe(input.gatewayA);
  const workspaces = await api('/api/gateway/workspaces');
  const remote = workspaces.items.find(item => item.gateway_id === input.gatewayB && item.workspace_id === input.workspaceId);
  expect(remote?.connection_kind).toBe('remote_gateway');
  expect(remote.remote_gateways[0].gateway_connection_id).toBe(input.connectionId);

  const row = page.getByTestId(`workspace-node-${JSON.stringify([input.gatewayB, input.workspaceId])}`);
  await expect(row).toBeVisible();
  await row.locator('button.workspace-label').click();
  await expect(row).toHaveAttribute('aria-current', 'location');
  const targetHeaders = {
    'X-BoxTeam-Target-Gateway-Id': input.gatewayB,
    'X-BoxTeam-Workspace-Id': input.workspaceId,
    'X-BoxTeam-Gateway-Connection-Id': input.connectionId,
  };
  const roots = await api('/api/v1/workspace/files/roots', { headers: targetHeaders });
  const fileUri = await page.evaluate(({ gatewayId, workspaceId, rootUri }) => globalThis.workbench.URI.from({
    scheme: 'boxteam-file', authority: gatewayId,
    path: `/${workspaceId}/${encodeURIComponent(rootUri)}/remote-command-owner.txt`,
  }).toString(), { gatewayId: input.gatewayB, workspaceId: input.workspaceId, rootUri: roots.workspace_uri });
  await command('_workbench.open', fileUri, [undefined, { pinned: true }]);
  const editor = page.locator('.editor-instance .monaco-editor').filter({ visible: true }).last();
  await expect(editor.locator('.view-lines')).toContainText('远程命令归属');

  const scopes = [null, input.workspaceId];
  await expect.poll(() => receipt.streams.length).toBe(2);
  expect(sortScopes(receipt.streams.map(stream => stream.target.workspace_id ?? null))).toEqual(scopes);
  const commandId = 'boxteam.plugin:proof.remote-command-ui/commands/execute';
  const palette = page.locator('.quick-input-widget').filter({ visible: true });
  const openCommand = async () => {
    if (await palette.isVisible()) await page.keyboard.press('Escape');
    await command('workbench.action.showCommands');
    await palette.locator('input').fill('>远程命令验收：执行');
    await expect(palette.getByRole('option').filter({ hasText: '远程命令验收：执行' })).toHaveCount(1);
  };
  receipt.phase = 'initial-command';
  await openCommand();
  await palette.getByRole('option').filter({ hasText: '远程命令验收：执行' }).click();
  await expect.poll(countExecutions).toBe(1);
  await expect.poll(() => receipt.calls.length).toBe(1);
  receipt.initialInstanceId = input.initialInstanceId;

  receipt.phase = 'open-connection-manager';
  await page.getByRole('button', { name: '添加工作区', exact: true }).click();
  await page.getByRole('button', { name: /连接新的远程主机/ }).click();
  await expect(page.getByRole('dialog', { name: 'Gateway 控制中心' })).toBeVisible();
  const reconnectButton = page.getByRole('button', { name: '重新连接', exact: true });
  await expect(reconnectButton).toBeVisible();
  await reconnectButton.click();
  const confirm = page.getByRole('dialog', { name: '重新连接远程 Gateway' });
  await expect(confirm).toBeVisible();
  const reconnectResponse = page.waitForResponse(response =>
    new URL(response.url()).pathname === `/api/gateway/remote-gateways/${encodeURIComponent(input.connectionId)}/reconnect`
      && response.request().method() === 'POST');
  await confirm.getByRole('button', { name: '重新连接', exact: true }).click();
  const reconnect = await reconnectResponse;
  expect(reconnect.status()).toBe(200);
  await expect(page.getByRole('status').filter({ hasText: '已重新连接' })).toBeVisible();

  receipt.phase = 'reconnected';
  await expect.poll(() => receipt.streams.length).toBe(4);
  await expect.poll(() => receipt.streams.slice(0, 2).every(stream => stream.state !== 'open')).toBe(true);
  await expect.poll(() => receipt.streams.slice(2).every(stream => stream.state === 'open')).toBe(true);
  const snapshot = await api('/api/gateway/service-plugins/commands/publications', {
    method: 'POST', body: { gateway_id: input.gatewayB, gateway_connection_id: input.connectionId },
  });
  const publication = snapshot.publications.find(item => item.binding.package_id === 'proof.remote-command-ui');
  expect(publication?.state).toBe('ready');
  receipt.reconnectedInstanceId = publication.instance_id;
  expect(receipt.reconnectedInstanceId).toBe(input.initialInstanceId);
  receipt.executionsBeforeSecondCall = await countExecutions();
  expect(receipt.executionsBeforeSecondCall).toBe(1);

  receipt.phase = 'command-after-reconnect';
  await openCommand();
  await page.keyboard.press('Escape');
  const result = await command(commandId, { value: 'after-reconnect' });
  expect(result).toEqual({ value: 'after-reconnect', pid: input.initialProcessId, count: 2 });
  await expect.poll(countExecutions).toBe(2);
  await expect.poll(() => receipt.calls.length).toBe(2);
  receipt.subscriptionScopes = sortScopes(new Set(receipt.streams.map(stream => stream.target.workspace_id ?? null)));
  receipt.subscriptionReceipts = scopes.map(scope => {
    const matching = receipt.streams.filter(stream => (stream.target.workspace_id ?? null) === scope);
    return { scope, request_count: matching.length, old_stream_closed: matching[0].state !== 'open' };
  });
  expect(receipt.calls.map(call => call.call_id).length).toBe(new Set(receipt.calls.map(call => call.call_id)).size);
  expect(receipt.calls.every(call => call.gateway_id === input.gatewayB
    && call.gateway_connection_id === input.connectionId)).toBe(true);
  expect(receipt.pageErrors).toEqual([]);
  receipt.phase = 'complete';
} catch (error) {
  failure = error;
  receipt.failure = String(error.stack ?? error);
} finally {
  const errors = failure ? [failure] : [];
  if (failure && page) {
    try { await page.screenshot({ path: input.screenshot, fullPage: true }); } catch (error) { errors.push(error); }
  }
  try { await writeFile(input.result, JSON.stringify(receipt, null, 2) + '\n'); } catch (error) { errors.push(error); }
  try { await run.close(); } catch (error) { errors.push(error); }
  if (errors.length) throw new AggregateError(errors, '远程命令订阅重连页面验收失败');
}
