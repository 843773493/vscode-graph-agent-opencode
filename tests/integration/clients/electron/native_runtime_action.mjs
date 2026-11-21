import { writeFile } from 'node:fs/promises';
import { expect } from 'playwright/test';
import { TestRunContext } from '../../../harness/js/run-context.mjs';
import { launchElectron } from './launch.mjs';

const required = name => {
  const value = process.env[name];
  if (!value) throw new Error(`Electron 运行资源验收缺少 ${name}`);
  return value;
};

const output = required('BOXTEAM_BROWSER_RESULT_PATH');
const owner = JSON.parse(required('BOXTEAM_NATIVE_ACTION_OWNER'));
const resource = JSON.parse(required('BOXTEAM_NATIVE_ACTION_RESOURCE'));
const run = TestRunContext.fromTestFile('tests/integration/clients/electron/test_native_runtime_action.py');
const result = { classification: 'Integration', phase: 'initialize', pageErrors: [], requests: [], receipts: [],
  boundary: 'Linux 真实 development ASAR、真实 Gateway/Backend 与插件托管终端；原Quick Access执行停止动作，未使用renderer bridge' };
let page, host, failure;
const save = () => writeFile(output, JSON.stringify(result, null, 2) + '\n');
const api = (path, init = {}) => page.evaluate(async ({ path, init }) => {
  const credential = await (await fetch('/api/gateway/auth/local-credential')).json();
  const response = await fetch(path, { ...init, headers: {
    'X-Local-Token': credential.data.token, 'Content-Type': 'application/json',
    'X-BoxTeam-Target-Gateway-Id': window.__boxteamNativeActionOwner.gateway_id,
    'X-BoxTeam-Workspace-Id': window.__boxteamNativeActionOwner.workspace_id, ...init.headers,
  } });
  const body = await response.json();
  if (!response.ok || body.request_id !== response.headers.get('X-Request-ID')) {
    throw new Error(JSON.stringify({ path, status: response.status, body }));
  }
  return body;
}, { path, init });

try {
  host = await launchElectron({ run, result, usePage(value) {
    page = value;
    page.on('pageerror', error => result.pageErrors.push(String(error)));
    page.on('request', request => {
      if (!new URL(request.url()).pathname.startsWith('/api/v1/resources/')) return;
      result.requests.push({ method: request.method(), path: new URL(request.url()).pathname,
        workspace_id: request.headers()['x-boxteam-workspace-id'], gateway_id: request.headers()['x-boxteam-target-gateway-id'],
        request_id: request.headers()['x-request-id'], request });
    });
    page.on('response', response => {
      if (!new URL(response.url()).pathname.startsWith('/api/v1/resources/')) return;
      const item = result.requests.find(entry => entry.request === response.request());
      if (item) {
        item.response_request_id = response.headers()['x-request-id'];
        item.status = response.status();
        delete item.request;
      }
    });
    page.on('requestfailed', request => {
      const item = result.requests.find(entry => entry.request === request);
      if (item) { item.failure = request.failure()?.errorText ?? 'request failed'; delete item.request; }
    });
  } });
  await expect(page.getByTestId("workspace-navigation-root-drop-target")).toBeVisible({ timeout: 30_000 });
  await page.evaluate(value => { window.__boxteamNativeActionOwner = value; }, owner);
  const navigation = (await api('/api/gateway/workspaces')).data;
  expect(navigation.items.some(item => item.gateway_id === owner.gateway_id
    && item.workspace_id === owner.workspace_id)).toBe(true);
  result.phase = 'resource-quick-access-stop';
  await page.evaluate(({ owner }) => globalThis.workbench.commands.executeCommand(
    'boxteam.quickAccess.resources', { gatewayId: owner.gateway_id, workspaceId: owner.workspace_id }), { owner });
  const quick = page.locator('.quick-input-widget'), input = quick.locator('input').first();
  await expect(input).toHaveValue('resource ');
  await input.fill(`resource ${resource.resource_id}`);
  const row = quick.locator('.monaco-list-row').filter({ has: page.locator(`[data-quick-input-id$="/${resource.resource_id}"]`) });
  await expect(row).toBeVisible();
  await expect(row).toContainText('原生 Quick Access 终端');
  await row.hover();
  await row.getByRole('button', { name: '更多资源动作' }).click();
  await expect(quick.getByText('资源动作 ·')).toBeVisible();
  const stoppedResponse = page.waitForResponse(response => response.request().method() === 'POST'
    && new URL(response.url()).pathname === `/api/v1/resources/${resource.resource_id}/stop`, { timeout: 30_000 });
  await quick.getByText('停止资源', { exact: true }).click();
  const response = await stoppedResponse, body = await response.json();
  expect(response.status()).toBe(200);
  expect(body.request_id).toBe(response.headers()['x-request-id']);
  expect(response.request().headers()['x-boxteam-workspace-id']).toBe(owner.workspace_id);
  expect(response.request().headers()['x-boxteam-target-gateway-id']).toBe(owner.gateway_id);
  expect(body.data).toMatchObject({ resource_id: resource.resource_id, status: 'terminated' });
  const readback = await api(`/api/v1/resources/${resource.resource_id}`);
  expect(readback.data).toMatchObject({ resource_id: resource.resource_id, status: 'terminated' });
  result.stop = { path: new URL(response.url()).pathname, status: response.status(), request_id: body.request_id,
    header: response.headers()['x-request-id'], owner: { gateway_id: owner.gateway_id, workspace_id: owner.workspace_id },
    result: body.data, readback: readback.data };
  result.quickAccessRuntimeAction = true;
  expect(result.requests.filter(item => item.method === 'POST')).toHaveLength(1);
  expect(result.requests.every(item => !item.failure && item.workspace_id === owner.workspace_id
    && item.gateway_id === owner.gateway_id && item.response_request_id)).toBe(true);
  expect(result.pageErrors).toEqual([]);
  await save();
  await host.leave();
  result.phase = 'complete';
} catch (error) {
  failure = error;
  result.error = error?.stack ?? String(error);
} finally {
  result.phase = failure ? 'failed' : result.phase;
  try { await writeFile(output, JSON.stringify(result, null, 2) + '\n'); }
  catch (error) { failure ??= error; result.writeError = String(error?.stack ?? error); }
  try { await run.close(); } catch (error) { failure ??= error; }
}
if (failure) throw failure;
