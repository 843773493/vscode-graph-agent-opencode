import { expect } from 'playwright/test';
import { searchRequest, searchReceipt } from '../../../../support/workbench/search-observation.mjs';
import { searchFault } from './query_faults.mjs';
import { expectLastSearchStatus } from './default_interactions.mjs';

/** 范围勾选和确认走原 Quick Input；关键状态经真实 Gateway 管理入口生成。 */
export async function verifyDefaultScope({ page, command, api, result, fileUri, ownerA, ownerB, stoppedWorkspace }) {
  result.phase = 'quick-access-choose-scopes';
  await api('/api/gateway/managed-workspaces', { method: 'POST',
    body: JSON.stringify({ root_path: stoppedWorkspace, name: '范围隔离 C' }) });
  const registered = (await api('/api/gateway/workspaces?check_health=false')).data.items.find(item => item.name === '范围隔离 C');
  if (!registered?.backend_workspace_id) throw new Error('C 注册回执缺少稳定工作区身份');
  const stopped = (await api(`/api/gateway/workspaces/${registered.workspace_id}/runtime/stop`, { method: 'POST' })).data;
  expect(stopped.status).toBe('stopped');
  await page.reload({ waitUntil: 'domcontentloaded' });
  await expect(page.getByTestId(`workspace-node-${registered.workspace_id}`)).toContainText('范围隔离 C');
  const requests = [], responses = [], lifecycle = [], responseReads = [], responseErrors = [], cancelled = [];
  result.defaultScope = { requests, responses, lifecycle, responseErrors, cancelled };
  const observe = request => {
    const url = new URL(request.url());
    if (request.method() !== 'GET' && /^\/api\/gateway\/(?:managed-workspaces|workspaces|ssh-connections|gateway-connections)(?:\/|$)/.test(url.pathname)) {
      lifecycle.push({ path: url.pathname, method: request.method() });
    }
    const observed = searchRequest(request);
    if (observed) requests.push(observed);
  };
  const observeResponse = request => {
    if (new URL(request.url()).pathname !== '/api/gateway/search') return;
    responseReads.push(searchReceipt(request).then(receipt => responses.push(receipt)).catch(error => responseErrors.push(String(error))));
  };
  const observeCancellation = request => {
    const observed = searchRequest(request);
    if (observed?.query === 'quick-concurrency') cancelled.push({ path: observed.path, categories: observed.categories, reason: request.failure()?.errorText });
  };
  page.on('request', observe);
  // 取消的请求可能发出 response 后丢弃正文；只对成功传完的请求核验正文信封。
  page.on('requestfinished', observeResponse);
  page.on('requestfailed', observeCancellation);
  const quick = page.locator('.quick-input-widget'), input = quick.locator('input').first();
  const choose = async () => {
    await quick.getByRole('button', { name: '选择搜索范围', exact: true }).click();
    await expect(quick).toContainText('选择快速访问搜索范围');
  };
  const row = id => quick.locator('.monaco-list-row').filter({ hasText: id });
  const queryRequests = start => requests.slice(start).filter(item => item.query === 'quickdiscover');
  const lastStatus = label => expectLastSearchStatus({ page, quick, label });
  try {
    await command('_workbench.open', await fileUri(ownerA), [undefined, { pinned: true }]);
    await command('workbench.action.quickOpen', 'quickdiscover');
    await expect(quick).toContainText('quickdiscover A 会话');
    await lastStatus('文件 · 本次查询完成');
    // 先由用户键盘明确选择候选，再验证取消范围选择保留同一身份。
    await command('quickInput.first');
    await command('quickInput.next');
    await expect(quick.locator('.monaco-list-row.focused [data-quick-input-id]')).toHaveAttribute('data-quick-input-id', /^session:/);
    const activeId = await quick.locator('.monaco-list-row.focused [data-quick-input-id]').getAttribute('data-quick-input-id');
    await choose();
    const beforeCancel = requests.length;
    await row(ownerB.workspaceId).getByRole('checkbox').click();
    await page.waitForTimeout(250);
    expect(requests.length).toBe(beforeCancel);
    await page.keyboard.press('Escape');
    await expect(input).toHaveValue('quickdiscover');
    await expect(quick).toContainText('quickdiscover A 会话');
    await lastStatus('文件 · 本次查询完成');
    await expect(quick).not.toContainText('quickdiscover B 会话');
    await expect(quick.locator('.monaco-list-row.focused [data-quick-input-id]')).toHaveAttribute('data-quick-input-id', activeId);
    expect(queryRequests(beforeCancel).every(item => item.path === '/api/gateway/search'
      ? item.owners.length === 1 && item.owners[0] === ownerA.workspaceId : item.route === ownerA.route)).toBe(true);

    await choose();
    const beforeConfirm = requests.length;
    await row(ownerB.workspaceId).getByRole('checkbox').click();
    expect(requests.length).toBe(beforeConfirm);
    await quick.getByRole('button', { name: 'OK', exact: true }).click();
    await expect(quick).toContainText('quickdiscover A 会话');
    await expect(quick).toContainText('quickdiscover B 会话');
    await lastStatus('搜索隔离 B · 文件 · 本次查询完成');
    const both = queryRequests(beforeConfirm);
    expect(both.filter(item => item.path === '/api/gateway/search' && !item.categories.includes('file')).map(item => item.owners)).toEqual([[ownerA.workspaceId, ownerB.workspaceId]]);
    expect([...new Set(both.filter(item => item.categories?.includes('file')).flatMap(item => item.owners))].sort()).toEqual([ownerA.workspaceId, ownerB.workspaceId].sort());
    expect(requests.every(item => item.route !== registered.workspace_id)).toBe(true);
    await expect(input).toHaveAttribute('placeholder', /home.*搜索隔离 B/);

    // Gateway 出站故障点持有三个已完成的真实领域响应，第四分支仍在后端队列。
    await searchFault(page, 'quick-concurrency', 'delay');
    try {
      await input.fill('quick-concurrency');
      await expect.poll(async () => (await searchFault(page, 'quick-concurrency')).reads.filter(read => read.received).length).toBe(3);
      const blocked = (await searchFault(page, 'quick-concurrency')).reads;
      expect(blocked).toHaveLength(3);
      expect(new Set(blocked.map(read => read.workspace_id))).toEqual(new Set([ownerA.workspaceId, ownerB.workspaceId]));
      await input.fill('quickdiscover');
      await expect(quick).toContainText('quickdiscover A 会话');
      await expect(quick).toContainText('quickdiscover B 会话');
      await lastStatus('搜索隔离 B · 文件 · 本次查询完成');
      await expect.poll(async () => (await searchFault(page, 'quick-concurrency')).reads.filter(read => read.cancelled && read.settled).length).toBe(3);
      expect((await searchFault(page, 'quick-concurrency')).reads).toHaveLength(3);
      expect(requests.filter(item => item.path === '/api/gateway/search' && item.query === 'quick-concurrency' && !item.categories.includes('file'))).toHaveLength(1);
      await expect.poll(() => cancelled.filter(item => item.path === '/api/gateway/search' && !item.categories.includes('file')).length).toBe(1);
      expect(cancelled.every(item => /ERR_ABORTED/.test(item.reason))).toBe(true);
      result.defaultScope.cancelledReads = (await searchFault(page, 'quick-concurrency')).reads;
      await expect(quick).toContainText('quickdiscover A 会话');
      await expect(quick).toContainText('quickdiscover B 会话');
    } finally { await searchFault(page, 'quick-concurrency', 'release'); }

    // 原帮助及正文模式保留用户明确的集合，不能重新捕获当前活动文件的单一 owner。
    await input.fill('?');
    await quick.getByRole('option', { name: /^%,/ }).click();
    const beforeText = requests.length;
    await input.fill('%quickmodeword');
    await expect(quick).toContainText('mode scope A quickmodeword');
    await expect(quick).toContainText('mode scope B quickmodeword');
    const text = requests.slice(beforeText).filter(item => item.query === 'quickmodeword');
    expect([...new Set(text.flatMap(item => item.owners))].sort()).toEqual([ownerA.workspaceId, ownerB.workspaceId].sort());
    await input.fill('?');
    await quick.getByRole('option', { name: /^…,/ }).click();
    await input.fill('quickdiscover');
    await expect(quick).toContainText('quickdiscover B 会话');
    await lastStatus('搜索隔离 B · 文件 · 本次查询完成');
    await choose();
    await row(registered.backend_workspace_id).getByRole('checkbox').click();
    const beforeStopped = requests.length;
    await quick.getByRole('button', { name: 'OK', exact: true }).click();
    await lastStatus('范围隔离 C 当前不可查询');
    await expect(quick).toContainText('quickdiscover A 会话');
    await expect(quick).toContainText('quickdiscover B 会话');
    expect(requests.slice(beforeStopped).every(item => item.route !== registered.workspace_id)).toBe(true);
    expect(lifecycle).toEqual([]);
    await page.keyboard.press('Escape');
    const current = (await api('/api/gateway/workspaces?check_health=false')).data;
    const stillStopped = current.items.find(item => item.workspace_id === registered.workspace_id);
    expect(stillStopped.status).toBe('offline');
    expect(stillStopped.runtime_action).toBe('start_managed_backend');
    expect(responses.length).toBeGreaterThan(0);
    await Promise.all(responseReads);
    expect(responseErrors).toEqual([]);
    expect(responses.every(item => item.status === 200 && item.requestId && item.requestId === item.header)).toBe(true);
    result.defaultScope = { requests, responses, lifecycle, responseErrors, cancelled, cancelKeepsQueryAndSelection: true, explicitAB: true,
      helpKeepsScopes: true, cancelledReads: result.defaultScope.cancelledReads, boundedQueueCancelled: true, stoppedHasNoBusinessRequests: true, stoppedRoute: registered.workspace_id };
  } finally {
    page.off('request', observe);
    page.off('requestfinished', observeResponse);
    page.off('requestfailed', observeCancellation);
    await Promise.all(responseReads);
    await command('workbench.action.closeAllEditors');
    const current = (await api('/api/gateway/workspaces?check_health=false')).data.items.find(item => item.workspace_id === registered.workspace_id);
    await api(`/api/gateway/managed-workspaces/${registered.workspace_id}?force=true`, { method: 'DELETE',
      body: JSON.stringify({ expected_backend_generation: current.backend_generation, expected_workspace_ids: current.affected_workspace_ids }) });
  }
}
