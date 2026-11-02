import { expect } from 'playwright/test';

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
    const url = new URL(request.url()), route = request.headers()['x-boxteam-workspace-id'];
    if (request.method() !== 'GET' && /^\/api\/gateway\/(?:managed-workspaces|workspaces|ssh-connections|gateway-connections)(?:\/|$)/.test(url.pathname)) {
      lifecycle.push({ path: url.pathname, method: request.method() });
    }
    if (['/api/v1/session-catalog/search', '/api/v1/session-threads/search', '/api/v1/workspace/files/roots'].includes(url.pathname)
      || url.pathname === '/api/v1/resources/navigation' && request.postDataJSON()?.operation === 'search') {
      requests.push({ path: url.pathname, route, query: url.searchParams.get('query') ?? request.postDataJSON()?.query });
    }
  };
  const observeResponse = request => {
    if (!['/api/v1/session-catalog/search', '/api/v1/session-threads/search'].includes(new URL(request.url()).pathname)) return;
    responseReads.push((async () => {
      const response = await request.response();
      if (!response) throw new Error(`完成的查询缺少 HTTP 响应：${request.url()}`);
      const body = await response.json();
      responses.push({ status: response.status(), header: response.headers()['x-request-id'], requestId: body.request_id });
    })().catch(error => responseErrors.push(String(error))));
  };
  const observeCancellation = request => {
    if (new URL(request.url()).searchParams.get('query') === 'quick-concurrency') {
      cancelled.push({ path: new URL(request.url()).pathname, reason: request.failure()?.errorText });
    }
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
  const lastStatus = async label => {
    // 原列表只挂载可见行；经真实导航滚到末尾核状态，再返回顶部核候选。
    await expect.poll(async () => {
      await command('quickInput.last');
      return quick.getByRole('option', { name: label, exact: true }).isVisible();
    }).toBe(true);
    await command('quickInput.first');
  };
  try {
    await command('_workbench.open', await fileUri(ownerA), [undefined, { pinned: true }]);
    await command('workbench.action.quickOpen', 'quickdiscover');
    await expect(quick).toContainText('quickdiscover A 会话');
    await expect(quick).toContainText('文件 · 本次查询完成');
    const activeId = await quick.locator('.monaco-list-row.focused [data-quick-input-id]').getAttribute('data-quick-input-id');
    await choose();
    const beforeCancel = requests.length;
    await row(ownerB.workspaceId).getByRole('checkbox').click();
    await page.waitForTimeout(250);
    expect(requests.length).toBe(beforeCancel);
    await page.keyboard.press('Escape');
    await expect(input).toHaveValue('quickdiscover');
    await expect(quick).toContainText('quickdiscover A 会话');
    await expect(quick).toContainText('文件 · 本次查询完成');
    await expect(quick).not.toContainText('quickdiscover B 会话');
    await expect(quick.locator('.monaco-list-row.focused [data-quick-input-id]')).toHaveAttribute('data-quick-input-id', activeId);
    expect(queryRequests(beforeCancel).every(item => item.route === ownerA.route)).toBe(true);

    await choose();
    const beforeConfirm = requests.length;
    await row(ownerB.workspaceId).getByRole('checkbox').click();
    expect(requests.length).toBe(beforeConfirm);
    await quick.getByRole('button', { name: 'OK', exact: true }).click();
    await expect(quick).toContainText('quickdiscover A 会话');
    await expect(quick).toContainText('quickdiscover B 会话');
    await lastStatus('搜索隔离 B · 文件 · 本次查询完成');
    const both = queryRequests(beforeConfirm);
    expect([...new Set(both.map(item => item.route))].sort()).toEqual([ownerA.route, ownerB.route].sort());
    expect(requests.every(item => item.route !== registered.workspace_id)).toBe(true);
    await expect(input).toHaveAttribute('placeholder', /home.*搜索隔离 B/);

    // 只延迟原服务的真实响应；三个运行分支占满预算时，其余分支仍在本地排队。
    const gate = Promise.withResolvers(), released = Promise.withResolvers(), blocked = [];
    let finished = 0;
    const pattern = '**/api/v1/session-*/search?**';
    const delay = async route => {
      if (new URL(route.request().url()).searchParams.get('query') !== 'quick-concurrency') { await route.continue(); return; }
      const response = await route.fetch();
      blocked.push(route.request().headers()['x-boxteam-workspace-id']);
      await gate.promise;
      await route.fulfill({ response });
      if (++finished === 3) released.resolve();
    };
    await page.route(pattern, delay);
    try {
      await input.fill('quick-concurrency');
      await expect.poll(() => blocked.length).toBe(3);
      await page.waitForTimeout(250);
      expect(blocked).toHaveLength(3);
      expect(new Set(blocked)).toEqual(new Set([ownerA.route, ownerB.route]));
      await input.fill('quickdiscover');
      await expect(quick).toContainText('quickdiscover A 会话');
      await expect(quick).toContainText('quickdiscover B 会话');
      await lastStatus('搜索隔离 B · 文件 · 本次查询完成');
      gate.resolve();
      await released.promise;
      await page.waitForTimeout(250);
      expect(requests.filter(item => item.query === 'quick-concurrency')).toHaveLength(3);
      await expect.poll(() => cancelled.length).toBe(3);
      expect(cancelled.every(item => /ERR_ABORTED/.test(item.reason))).toBe(true);
      await expect(quick).toContainText('quickdiscover A 会话');
      await expect(quick).toContainText('quickdiscover B 会话');
    } finally { gate.resolve(); await page.unroute(pattern, delay); }

    // 原帮助及正文模式保留用户明确的集合，不能重新捕获当前活动文件的单一 owner。
    await input.fill('?');
    await quick.getByRole('option', { name: /^%,/ }).click();
    const beforeText = requests.length;
    await input.fill('%quickmodeword');
    await expect(quick).toContainText('mode scope A quickmodeword');
    await expect(quick).toContainText('mode scope B quickmodeword');
    const text = requests.slice(beforeText).filter(item => item.query === 'quickmodeword');
    expect([...new Set(text.map(item => item.route))].sort()).toEqual([ownerA.route, ownerB.route].sort());
    await input.fill('?');
    await quick.getByRole('option', { name: /^…,/ }).click();
    await input.fill('quickdiscover');
    await expect(quick).toContainText('quickdiscover B 会话');
    await lastStatus('搜索隔离 B · 文件 · 本次查询完成');
    await choose();
    await row(registered.backend_workspace_id).getByRole('checkbox').click();
    const beforeStopped = requests.length;
    await quick.getByRole('button', { name: 'OK', exact: true }).click();
    await expect(quick).toContainText('范围隔离 C 当前不可查询');
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
      helpKeepsScopes: true, boundedQueueCancelled: true, stoppedHasNoBusinessRequests: true, stoppedRoute: registered.workspace_id };
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
