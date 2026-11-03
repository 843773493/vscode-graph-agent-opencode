import { expect } from 'playwright/test';
import { readFile } from 'node:fs/promises';
import { join } from 'node:path';
import { verifySourceDocuments } from './quick_access_source_documents.mjs';

/** 规范链接经过真实原 Opener，关键状态仍由本场景的产品 API 创建。 */
export async function verifyResourceLinks({ page, command, api, result, owner }) {
  const quick = page.locator('.quick-input-widget');
  const links = { requests: [], receipts: [], byteReads: [] };
  result.resourceLinks = links;
  const observe = request => {
    if (new URL(request.url()).pathname === '/api/gateway/resource-addresses/parse') links.requests.push(request.postDataJSON());
  };
  page.on('request', observe);
  const observeBytes = response => {
    const request = response.request(), url = new URL(request.url());
    if (url.pathname !== '/api/v1/workspace/files/raw') return;
    links.byteReads.push({ uri: url.searchParams.get('path'), route: request.headers()['x-boxteam-workspace-id'],
      range: request.headers().range ?? null, status: response.status(), requestId: response.headers()['x-request-id'] });
  };
  page.on('response', observeBytes);
  const open = async (uri, expected = 200, modifiers = []) => {
    await expect(quick).not.toBeVisible();
    await command('workbench.action.quickOpen', uri);
    await expect(quick.locator('input').first()).toHaveValue(uri);
    const [response] = await Promise.all([
      page.waitForResponse(response => new URL(response.url()).pathname === '/api/gateway/resource-addresses/parse'
        && response.request().postDataJSON()?.uri === uri),
      quick.getByText('打开链接', { exact: true }).click({ modifiers }),
    ]);
    const body = await response.json();
    expect(response.status()).toBe(expected);
    expect(body.request_id).toBe(response.headers()['x-request-id']);
    links.receipts.push({ uri, status: response.status(), requestId: body.request_id });
    return body.data;
  };
  const thread = id => page.locator(`section.boxteam-thread[data-workspace-id="${owner.workspaceId}"][data-session-id="${owner.sessionId}"][data-thread-id="${id}"]`);
  try {
    result.phase = 'canonical-child-uri';
    const pageOfThreads = await api(`/api/v1/session-threads/search?query=${owner.childId}`, owner.routeA);
    const childUri = pageOfThreads.items.find(item => item.thread_id === owner.childId)?.thread_uri;
    expect(childUri).toBeTruthy();
    await command('workbench.action.closeAllEditors');
    await expect(thread(owner.childId)).toHaveCount(0);
    const child = await open(childUri);
    expect(child.thread_id).toBe(owner.childId);
    await expect(thread(owner.childId).getByTestId('composer-input')).toBeVisible({ timeout: 30_000 });
    expect(await thread(owner.mainId).count()).toBe(0);
    links.childKeepsIdentity = true;

    result.phase = 'canonical-session-uri';
    const sessionUri = childUri.slice(0, childUri.lastIndexOf('/threads/'));
    const session = await open(sessionUri, 200, ['Alt']);
    expect(session.thread_id).toBeNull();
    await expect(thread(owner.mainId).getByTestId('composer-input')).toBeVisible({ timeout: 30_000 });
    await expect(thread(owner.childId)).toBeVisible();
    expect(await thread(owner.mainId).evaluate(element => element.closest('.editor-group-container') === null)).toBe(false);
    expect(await thread(owner.mainId).evaluate((element, id) => element.closest('.editor-group-container')
      !== document.querySelector(`section.boxteam-thread[data-thread-id="${id}"]`)?.closest('.editor-group-container'), owner.childId)).toBe(true);
    links.sessionUsesMainAndSideGroup = true;

    result.phase = 'canonical-file-uri';
    const roots = await api('/api/v1/workspace/files/roots', owner.routeA);
    const fileUri = roots.workspace_uri + '/uri%40target.txt';
    await open(fileUri);
    const editor = page.locator('.editor-instance .monaco-editor').filter({ visible: true });
    await expect(editor).toBeVisible();
    await expect(editor.locator('.view-lines')).toContainText('URI link A');
    await expect(editor.locator('.view-lines')).not.toContainText('URI link B');
    links.fileUsesOriginalProjection = true;

    result.phase = 'canonical-image-uri';
    await open(roots.workspace_uri + '/uri-image.png');
    const image = page.locator('.image-preview-editor').filter({ visible: true }).locator('img');
    await expect(image).toBeVisible();
    await expect.poll(() => image.evaluate(element => [element.naturalWidth, element.naturalHeight])).toEqual([1, 1]);
    links.imageDecoded = true;

    result.phase = 'canonical-real-http-range';
    const ranged = await page.evaluate(async ({ uri, route }) => {
      const credential = await (await fetch('/api/gateway/auth/local-credential')).json();
      const query = new URLSearchParams({ path: uri, scope: 'workspace' });
      const response = await fetch(`/api/v1/workspace/files/raw?${query}`, {
        headers: { 'X-Local-Token': credential.data.token, 'X-BoxTeam-Workspace-Id': route, Range: 'bytes=1-3' },
      });
      return { status: response.status, bytes: [...new Uint8Array(await response.arrayBuffer())],
        range: response.headers.get('content-range'), requestId: response.headers.get('x-request-id') };
    }, { uri: roots.workspace_uri + '/uri-image.png', route: owner.routeA });
    expect(ranged.status).toBe(206);
    expect(ranged.bytes).toEqual([80, 78, 71]);
    expect(ranged.range).toMatch(/^bytes 1-3\/\d+$/);
    expect(ranged.requestId).toBeTruthy();
    links.realHttpRange = ranged;

    result.phase = 'canonical-text-raw-byte-save';
    await open(roots.workspace_uri + '/uri-bom.txt');
    const text = page.locator('.editor-instance .monaco-editor').filter({ visible: true });
    await expect(text.locator('.view-lines')).toContainText('BOM original');
    await text.locator('[role="textbox"]').focus();
    await page.keyboard.press('ControlOrMeta+A');
    await page.keyboard.insertText('BOM saved');
    const [saved] = await Promise.all([
      page.waitForResponse(response => new URL(response.url()).pathname === '/api/v1/workspace/files/content'
        && response.request().method() === 'PUT'),
      command('workbench.action.files.save'),
    ]);
    expect(saved.status()).toBe(200);
    const savedBytes = await readFile(join(process.env.BOXTEAM_CATALOG_WORKSPACE, 'uri-bom.txt'));
    expect(Array.from(savedBytes.subarray(0, 3))).toEqual([239, 187, 191]);
    expect(savedBytes.subarray(3).toString()).toBe('BOM saved');
    links.textSavePreservesBom = true;
    expect(links.byteReads.every(read => read.route === owner.routeA && read.requestId)).toBe(true);

    await verifySourceDocuments({ page, command, api, result, owner, open });
    // 恢复 A 的文件编辑器，后续专用模式继续验证原发起工作区。
    await open(fileUri);
    await expect(page.locator('.editor-group-container.active .view-lines')).toContainText('URI link A');

    result.phase = 'invalid-canonical-uri';
    const tabs = await page.locator('.tabs-container .tab').count();
    const groups = await page.locator('.editor-group-container').count();
    await open(fileUri + '#L2', 400, ['Alt']);
    await expect(page.locator('.notifications-toasts')).toContainText('fragment');
    expect(await page.locator('.tabs-container .tab').count()).toBe(tabs);
    expect(await page.locator('.editor-group-container').count()).toBe(groups);
    expect(links.requests.every(item => item.local_gateway_id === owner.gatewayId)).toBe(true);
    links.invalidUriIsNotRewritten = true;
    links.noExecution = result.forbidden.length === 0;
    expect(links.noExecution).toBe(true);
  } finally { page.off('request', observe); page.off('response', observeBytes); }
}
