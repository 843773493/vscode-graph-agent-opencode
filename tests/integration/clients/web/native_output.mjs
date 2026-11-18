// Integration：原生输出通道通过真实 Gateway 读取；不伪造日志或预写浏览器存储。
import { expect } from 'playwright/test';

export async function verifyNativeOutput(page, owner, result) {
  const panel = page.locator('.part.panel');
  const execute = command => page.evaluate(id => globalThis.workbench.commands.executeCommand(id), command);
  const pickChannel = async label => {
    // 原版 Quick Pick 的命令在选择结束时才返回，不能在点击候选前等待它结束。
    await page.evaluate(() => { void globalThis.workbench.commands.executeCommand('workbench.action.showOutputChannels'); });
    await page.locator('.quick-input-widget').getByText(label, { exact: true }).click();
    await expect(page.locator('.quick-input-widget')).toBeHidden();
  };
  await execute('boxteam.output.focus');
  await expect(panel.locator('.output-view .monaco-editor')).toBeVisible();
  await panel.locator(':scope > .title').getByRole('button', { name: /选择面板工作区/ }).click();
  const scoped = page.waitForResponse(response => {
    const url = new URL(response.url());
    return url.pathname === '/api/gateway/diagnostics' && url.searchParams.get('workspace_id') === owner.workspaceId
      && !url.searchParams.has('log_id');
  });
  await page.locator('.quick-input-widget .monaco-list-row').filter({ hasText: owner.workspaceId }).click();
  await scoped;
  const channels = panel.locator('select[aria-label="切换输出"]').first();
  await expect(channels.locator('option').filter({ hasText: '工作区后端' })).toHaveCount(1);
  await expect(channels.locator('option').filter({ hasText: 'Launcher 日志' })).toHaveCount(1);
  await expect(channels.locator('option').filter({ hasText: /^窗口$/ })).toHaveCount(1);
  await pickChannel('工作区后端');
  await expect(panel.locator(':scope > .title .boxteam-panel-scope:visible')).toHaveCount(2);
  const responseReady = page.waitForResponse(response => {
    const url = new URL(response.url());
    return url.pathname === '/api/gateway/diagnostics'
      && url.searchParams.get('workspace_id') === owner.workspaceId
      && url.searchParams.get('log_id') === `workspace:${owner.workspaceId}:workspace_api`;
  });
  await execute('boxteam.output.refresh');
  const response = await responseReady;
  const body = await response.json();
  expect(response.status()).toBe(200);
  expect(response.headers()['x-request-id']).toBe(body.request_id);
  expect(body.data.gateway_id).toBe(owner.gatewayId);
  expect(body.data.selected_workspace_id).toBe(owner.workspaceId);
  const log = body.data.logs.find(log => log.log_id === body.data.selected_log_id);
  expect(log.status).toBe('available');
  expect(log.tail.length).toBeGreaterThan(0);
  await expect.poll(() => panel.locator('.output-view .view-lines').innerText()).toMatch(/\S/);
  const editor = panel.locator('.output-view .monaco-editor');
  await editor.locator('textarea').focus();
  await page.keyboard.press('ControlOrMeta+End');
  const sample = log.tail.trimEnd().split('\n').at(-1).slice(0, 100).replace(/\s/g, '');
  await expect.poll(async () => (await editor.locator('.view-lines').innerText()).replace(/\s/g, '')).toContain(sample);
  result.native_output = {
    request_id: body.request_id, workspace_id: owner.workspaceId,
    channels: await channels.locator('option').allTextContents(),
    selected_log_id: log.log_id, tail_bytes: new TextEncoder().encode(log.tail).length,
  };
  // 原生窗口日志仍可打开；不是只恢复一个带原版外壳的工作区日志页面。
  await pickChannel('窗口');
  await expect(panel.locator(':scope > .title .boxteam-panel-scope:visible')).toHaveCount(0);
  await expect.poll(() => panel.locator('.output-view .view-lines').innerText()).toMatch(/\S/);
  await pickChannel('工作区后端');
  await expect(panel.locator(':scope > .title .boxteam-panel-scope:visible')).toHaveCount(2);
  const filter = panel.locator('.viewpane-filter input:visible');
  await filter.fill('native-output-no-match-7b2197');
  await expect.poll(() => panel.locator('.output-view .view-lines').innerText()).not.toMatch(/\S/);
  await filter.fill('');
  await expect.poll(() => panel.locator('.output-view .view-lines').innerText()).toMatch(/\S/);
  let hiddenReads = 0;
  const countHidden = request => { if (new URL(request.url()).pathname === '/api/gateway/diagnostics') hiddenReads += 1; };
  await execute('workbench.action.closePanel');
  page.on('request', countHidden);
  try {
    // 跨过一个实际轮询周期，证明隐藏后没有继续读取。
    await page.waitForTimeout(3400);
    expect(hiddenReads).toBe(0);
  } finally { page.off('request', countHidden); }
  await execute('boxteam.output.focus');
  await expect(panel.locator('.output-view .view-lines')).toBeVisible();
  await expect.poll(() => panel.locator('.output-view .view-lines').innerText()).toMatch(/\S/);
  expect(await panel.locator('.output-view').count()).toBe(1);
  result.native_output.hidden_reads = hiddenReads;
  result.native_output.filter_and_reopen = true;
}
