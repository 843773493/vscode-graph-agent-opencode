import { unlink } from 'node:fs/promises';
import { join } from 'node:path';
import { expect } from 'playwright/test';
import { searchRequest, searchReceipt } from '../../../support/workbench/search-observation.mjs';
import { scrollQuickInput } from '../../../support/workbench/quick-input.mjs';

/** 原 Quick Input 的独立续页；历史由产品 API 创建，文件变化来自真实文件系统。 */
export async function verifyQuickAccessPagination({ page, command, api, result, owner, workspace }) {
  if (!workspace) throw new Error('文件分页验收缺少隔离工作区');
  const requests = [], responses = [], reads = [], errors = [];
  const quick = page.locator('.quick-input-widget'), input = quick.locator('input').first();
  const key = `${owner.gatewayId}/${owner.workspaceId}`;
  const status = category => quick.locator(`[data-quick-input-id="status-${key}/${category}"]`);
  const wire = [], receipts = new Map();
  const branches = request => {
    const observed = searchRequest(request);
    if (observed?.path !== '/api/gateway/search') return [];
    const { body } = observed;
    return body.queries.flatMap(query => {
      const selected = body.continuations.length ? body.continuations.filter(item => item.branch.category === query.category)
        : observed.owners.map(workspace_id => ({ branch: { category: query.category, owner: { workspace_id } } }));
      return selected.map(item => ({ category: query.category, query: query.query, limit: query.limit,
        workspaceId: item.branch.owner.workspace_id, cursor: item.cursor }));
    });
  };
  const observe = request => {
    const observed = searchRequest(request);
    if (observed?.path !== '/api/gateway/search') return;
    wire.push(observed);
    if (['pagingfiles', 'pagingcatalog'].includes(observed.query)) requests.push(...branches(request));
  };
  const read = request => {
    if (!receipts.has(request)) {
      const pending = searchReceipt(request).then(receipt => {
        for (const branch of receipt.summary.branches) {
          const page = receipt.pages.find(event => event.branch.branch.category === branch.branch.category
            && event.branch.branch.owner.workspace_id === branch.branch.owner.workspace_id)?.page;
          const domain = page?.catalog ?? page?.threads ?? page?.files;
          responses.push({ query: receipt.query, category: branch.branch.category, workspaceId: branch.branch.owner.workspace_id,
            count: branch.returned_results, identities: domain?.items?.map(item => item.node?.node_id ?? item.thread_id),
            cursor: branch.cursor, state: branch.state, code: branch.code, reason: branch.reason, errorOwner: branch.error_owner,
            httpStatus: branch.http_status, gatewayStatus: receipt.status, requestId: receipt.requestId,
            domain, elapsedMs: request.timing().responseEnd });
        }
        return receipt;
      });
      receipts.set(request, pending);
      reads.push(pending.catch(error => errors.push(String(error))));
    }
    return receipts.get(request);
  };
  const completed = request => {
    if (searchRequest(request)?.path === '/api/gateway/search') void read(request);
  };
  const fileRequests = () => requests.filter(item => item.category === 'file');
  const scroll = end => scrollQuickInput(page, quick, end);
  const pageButton = async (category, name = '继续查询此类别') => {
    // 原滚轮访问虚拟列表末尾，不用鼠标按下或键盘导航改选活动目标。
    await scroll(true);
    await expect(status(category)).toBeVisible();
    await status(category).hover();
    await status(category).getByRole('button', { name, exact: true }).click();
  };
  page.on('request', observe);
  page.on('requestfinished', completed);
  result.pagination = { requests, wire, responses, errors };
  try {
    result.phase = 'file-explicit-pagination';
    await command('workbench.action.quickOpen', 'pagingfiles');
    await expect(quick).toContainText('pagingfiles-000.txt');
    await expect.poll(() => responses.filter(item => item.query === 'pagingfiles').length).toBe(3);
    expect(fileRequests()).toHaveLength(1);
    expect(responses.find(item => item.category === 'file').count).toBe(50);
    const first = quick.locator('.monaco-list-row.focused [data-quick-input-id]');
    const active = await first.getAttribute('data-quick-input-id');
    expect(active).toContain('/pagingfiles-000.txt');
    await pageButton('文件');
    await expect.poll(() => fileRequests().length).toBe(2);
    await expect.poll(() => responses.filter(item => item.category === 'file').length).toBe(2);
    await scroll(true);
    await expect(status('文件')).toContainText('本次查询完成');
    await scroll(false);
    await expect(first).toHaveAttribute('data-quick-input-id', active);
    expect(fileRequests()[0].cursor).toBeUndefined();
    expect(fileRequests()[1].cursor).toBeTruthy();
    expect(fileRequests().every(item => item.limit === 50 && item.workspaceId === owner.workspaceId)).toBe(true);
    // 初始名称与文件各一次 HTTP，随后仅继续文件分支；分支计量不冒充请求数。
    expect(wire.filter(item => item.query === 'pagingfiles')).toHaveLength(3);
    result.pagination.selectionPreserved = true;

    result.phase = 'file-stale-cursor-and-explicit-restart';
    await page.keyboard.press('Escape');
    await command('workbench.action.quickOpen', 'pagingfiles');
    await expect(quick).toContainText('pagingfiles-000.txt');
    await expect.poll(() => fileRequests().length).toBe(3);
    await unlink(join(workspace, 'pagingfiles-000.txt'));
    const tabs = await page.locator('.tabs-container .tab').count();
    await pageButton('文件');
    await expect(status('文件')).toContainText('查询失败');
    await expect(quick.locator('[data-quick-input-id^="boxteam-file:"]')).toHaveCount(0);
    const failed = fileRequests().at(-1);
    expect(failed.cursor).toBeTruthy();
    await expect.poll(() => responses.filter(item => item.category === 'file').length).toBe(4);
    const rejection = responses.filter(item => item.category === 'file').at(-1);
    expect(rejection).toMatchObject({ state: 'failed', code: 'execution_failed', reason: 'stale_cursor',
      errorOwner: 'resource_file_operations', httpStatus: 400, count: 0, cursor: failed.cursor });
    await pageButton('文件', '重新查询此类别');
    await expect.poll(() => responses.filter(item => item.category === 'file').length).toBe(5);
    await scroll(true);
    expect(responses.filter(item => item.category === 'file').at(-1)).toMatchObject({ state: 'limited', code: 'result_limit', count: 50 });
    await expect(status('文件')).toContainText('本次查询覆盖受限（result_limit）');
    await status('文件').hover();
    await expect(status('文件').getByRole('button', { name: '继续查询此类别', exact: true })).toBeVisible();
    await scroll(false);
    await expect(quick).toContainText('pagingfiles-001.txt');
    await expect(quick.locator('.monaco-list-row.focused [data-quick-input-id]')).toHaveCount(0);
    await input.press('Enter');
    expect(await page.locator('.tabs-container .tab').count()).toBe(tabs);
    expect(fileRequests().at(-1).cursor).toBeUndefined();
    expect(requests.filter(item => item.query === 'pagingfiles' && item.category !== 'file')).toHaveLength(4);
    result.pagination.stalePageCleared = true;
    result.pagination.explicitBranchRestart = true;
    await page.keyboard.press('Escape');

    result.phase = 'catalog-and-thread-independent-cursors';
    // 关键目录及 Thread 状态由真实创建入口产生，不预写 catalog／控制库。
    const sessions = [];
    for (let index = 0; index < 55; index++) sessions.push(
      await api('/api/v1/sessions', owner.routeA, { title: `pagingcatalog 会话 ${String(index).padStart(3, '0')}` }));
    await command('workbench.action.quickOpen', 'pagingcatalog');
    await expect(quick).toContainText('pagingcatalog 会话');
    await expect.poll(() => requests.filter(item => item.query === 'pagingcatalog').length).toBe(3);
    await expect.poll(() => responses.filter(item => item.query === 'pagingcatalog').length).toBe(3);
    const revealStatus = async category => {
      // 分隔组的状态位于文件组之前，原键盘可到达任意行，不依赖 DOM 全部挂载。
      await command('quickInput.last');
      for (let step = 0; step < 110 && !await status(category).isVisible(); step++) {
        await command('quickInput.previous');
      }
      await expect(status(category)).toBeVisible();
    };
    for (const category of ['Thread', '会话']) {
      await revealStatus(category);
      await status(category).hover();
      await status(category).getByRole('button', { name: '继续查询此类别', exact: true }).click();
      const domain = category === 'Thread' ? 'thread' : 'catalog';
      await expect.poll(() => responses.filter(item => item.query === 'pagingcatalog' && item.category === domain).length).toBe(2);
      await revealStatus(category);
      await expect(status(category)).toContainText('本次查询完成');
    }
    await expect.poll(() => responses.filter(item => item.query === 'pagingcatalog').length).toBe(5);
    for (const domain of ['catalog', 'thread']) {
      const pages = requests.filter(item => item.query === 'pagingcatalog' && item.category === domain);
      expect(pages).toHaveLength(2);
      expect(pages[0].cursor).toBeUndefined();
      expect(pages[1].cursor).toBeTruthy();
      const counts = responses.filter(item => item.query === 'pagingcatalog' && item.category === domain).map(item => item.count);
      expect(counts).toEqual([50, 5]);
    }
    expect(requests.every(item => item.workspaceId === owner.workspaceId)).toBe(true);
    expect(wire.every(item => item.owners.length === 1 && item.owners[0] === owner.workspaceId)).toBe(true);
    await Promise.all(reads);
    expect(errors).toEqual([]);
    result.pagination.explicitIndependentPages = true;
    result.pagination.visibleRows = await quick.locator('.monaco-list-row').count();
    await page.keyboard.press('Escape');

    result.phase = 'fuzzy-catalog-recall-outside-first-page';
    const firstIds = responses.find(item => item.query === 'pagingcatalog' && item.category === 'catalog').identities;
    expect(firstIds).toHaveLength(50);
    const outside = sessions.findIndex(session => !firstIds.includes(session.session_id));
    expect(outside).toBeGreaterThanOrEqual(0);
    const target = sessions[outside], query = `pgcg${String(outside).padStart(3, '0')}`;
    const waitSearch = async (category, filter, cursor) => {
      const request = await page.waitForRequest(request => branches(request).some(branch => branch.category === category
        && branch.query === filter && Boolean(branch.cursor) === cursor && branch.workspaceId === owner.workspaceId));
      const receipt = await read(request);
      return { receipt, branch: receipt.summary.branches.find(item => item.branch.category === category),
        page: receipt.pages.find(item => item.page.category === category)?.page };
    };
    const fuzzy = await Promise.all([waitSearch('catalog', query, false), waitSearch('thread', query, false),
      command('workbench.action.quickOpen', query)]);
    const fuzzyReceipts = fuzzy.slice(0, 2).map(({ receipt, page }) => {
      expect((page.catalog ?? page.threads).items.map(item => item.node?.session_id ?? item.session_id)).toEqual([target.session_id]);
      return { category: page.category, requestId: receipt.requestId };
    });
    await expect(quick.locator(`[data-quick-input-id="session:${key}/${target.session_id}"]`)).toHaveCount(1);
    const main = await api(`/api/v1/sessions/${target.session_id}/main-thread`, owner.routeA);
    await expect(quick.locator(`[data-quick-input-id$="/${main.thread_id}"]`)).toHaveCount(1);
    await page.keyboard.press('Escape');

    result.phase = 'catalog-stale-cursor-clears-only-own-branch';
    const [firstPage] = await Promise.all([waitSearch('catalog', 'pagingcatalog', false),
      command('workbench.action.quickOpen', 'pagingcatalog')]);
    expect(firstPage.page.catalog.total).toBe(55);
    expect(firstPage.branch.cursor).toBeTruthy();
    // 产品创建入口推进 catalog revision，原 cursor 必须拒绝，不能拼入新的目录快照。
    await api('/api/v1/sessions', owner.routeA, { title: 'catalog cursor revision trigger' });
    await revealStatus('会话');
    await status('会话').hover();
    const [stale] = await Promise.all([waitSearch('catalog', 'pagingcatalog', true),
      status('会话').getByRole('button', { name: '继续查询此类别', exact: true }).click()]);
    expect(stale.receipt.status).toBe(200);
    expect(stale.branch.state).toBe('failed');
    expect(stale.branch.http_status).toBe(409);
    expect(stale.branch.code).toBe('stale_catalog_cursor');
    expect(stale.branch.reason).toBe('revision');
    expect(stale.branch.error_owner).toBe('session_catalog');
    expect(stale.branch.cursor).toBe(firstPage.branch.cursor);
    expect(stale.branch.returned_results).toBe(0);
    expect(stale.receipt.pages).toEqual([]);
    // 原页移除后状态行前移，重新通过原列表导航将其挂载。
    await revealStatus('会话');
    await expect(status('会话')).toContainText('查询失败');
    await expect(quick.locator('[data-quick-input-id^="session:"]')).toHaveCount(0);
    await status('会话').hover();
    const [restarted] = await Promise.all([waitSearch('catalog', 'pagingcatalog', false),
      status('会话').getByRole('button', { name: '重新查询此类别', exact: true }).click()]);
    expect(restarted.branch.state).toBe('limited');
    expect(restarted.page.catalog.items).toHaveLength(50);
    await page.keyboard.press('Escape');

    result.phase = 'catalog-complete-empty-is-not-limited';
    const absent = 'catalog_absent_' + crypto.randomUUID().replaceAll('-', '');
    const [empty] = await Promise.all([waitSearch('catalog', absent, false),
      command('workbench.action.quickOpen', 'session ' + absent)]);
    expect(empty.branch.state).toBe('complete');
    expect(empty.page.catalog.items).toEqual([]);
    expect(empty.page.catalog.total).toBe(0);
    expect(empty.branch.cursor).toBeNull();
    await expect(status('会话')).toContainText('没有匹配的资源');
    result.discovery = { outsideFirstPage: true, exactSessionAndMain: true, query, target: target.session_id, fuzzyReceipts,
      staleCatalogCursor: { status: stale.branch.http_status, code: stale.branch.code, reason: stale.branch.reason, requestId: stale.receipt.requestId },
      restartedFromFirstPage: restarted.branch.state === 'limited', completeEmpty: true };
    await page.keyboard.press('Escape');
    await Promise.all(reads);
    expect(errors).toEqual([]);
  } finally {
    page.off('request', observe);
    page.off('requestfinished', completed);
    await Promise.all(reads);
  }
}
