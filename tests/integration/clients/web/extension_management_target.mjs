import { writeFile } from 'node:fs/promises';
import { chromium } from 'playwright';
import { expect } from 'playwright/test';
import { TestRunContext } from '../../../harness/js/run-context.mjs';

const required = name => { if (!process.env[name]) throw new Error('缺少 ' + name); return process.env[name]; };
const fixture = JSON.parse(required('BOXTEAM_EXTENSION_MANAGEMENT_TARGET'));
const output = required('BOXTEAM_BROWSER_RESULT_PATH');
const run = await TestRunContext.fromTestFile('tests/integration/clients/web/test_extension_management_target.py').prepare();
const result = { phase: 'initialize', pageErrors: [], consoleErrors: [], writes: [], receipts: [], tabs: [],
  boundary: '正式Workbench／双真实Gateway／Backend／原Node安装器；本地无签名私有HTTP Gallery为明确外部服务装具（原private准入不要求public签名或发布者确认），SSH执行与隧道复用loopback装具；原Extensions标题、搜索、详情和按钮实际操作。无模型或扩展激活，不代表公网签名、发布者信任、真实SSH、Windows或Electron。' };
const save = () => writeFile(output, JSON.stringify(result, null, 2) + '\n');
const phase = async value => { result.phase = value; await save(); };
let page, failure;
try {
  const browser = await chromium.launch({ executablePath: required('PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH'), headless: true,
    args: ['--no-sandbox', '--disable-dev-shm-usage', '--disable-extensions', '--renderer-process-limit=2'] });
  run.addCleanup('扩展目标Chromium', () => browser.close());
  const context = await browser.newContext({ viewport: { width: 1700, height: 1100 } });
  run.addCleanup('扩展目标BrowserContext', () => context.close());
  // 使用原上游明确支持的产品配置入口，只把外部Gallery指向本测试HTTP服务。
  await context.addInitScript(({ product, version }) => {
    globalThis._VSCODE_PRODUCT_JSON = product;
    globalThis._VSCODE_PACKAGE_JSON = { version };
  }, fixture);
  page = await context.newPage();
  page.on('pageerror', error => result.pageErrors.push(String(error.stack ?? error)));
  page.on('console', entry => { if (entry.type() === 'error') result.consoleErrors.push(entry.text()); });
  page.on('request', request => {
    if (['/api/gateway/extension-installations/install-gallery', '/api/gateway/extension-installations/uninstall'].includes(new URL(request.url()).pathname)) {
      result.writes.push({ path: new URL(request.url()).pathname, ...request.postDataJSON() });
    }
  });
  const command = (id, ...args) => page.evaluate(async ({ id, args }) => globalThis.workbench.commands.executeCommand(id, ...args), { id, args });
  const api = async (path, method = 'GET', payload) => {
    const receipt = await page.evaluate(async ({ path, method, payload }) => {
      const credential = await (await fetch('/api/gateway/auth/local-credential')).json();
      const response = await fetch(path, { method, headers: { 'Content-Type': 'application/json', 'X-Local-Token': credential.data.token },
        ...(payload ? { body: JSON.stringify(payload) } : {}) });
      return { status: response.status, header: response.headers.get('X-Request-ID'), body: await response.json() };
    }, { path, method, payload });
    result.receipts.push(receipt);
    expect(receipt.status).toBe(200); expect(receipt.body.request_id).toBeTruthy(); expect(receipt.body.request_id).toBe(receipt.header);
    return receipt.body.data;
  };
  const inventory = target => api('/api/gateway/extension-installations/inventory', 'POST', { protocol_version: 1, ...target });
  const ids = target => inventory(target).then(value => value.inventory.extensions.map(extension => extension.description.id).sort());
  await page.goto(new URL('/api/gateway/health', fixture.base_url).href);
  await api('/api/gateway/users/guest', 'POST', { tracking: { source: 'extension-target-view' } });
  await page.goto(fixture.base_url, { waitUntil: 'domcontentloaded' });
  await expect(page.locator('.native-session-navigation')).toBeVisible({ timeout: 30_000 });
  const health = await api('/api/gateway/health'), workspaces = await api('/api/gateway/workspaces');
  expect(workspaces.active_workspace.gateway_id).toBe(health.gateway_id);
  await api('/api/v1/workspace');
  await command('workbench.view.extensions');
  // 原 ActionViewItem 用 tooltip 生成 aria-label；标题文字才是随目标变化的可见入口。
  const targetAction = page.getByRole('button').filter({ hasText: /^目标：/ });
  const select = async label => {
    await targetAction.click();
    const picker = page.locator('.quick-input-widget:visible');
    await expect(picker).toBeVisible();
    await picker.locator('.monaco-list-row').filter({ hasText: label }).click();
    await expect(picker).toBeHidden();
    await expect(targetAction).toHaveText('目标：' + label);
  };
  const details = page.locator('.extension-editor:visible');
  const search = async (name, label) => {
    await command('workbench.extensions.search', '@id:boxteam-probe.' + name);
    const row = page.locator('.extensions-list .monaco-list-row').filter({ hasText: label });
    await expect(row).toBeVisible({ timeout: 20_000 });
    await row.dblclick();
    await expect(details.locator('.name')).toHaveText(label);
    // 原扩展详情默认可在modal editor打开；用原命令移到主编辑器再保留Tab。
    if (await page.locator('.monaco-modal-editor-block').count()) await command('workbench.action.moveModalEditorToMain');
    await command('workbench.action.keepEditor');
  };
  const clickInstall = async label => {
    const install = details.getByRole('button', { name: 'Install', exact: true });
    await expect(install).toBeEnabled();
    const [installed] = await Promise.all([
      page.waitForResponse(item => new URL(item.url()).pathname === '/api/gateway/extension-installations/install-gallery'
        && item.request().postDataJSON().extension_id === 'boxteam-probe.' + label),
      install.click(),
    ]);
    expect(installed.status()).toBe(200);
    const receipt = await installed.json();
    expect(receipt.request_id).toBe(installed.headers()['x-request-id']);
    await expect(details.getByRole('button', { name: 'Uninstall', exact: true })).toBeVisible({ timeout: 20_000 });
    return receipt.data;
  };
  await phase('open-b-and-a-details');
  await select('Extension Target B');
  await search('view-alpha', 'Target Alpha');
  await select('本地');
  await search('view-alpha', 'Target Alpha');
  const tabs = page.getByRole('tab').filter({ hasText: 'Target Alpha' });
  await expect(tabs).toHaveCount(2);
  result.tabs = await tabs.evaluateAll(elements => elements.map(element => ({ text: element.textContent, label: element.getAttribute('aria-label'), title: element.getAttribute('title') })));
  await phase('old-b-detail-install');
  // A为当前列表，沿原编辑器导航返回先打开的B详情；实际Install按钮仍必须投B。
  await command('workbench.action.previousEditor');
  await expect(details.locator('.name')).toHaveText('Target Alpha');
  const first = await clickInstall('view-alpha');
  expect(first.gateway_id).toBe(fixture.targets.b.gateway_id); expect(first.backend_id).toBe(fixture.targets.b.backend_id);
  expect(await ids(fixture.targets.a)).toEqual([]);
  expect(await ids(fixture.targets.b)).toEqual(['boxteam-probe.view-alpha']);
  await phase('two-installs-selected-b');
  await select('Extension Target B');
  await search('view-beta', 'Target Beta');
  const second = await clickInstall('view-beta');
  expect(second.backend_id).toBe(fixture.targets.b.backend_id);
  expect(await ids(fixture.targets.b)).toEqual(['boxteam-probe.view-alpha', 'boxteam-probe.view-beta']);
  expect(await ids(fixture.targets.a)).toEqual([]);
  expect(await page.locator('.quick-input-widget:visible').count()).toBe(0);
  result.oldDetailKeptB = true;
  await phase('new-a-operation');
  await select('本地');
  await search('view-beta', 'Target Beta');
  const third = await clickInstall('view-beta');
  expect(third.backend_id).toBe(fixture.targets.a.backend_id);
  expect(await ids(fixture.targets.a)).toEqual(['boxteam-probe.view-beta']);
  expect(await ids(fixture.targets.b)).toEqual(['boxteam-probe.view-alpha', 'boxteam-probe.view-beta']);
  result.newOperationKeptA = true;
  await page.screenshot({ path: output.replace(/\.json$/, '.png'), fullPage: true });
  await phase('complete');
} catch (error) {
  failure = error; result.error = String(error.stack ?? error);
  if (page) {
    result.visible = await page.locator('body').innerText().catch(error => String(error));
    await writeFile(output.replace(/\.json$/, '.failed.html'), await page.content()).catch(error => { result.snapshotError = String(error); });
    await page.screenshot({ path: output.replace(/\.json$/, '.failed.png'), fullPage: true }).catch(error => { result.screenshotError = String(error); });
  }
} finally {
  await save();
  try { await run.close(); } catch (error) { failure = failure ? new AggregateError([failure, error], '扩展目标验收及清理失败') : error; }
}
if (failure) throw failure;
