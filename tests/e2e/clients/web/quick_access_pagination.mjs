import { unlink } from 'node:fs/promises';
import { join } from 'node:path';
import { expect } from 'playwright/test';

/** 原 Quick Input 的独立续页；历史由产品 API 创建，文件变化来自真实文件系统。 */
export async function verifyQuickAccessPagination({ page, command, api, result, owner, workspace }) {
  if (!workspace) throw new Error('文件分页验收缺少隔离工作区');
  const requests = [], responses = [], reads = [], errors = [];
  const quick = page.locator('.quick-input-widget'), input = quick.locator('input').first();
  const key = `${owner.gatewayId}/${owner.workspaceId}`;
  const status = category => quick.locator(`[data-quick-input-id="status-${key}/${category}"]`);
  const observe = request => {
    const url = new URL(request.url()), body = request.postDataJSON?.();
    const query = url.searchParams.get('query') ?? body?.query;
    if (!['pagingfiles', 'pagingcatalog'].includes(query)) return;
    requests.push({ path: url.pathname, route: request.headers()['x-boxteam-workspace-id'], query,
      cursor: url.searchParams.get('cursor') ?? body?.cursor, limit: Number(url.searchParams.get('limit') ?? body?.limit) });
  };
  const completed = request => {
    if (!requests.some(item => item.path === new URL(request.url()).pathname)) return;
    const url = new URL(request.url()), payload = request.postDataJSON?.();
    const query = url.searchParams.get('query') ?? payload?.query;
    if (!['pagingfiles', 'pagingcatalog'].includes(query)) return;
    reads.push((async () => {
      const response = await request.response(), body = await response.json();
      expect(body.request_id).toBeTruthy();
      expect(body.request_id).toBe(response.headers()['x-request-id']);
      responses.push({ path: url.pathname, query, status: response.status(), requestId: body.request_id,
        count: body.data?.entries?.length ?? body.data?.items?.length,
        cursor: body.data?.next_cursor ?? body.data?.cursor, elapsedMs: request.timing().responseEnd,
        bytes: (await request.sizes()).responseBodySize });
    })().catch(error => errors.push(String(error))));
  };
  const fileRequests = () => requests.filter(item => item.path === '/api/v1/resources/navigation');
  const scroll = async end => {
    const list = quick.getByRole('listbox');
    await list.hover();
    const slider = list.locator('.scrollbar.vertical .slider');
    if (!await slider.isVisible()) return;
    const thumb = await slider.boundingBox(), track = await list.locator('.scrollbar.vertical').boundingBox();
    await page.mouse.move(thumb.x + thumb.width / 2, thumb.y + thumb.height / 2);
    await page.mouse.down();
    await page.mouse.move(thumb.x + thumb.width / 2, end ? track.y + track.height - thumb.height / 2 : track.y + thumb.height / 2, { steps: 4 });
    await page.mouse.up();
  };
  const pageButton = async (category, name = '继续查询此类别') => {
    // 拖动原版滚动条访问虚拟列表末尾，不用键盘导航改选活动目标。
    await scroll(true);
    await expect(status(category)).toBeVisible();
    await status(category).hover();
    await status(category).getByRole('button', { name, exact: true }).click();
  };
  page.on('request', observe);
  page.on('requestfinished', completed);
  result.pagination = { requests, responses, errors };
  try {
    result.phase = 'file-explicit-pagination';
    await command('workbench.action.quickOpen', 'pagingfiles');
    await expect(quick).toContainText('pagingfiles-000.txt');
    await expect.poll(() => responses.filter(item => item.query === 'pagingfiles').length).toBe(3);
    expect(fileRequests()).toHaveLength(1);
    expect(responses.find(item => item.path === '/api/v1/resources/navigation').count).toBe(50);
    const first = quick.locator('.monaco-list-row.focused [data-quick-input-id]');
    const active = await first.getAttribute('data-quick-input-id');
    expect(active).toContain('/pagingfiles-000.txt');
    await pageButton('文件');
    await expect.poll(() => fileRequests().length).toBe(2);
    await expect.poll(() => responses.filter(item => item.path === '/api/v1/resources/navigation').length).toBe(2);
    await scroll(true);
    await expect(status('文件')).toContainText('本次查询完成');
    await scroll(false);
    await expect(first).toHaveAttribute('data-quick-input-id', active);
    expect(fileRequests()[0].cursor).toBeUndefined();
    expect(fileRequests()[1].cursor).toBeTruthy();
    expect(fileRequests().every(item => item.limit === 50 && item.route === owner.routeA)).toBe(true);
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
    await pageButton('文件', '重新查询此类别');
    await expect.poll(() => responses.filter(item => item.path === '/api/v1/resources/navigation').length).toBe(5);
    await scroll(true);
    await expect(status('文件')).toContainText('还有更多结果');
    await scroll(false);
    await expect(quick).toContainText('pagingfiles-001.txt');
    await expect(quick.locator('.monaco-list-row.focused [data-quick-input-id]')).toHaveCount(0);
    await input.press('Enter');
    expect(await page.locator('.tabs-container .tab').count()).toBe(tabs);
    expect(fileRequests().at(-1).cursor).toBeUndefined();
    expect(requests.filter(item => item.query === 'pagingfiles' && item.path !== '/api/v1/resources/navigation')).toHaveLength(4);
    result.pagination.stalePageCleared = true;
    result.pagination.explicitBranchRestart = true;
    await page.keyboard.press('Escape');

    result.phase = 'catalog-and-thread-independent-cursors';
    // 关键目录及 Thread 状态由真实创建入口产生，不预写 catalog／控制库。
    for (let index = 0; index < 55; index++) {
      await api('/api/v1/sessions', owner.routeA, { title: `pagingcatalog 会话 ${String(index).padStart(3, '0')}` });
    }
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
      const path = category === 'Thread' ? '/api/v1/session-threads/search' : '/api/v1/session-catalog/search';
      await expect.poll(() => responses.filter(item => item.query === 'pagingcatalog' && item.path === path).length).toBe(2);
      await revealStatus(category);
      await expect(status(category)).toContainText('本次查询完成');
    }
    await expect.poll(() => responses.filter(item => item.query === 'pagingcatalog').length).toBe(5);
    for (const path of ['/api/v1/session-catalog/search', '/api/v1/session-threads/search']) {
      const pages = requests.filter(item => item.query === 'pagingcatalog' && item.path === path);
      expect(pages).toHaveLength(2);
      expect(pages[0].cursor).toBeUndefined();
      expect(pages[1].cursor).toBeTruthy();
      const counts = responses.filter(item => item.query === 'pagingcatalog' && item.path === path).map(item => item.count);
      expect(counts).toEqual([50, 5]);
    }
    expect(requests.every(item => item.route === owner.routeA)).toBe(true);
    await Promise.all(reads);
    expect(errors).toEqual([]);
    result.pagination.explicitIndependentPages = true;
    result.pagination.visibleRows = await quick.locator('.monaco-list-row').count();
    await page.keyboard.press('Escape');
  } finally {
    page.off('request', observe);
    page.off('requestfinished', completed);
    await Promise.all(reads);
  }
}
