// Integration：真实 Gateway／Workbench；回放仅供启动，无响应替身或服务注册替换。
import { dirname, join } from 'node:path';
import { writeFile } from 'node:fs/promises';
import { chromium } from 'playwright';
import { expect } from 'playwright/test';
import { TestRunContext } from '../../../../harness/js/run-context.mjs';

const required = name => {
  const value = process.env[name];
  if (!value) throw new Error(`条件模式验收缺少 ${name}`);
  return value;
};
const output = required('BOXTEAM_BROWSER_RESULT_PATH');
const run = TestRunContext.fromTestFile('tests/integration/clients/web/quick_access/test_workspace_actions.py');
const result = { classification: 'Integration', phase: 'initialize', pageErrors: [], initialization: [],
  modes: [], requests: [], unexpectedWrites: [], externalRequests: [],
  boundary: '真实Gateway／Workspace／正式Workbench；原条件Provider和本项目owner；无响应替身，无预写浏览器存储，不声称空列表已验证实际运行能力' };
let page, failure;
try {
  const browser = await chromium.launch({ executablePath: required('PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH'),
    headless: true, args: ['--no-sandbox', '--disable-dev-shm-usage'] });
  run.addCleanup('条件模式 Chromium', () => browser.close());
  const context = await browser.newContext({ locale: 'en-US', viewport: { width: 1700, height: 1200 } });
  run.addCleanup('条件模式 context', () => context.close());
  page = await context.newPage();
  page.on('pageerror', error => result.pageErrors.push(String(error)));
  // 库存／地址解析与两类 SSE 订阅使用 POST，但都不接受业务写动作。
  const readonlyPosts = new Set([
    '/api/gateway/service-plugins/inventory', '/api/gateway/resource-addresses/parse',
    '/api/gateway/service-plugins/commands/events', '/api/v1/workspace/observations/stream',
  ]);
  page.on('request', request => {
    const url = new URL(request.url());
    if (/^https?:$/.test(url.protocol) && !['127.0.0.1', 'localhost'].includes(url.hostname)) {
      result.externalRequests.push({ phase: result.phase, url: request.url() });
    }
    if (!url.pathname.startsWith('/api/')) return;
    const entry = { phase: result.phase, path: url.pathname, method: request.method() };
    result.requests.push(entry);
    if (!['GET', 'HEAD'].includes(request.method())
      && !(request.method() === 'POST' && readonlyPosts.has(url.pathname))) {
      result.unexpectedWrites.push(entry);
    }
  });
  await page.goto(required('BOXTEAM_BROWSER_BASE_URL'), { waitUntil: 'domcontentloaded' });
  await expect(page.locator('.native-session-navigation')).toBeVisible({ timeout: 30_000 });
  await expect(page.locator('html')).toHaveAttribute('lang', 'zh-CN');
  const command = (id, ...args) => page.evaluate(async ({ id, args }) => {
    await globalThis.workbench.commands.executeCommand(id, ...args);
  }, { id, args });
  for (const path of ['/api/gateway/health', '/api/gateway/workspaces', '/api/v1/workspace']) {
    const receipt = await page.evaluate(async path => {
      const credential = await (await fetch('/api/gateway/auth/local-credential')).json();
      const response = await fetch(path, { headers: { 'X-Local-Token': credential.data.token } });
      return { path, status: response.status, requestId: response.headers.get('X-Request-ID'), body: await response.json() };
    }, path);
    expect(receipt.status).toBe(200);
    expect(receipt.requestId).toBeTruthy();
    expect(receipt.body.request_id).toBe(receipt.requestId);
    result.initialization.push(receipt);
  }
  const [health, listing, workspace] = result.initialization.map(item => item.body.data);
  expect(listing.active_workspace).toEqual({ gateway_id: health.gateway_id, workspace_id: workspace.workspace_id });
  const quick = page.locator('.quick-input-widget'), input = quick.locator('input').first();
  await command('workbench.action.quickOpen', '?');
  await expect(input).toHaveValue('?');
  // 逐行键盘遍历原虚拟列表，不能用只渲染首屏的 DOM 当作全部注册项。
  const help = new Map(), visitedRows = new Set();
  let helpComplete = false;
  for (let step = 0; step < 100; step++) {
    const row = quick.locator('.monaco-list-row.focused');
    await expect(row).toHaveCount(1);
    const index = await row.getAttribute('data-index');
    expect(index).not.toBeNull();
    if (visitedRows.has(index)) { helpComplete = true; break; }
    visitedRows.add(index);
    const label = (await row.locator('.label-name').first().innerText()).trim();
    help.set(label, await row.innerText());
    await input.press('ArrowDown');
  }
  expect(helpComplete, '必须遍历到原列表首尾边界').toBe(true);
  result.help = [...help.entries()].map(([prefix, text]) => ({ prefix, text }));
  for (const prefix of ['term', 'task', 'debug', 'debug consoles', 'ext', 'ext install']) expect(help.has(prefix), prefix).toBe(true);
  // issue 只在上游 electron-browser 贡献中登记；本产品的桌面同样运行 browser Workbench。
  expect(help.has('issue')).toBe(false);
  await page.keyboard.press('Escape');

  const show = async (prefix, placeholder) => {
    result.phase = prefix.trim();
    await command('workbench.action.quickOpen', prefix);
    await expect(input).toHaveValue(prefix);
    await expect(input).toHaveAttribute('placeholder', placeholder);
    await expect(quick.locator('.monaco-progress-container.active')).toHaveCount(0);
    return { prefix, placeholder: await input.getAttribute('placeholder'), items: await quick.getByRole('option').allTextContents() };
  };
  const terminal = await show('term ', '键入要打开的终端的名称。');
  await expect(quick).toContainText('创建新的终端');
  expect(terminal.items.filter(text => /^\d+[:.]/.test(text.trim()))).toEqual([]);
  result.modes.push({ ...terminal, condition: '原TerminalService无实例；项目PTY不伪装成原实例，创建动作未执行' });
  await page.keyboard.press('Escape');
  const tasks = await show('task ', '键入要运行的任务的名称。');
  await input.fill('task conditional-no-native-task-42');
  await expect(quick).toContainText('没有匹配的任务');
  tasks.filteredItems = await quick.getByRole('option').allTextContents();
  result.modes.push({ ...tasks, condition: '原TaskService；未注册构建任务，不把项目持久任务当原生Task' });
  await page.keyboard.press('Escape');
  const debug = await show('debug ', '键入准备运行的启动配置的名称。');
  await input.fill('debug conditional-no-native-debug-42');
  await expect(quick).toContainText('没有匹配的启动配置');
  debug.filteredItems = await quick.getByRole('option').allTextContents();
  result.modes.push({ ...debug, condition: '无已绑定的插件调试器和原launch配置；未启动调试或伪造Adapter' });
  await page.keyboard.press('Escape');
  const consoles = await show('debug consoles ', '键入要打开的调试控制台的名称。');
  await expect(quick).toContainText('启动新的调试会话');
  result.modes.push({ ...consoles, condition: '原DebugService没有运行中的独立REPL；未将日志作为调试控制台' });
  await page.keyboard.press('Escape');

  const inventoryResponse = page.waitForResponse(response => new URL(response.url()).pathname === '/api/gateway/service-plugins/inventory'
    && response.request().method() === 'POST');
  const plugins = await show('ext ', /查找已安装插件/);
  const inventory = await inventoryResponse;
  result.inventory = { status: inventory.status(), requestId: inventory.headers()['x-request-id'],
    target: inventory.request().postDataJSON(), body: await inventory.json() };
  expect(result.inventory.status).toBe(200);
  expect(result.inventory.requestId).toBeTruthy();
  expect(result.inventory.body.request_id).toBe(result.inventory.requestId);
  expect(result.inventory.target.gateway_id).toBe(health.gateway_id);
  expect(result.inventory.body.data.packages).toEqual([]);
  await input.fill('ext conditional-no-plugin-42');
  await expect(quick.locator('[data-quick-input-id="no-plugin"]')).toBeVisible();
  plugins.filteredItems = await quick.getByRole('option').allTextContents();
  result.modes.push({ ...plugins, condition: '复用目标Gateway插件库存，隔离Home无已安装包，未使用微软Gallery' });
  await page.keyboard.press('Escape');
  const installation = await show('ext install ', /安装自有插件包/);
  await expect(quick.locator('[data-quick-input-id="install-plugin-package"]')).toBeVisible();
  await quick.locator('[data-quick-input-id="install-plugin-package"]').click();
  await expect(quick).toContainText('ZIP 文件');
  await expect(quick).toContainText('开发目录');
  await expect(quick).toContainText('自有来源');
  await page.keyboard.press('Escape');
  await expect(quick).not.toBeVisible();
  result.pluginInstallationCancelled = true;
  result.modes.push({ ...installation, condition: '接受后进入同一安装器的来源选择；用户取消，零安装写入' });

  if (help.has('mcpr')) {
    const resources = await show('mcpr ', '筛选到 MCP 资源');
    expect(resources.items).toEqual([]);
    result.modes.push({ ...resources, condition: '原资源Provider没有接入资源；项目连接管理不宣称支持IMcpResource读取' });
    await page.keyboard.press('Escape');
  } else result.modes.push({ prefix: 'mcpr ', condition: '原ChatContextKeys.enabled条件不满足，帮助未发布此Provider；不回退文件搜索冒充MCP互通' });
  result.modes.push({ prefix: 'issue ', condition: '本产品未导入上游electron-browser问题报告贡献；未增加外部问题服务' });
  result.browserIssueStatus = await page.evaluate(() => globalThis.workbench.commands.executeCommand('_issues.getSystemStatus'));
  expect(result.browserIssueStatus).toContain('--status');
  expect(result.browserIssueStatus).not.toBe('');
  expect(result.pageErrors).toEqual([]);
  expect(result.unexpectedWrites).toEqual([]);
  expect(result.externalRequests).toEqual([]);
  result.phase = 'complete';
} catch (error) {
  failure = error;
  result.error = error.stack ?? String(error);
  if (page) {
    try { await page.screenshot({ path: join(dirname(output), 'conditional-providers-failure.png') }); }
    catch (diagnosticError) { failure = new AggregateError([failure, diagnosticError], '条件模式验收与截图均失败'); }
  }
} finally {
  try { await run.close(); }
  catch (cleanupError) { failure = failure ? new AggregateError([failure, cleanupError], '条件模式验收与清理均失败') : cleanupError; }
  await writeFile(output, JSON.stringify(result, null, 2) + '\n');
}
if (failure) throw failure;
