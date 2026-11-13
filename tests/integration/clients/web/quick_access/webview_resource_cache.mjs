// Integration：原 Markdown 扩展及隔离 Webview，真实 Gateway／FileService，无响应替身。
import { chmod, mkdir, readFile, stat, unlink, writeFile } from 'node:fs/promises';
import { dirname, join } from 'node:path';
import { pathToFileURL } from 'node:url';
import { chromium } from 'playwright';
import { expect } from 'playwright/test';
import { TestRunContext } from '../../../../harness/js/run-context.mjs';

const required = name => {
  if (!process.env[name]) throw new Error(`缺少环境变量 ${name}`);
  return process.env[name];
};
export async function runWebviewResourceCache({ run, launch }) {
  const fixture = JSON.parse(required('BOXTEAM_WEBVIEW_FIXTURE'));
  const artifacts = dirname(required('BOXTEAM_BROWSER_RESULT_PATH'));
  const result = { classification: 'Integration', boundary: '真实 Gateway、原 Markdown 预览和官方隔离 Worker；无响应替身', phase: 'initialize', routeStubs: [],
    pageErrors: [], consoleErrors: [], failedRequests: [], requests: [], responses: [], cacheEntries: [], frames: [] };
  const selectedHeaders = headers => Object.fromEntries(Object.entries(headers).filter(([name]) =>
    ['if-none-match', 'etag', 'cache-control', 'content-type', 'content-length', 'x-request-id',
      'x-boxteam-target-gateway-id', 'x-boxteam-workspace-id'].includes(name.toLowerCase())));
  let page, failure;
  try {
    const usePage = nextPage => {
      page = nextPage;
      page.on('pageerror', error => result.pageErrors.push({
        phase: result.phase, message: String(error), stack: error.stack,
      }));
      page.on('console', message => {
        if (message.type() === 'error') result.consoleErrors.push({ text: message.text(), location: message.location() });
      });
      page.context().on('requestfailed', request => result.failedRequests.push({
        phase: result.phase, url: request.url(), failure: request.failure(),
      }));
      page.on('framenavigated', frame => result.frames.push({ phase: result.phase, url: frame.url() }));
      page.context().on('request', request => {
        const url = new URL(request.url());
        if (url.pathname === '/api/v1/workspace/files/raw' || url.pathname.endsWith('/resources/navigation')) {
          result.requests.push({ phase: result.phase, url: request.url(), method: request.method(),
            headers: selectedHeaders(request.headers()), body: request.postData() });
        }
      });
      page.context().on('response', response => {
        const url = response.url();
        if (!url.includes('/api/v1/workspace/files/raw') && !url.includes('.vscode-resource.')) return;
        result.responses.push({ phase: result.phase, url, status: response.status(),
          fromServiceWorker: response.fromServiceWorker(), headers: selectedHeaders(response.headers()) });
      });
    };
    const host = await launch({ run, result, usePage });
    const ready = async () => {
      if (page.url() !== host.baseUrl + '/') await page.goto(host.baseUrl, { waitUntil: 'domcontentloaded' });
      await expect(page.locator('.native-session-navigation')).toBeVisible({ timeout: 45000 });
    };
    const command = (id, ...args) => page.evaluate(async ({ id, args }) => {
      await globalThis.workbench.commands.executeCommand(id, ...args);
    }, { id, args });
    await ready();
    const uri = await page.evaluate(fixture => globalThis.workbench.URI.from({ scheme: 'boxteam-file',
      authority: fixture.gatewayId,
      path: `/${fixture.workspaceId}/${encodeURIComponent(fixture.rootUri)}/webview-cache/preview.md` }).toString(), fixture);
    result.phase = 'open-original-preview';
    await command('_workbench.open', uri, [undefined, { pinned: true }]);
    await command('markdown.showPreview');
    const probe = () => page.frameLocator('iframe.webview:visible').frameLocator('iframe#active-frame').locator('.cache-probe');
    await expect(probe()).toHaveText('原文件 CSS 与字体', { timeout: 60000 });
    await expect(probe()).toHaveCSS('color', 'rgb(17, 34, 51)');
    await expect(probe()).toHaveCSS('background-color', 'rgb(90, 91, 92)');
    await probe().evaluate(async element => {
      await element.ownerDocument.fonts.load('16px CacheProbe');
      if (!element.ownerDocument.fonts.check('16px CacheProbe')) throw new Error('原 Webview 字体未加载');
    });
    for (const resource of ['markdown-math/notebook-out/katex.min.css',
      'markdown-math/preview-styles/index.css', 'mermaid-markdown-features/markdown-preview-out/index.js']) {
      await expect.poll(() => result.responses.some(item => item.phase === 'open-original-preview'
        && item.url.includes(`/extensions/${resource}`) && item.fromServiceWorker && item.status === 200)).toBe(true);
    }
    expect(result.failedRequests.filter(item => new URL(item.url).pathname.startsWith('/extensions/'))).toEqual([]);
    const cssUri = fixture.rootUri + '/webview-cache/style.css';
    const fontUri = fixture.rootUri + '/webview-cache/font.ttf';
    const retainedUri = fixture.rootUri + '/webview-cache/retained.css';
    const raw = uri => result.requests.filter(item => new URL(item.url).pathname === '/api/v1/workspace/files/raw'
      && new URL(item.url).searchParams.get('path') === uri);
    expect(raw(cssUri).length).toBeGreaterThan(0);
    expect(raw(fontUri).length).toBeGreaterThan(0);
    const outer = () => page.frames().find(frame => frame.url().includes('.vscode-cdn.net/') && frame.url().includes('/index.html?'));
    expect(outer()).toBeTruthy();
    const inspectCache = () => outer().evaluate(async () => {
      const entries = [];
      for (const name of await caches.keys()) {
        const cache = await caches.open(name);
        for (const request of await cache.keys()) {
          const response = await cache.match(request);
          entries.push({ name, url: request.url, etag: response.headers.get('etag'),
            mime: response.headers.get('content-type'), bytes: (await response.arrayBuffer()).byteLength });
        }
      }
      return entries;
    });
    await expect.poll(async () => (await inspectCache()).some(item =>
      item.url.includes('/webview-cache/style.css') && item.etag && item.bytes > 0)).toBe(true);
    await expect.poll(async () => (await inspectCache()).some(item =>
      item.url.includes('/webview-cache/font.ttf') && item.etag && item.bytes > 0)).toBe(true);
    await expect.poll(async () => (await inspectCache()).some(item =>
      item.url.includes('/webview-cache/retained.css') && item.etag && item.bytes > 0)).toBe(true);
    result.cacheEntries = await inspectCache();
    expect(result.cacheEntries.find(item => item.url.includes('/webview-cache/font.ttf'))?.mime).toBe('font/ttf');
    result.phase = 'warm-preview';
    const before = { css: raw(cssUri).length, font: raw(fontUri).length };
    await command('markdown.preview.refresh');
    await expect(probe()).toHaveCSS('color', 'rgb(17, 34, 51)');
    await expect.poll(() => result.responses.some(item => item.phase === 'warm-preview'
      && item.url.includes('/webview-cache/style.css') && item.fromServiceWorker)).toBe(true);
    await probe().evaluate(element => element.ownerDocument.fonts.load('16px CacheProbe').then(() => undefined));
    expect(raw(cssUri)).toHaveLength(before.css);
    expect(raw(fontUri)).toHaveLength(before.font);
    expect(result.requests.some(item => item.phase === 'warm-preview' && item.body
      && JSON.parse(item.body).uri === cssUri && JSON.parse(item.body).operation === 'describe')).toBe(true);
    const beforeRestart = { css: raw(cssUri).length, font: raw(fontUri).length, retained: raw(retainedUri).length };
    const entriesBeforeRestart = await inspectCache();
    await command('workbench.action.closeAllEditors');
    result.phase = 'restart-preview';
    await host.reload();
    await ready();
    await command('_workbench.open', uri, [undefined, { pinned: true }]);
    await command('markdown.showPreview');
    await expect(probe()).toHaveText('原文件 CSS 与字体', { timeout: 60000 });
    await expect(probe()).toHaveCSS('color', 'rgb(17, 34, 51)');
    await expect(probe()).toHaveCSS('background-color', 'rgb(90, 91, 92)');
    await probe().evaluate(element => element.ownerDocument.fonts.load('16px CacheProbe').then(() => undefined));
    await expect.poll(() => result.responses.some(item => item.phase === 'restart-preview'
      && item.url.includes('/webview-cache/style.css') && item.fromServiceWorker && item.status === 200)).toBe(true);
    expect(raw(cssUri)).toHaveLength(beforeRestart.css);
    expect(raw(fontUri)).toHaveLength(beforeRestart.font);
    expect(raw(retainedUri)).toHaveLength(beforeRestart.retained);
    expect(result.requests.some(item => item.phase === 'restart-preview' && item.body
      && JSON.parse(item.body).uri === cssUri && JSON.parse(item.body).operation === 'describe')).toBe(true);
    const entriesAfterRestart = await inspectCache();
    expect(entriesAfterRestart).toEqual(expect.arrayContaining(entriesBeforeRestart));
    result.restart = { origin: new URL(page.url()).origin, before: beforeRestart,
      after: { css: raw(cssUri).length, font: raw(fontUri).length, retained: raw(retainedUri).length },
      entriesBefore: entriesBeforeRestart, entriesAfter: entriesAfterRestart };
    result.phase = 'updated-preview';
    const style = join(fixture.directory, 'style.css');
    await writeFile(style, (await readFile(style, 'utf8')).replace('rgb(17, 34, 51)', 'rgb(61, 72, 83)'));
    await command('markdown.preview.refresh');
    await expect(probe()).toHaveCSS('color', 'rgb(61, 72, 83)');
    expect(raw(cssUri).length).toBeGreaterThan(before.css);
    result.phase = 'evicted-resource-entries';
    const originalEtag = result.cacheEntries.find(item => item.url.includes('/webview-cache/style.css')).etag;
    await expect.poll(async () => {
      const current = (await inspectCache()).find(item => item.url.includes('/webview-cache/style.css'));
      return Boolean(current?.etag && current.etag !== originalEtag);
    }).toBe(true);
    const stored = await inspectCache();
    const evicted = stored.filter(item => item.url.includes('/webview-cache/style.css')
      || item.url.includes('/webview-cache/font.ttf'));
    expect(evicted).toHaveLength(2);
    // 只模拟引擎丢弃两条可再生资源；其它扩展资源与工作台存储均保持原件。
    const removed = await outer().evaluate(async entries => {
      const removed = [];
      for (const entry of entries) {
        if (await (await caches.open(entry.name)).delete(entry.url)) removed.push(entry.url);
      }
      return removed;
    }, evicted);
    expect(removed).toEqual(evicted.map(item => item.url));
    const retained = stored.filter(item => !removed.includes(item.url));
    expect(retained.some(item => item.url.includes('/webview-cache/retained.css') && item.bytes > 0)).toBe(true);
    const afterEviction = await inspectCache();
    expect(afterEviction).toEqual(expect.arrayContaining(retained));
    expect(afterEviction.some(item => removed.includes(item.url))).toBe(false);
    const beforeEviction = { css: raw(cssUri).length, font: raw(fontUri).length, retained: raw(retainedUri).length };
    await command('markdown.preview.refresh');
    await expect(probe()).toHaveCSS('color', 'rgb(61, 72, 83)');
    await expect(probe()).toHaveCSS('background-color', 'rgb(90, 91, 92)');
    await probe().evaluate(element => element.ownerDocument.fonts.load('16px CacheProbe').then(() => undefined));
    await expect.poll(() => raw(cssUri).length).toBeGreaterThan(beforeEviction.css);
    await expect.poll(() => raw(fontUri).length).toBeGreaterThan(beforeEviction.font);
    await expect.poll(async () => (await inspectCache()).filter(item => removed.includes(item.url))
      .filter(item => item.etag && item.bytes > 0).length).toBe(2);
    expect(raw(retainedUri)).toHaveLength(beforeEviction.retained);
    expect(await inspectCache()).toEqual(expect.arrayContaining(retained));
    result.eviction = { removed, retained, restored: (await inspectCache()).filter(item => removed.includes(item.url)) };
    result.phase = 'deleted-cached-resource';
    const savedStyle = await readFile(style, 'utf8');
    const cachedStyle = (await inspectCache()).find(item => item.url.includes('/webview-cache/style.css'));
    expect(cachedStyle?.etag).toBeTruthy();
    const beforeDeletion = raw(cssUri).length;
    try {
      await unlink(style);
      const rejected = page.waitForResponse(response => response.status() === 404
        && new URL(response.url()).pathname.endsWith('/resources/navigation')
        && response.request().postDataJSON()?.uri === cssUri);
      await command('markdown.preview.refresh');
      const rejection = await rejected;
      const rejectedHeaders = await rejection.allHeaders();
      const rejectedBody = await rejection.json();
      expect(rejectedHeaders['x-request-id']).toBeTruthy();
      expect(rejectedBody.request_id).toBe(rejectedHeaders['x-request-id']);
      expect(rejectedBody.detail).toMatchObject({ code: 'file_not_found', reason: 'not_found' });
      await expect.poll(() => result.responses.some(item => item.phase === 'deleted-cached-resource'
        && item.url.includes('/webview-cache/style.css') && item.fromServiceWorker && item.status === 404)).toBe(true);
      await expect(probe()).not.toHaveCSS('color', 'rgb(61, 72, 83)');
      await expect(probe()).toHaveCSS('background-color', 'rgb(90, 91, 92)');
      expect(raw(cssUri)).toHaveLength(beforeDeletion);
      const afterDeletion = await inspectCache();
      expect(afterDeletion).toContainEqual(cachedStyle);
      result.deletion = { cached: cachedStyle, rejection: { status: rejection.status(),
        headers: selectedHeaders(rejectedHeaders), body: rejectedBody },
        cachedAfterFailure: afterDeletion.find(item => item.url === cachedStyle.url),
        rawReadsBefore: beforeDeletion, rawReadsAfter: raw(cssUri).length };
    } finally {
      await writeFile(style, savedStyle + '\n.cache-probe { color: rgb(101, 112, 123); }\n');
    }
    result.phase = 'restored-cached-resource';
    await command('markdown.preview.refresh');
    await expect(probe()).toHaveCSS('color', 'rgb(101, 112, 123)');
    await expect.poll(() => raw(cssUri).length).toBeGreaterThan(beforeDeletion);
    await expect.poll(async () => (await inspectCache()).some(item => item.url === cachedStyle.url
      && item.etag && item.etag !== cachedStyle.etag && item.bytes > cachedStyle.bytes)).toBe(true);
    result.deletion.restored = (await inspectCache()).find(item => item.url === cachedStyle.url);
    if (process.platform !== 'win32' && process.getuid() !== 0) {
      result.phase = 'permission-denied-cached-resource';
      const mode = (await stat(style)).mode;
      const cachedBefore = (await inspectCache()).find(item => item.url === cachedStyle.url);
      expect(cachedBefore?.etag).toBeTruthy();
      const beforeDenied = raw(cssUri).length;
      result.permission = { cachedBefore, rawReadsBefore: beforeDenied };
      try {
        await chmod(style, 0);
        const denied = page.waitForResponse(response => response.status() === 400
          && new URL(response.url()).pathname.endsWith('/resources/navigation')
          && response.request().postDataJSON()?.uri === cssUri);
        await command('markdown.preview.refresh');
        const rejection = await denied;
        const body = await rejection.json(), headers = await rejection.allHeaders();
        expect(headers['x-request-id']).toBeTruthy();
        expect(body.request_id).toBe(headers['x-request-id']);
        expect(body.detail).toMatchObject({ owner: 'resource_navigation', type: 'PermissionError', errno: 13 });
        result.permission.rejection = { status: rejection.status(), headers: selectedHeaders(headers), body };
        await expect.poll(() => result.responses.some(item => item.phase === 'permission-denied-cached-resource'
          && item.url === cachedStyle.url && item.fromServiceWorker && item.status === 404)).toBe(true);
        await expect(probe()).not.toHaveCSS('color', 'rgb(101, 112, 123)');
        await expect(probe()).toHaveCSS('background-color', 'rgb(90, 91, 92)');
        const cachedDuring = (await inspectCache()).find(item => item.url === cachedStyle.url);
        expect(cachedDuring).toEqual(cachedBefore);
        result.permission.cachedDuring = cachedDuring;
        result.permission.rawReadsWhileDenied = raw(cssUri).length;
      } finally {
        await chmod(style, mode);
      }
      result.phase = 'permission-restored-cached-resource';
      await command('markdown.preview.refresh');
      await expect(probe()).toHaveCSS('color', 'rgb(101, 112, 123)');
      await expect.poll(() => result.responses.some(item => item.phase === 'permission-restored-cached-resource'
        && item.url === cachedStyle.url && item.fromServiceWorker && item.status === 200)).toBe(true);
      result.permission.cachedAfter = (await inspectCache()).find(item => item.url === cachedStyle.url);
      expect(result.permission.cachedAfter).toEqual(cachedBefore);
      result.permission.rawReadsAfterRecovery = raw(cssUri).length;
      expect(raw(cssUri)).toHaveLength(beforeDenied);
    } else {
      // TODO: Windows 原生 ACL 验收需由目标机器提供实际权限撤销场景。
      result.permission = { unsupported: '此场景使用非 root POSIX 原生文件权限；Windows ACL 需另行验收' };
    }
    const runtime = async action => {
      const receipt = await page.evaluate(async ({ fixture, action }) => {
        const credential = await (await fetch('/api/gateway/auth/local-credential')).json();
        let body;
        if (action === 'stop') {
          const workspaces = await (await fetch('/api/gateway/workspaces?check_health=false', {
            headers: { 'X-Local-Token': credential.data.token },
          })).json();
          const target = workspaces.data.items.find(item => item.gateway_id === fixture.gatewayId
            && item.workspace_id === fixture.workspaceId);
          // 保留原预览及文件 watcher，使用完整组强制停机主动结清它持有的流。
          body = { expected_backend_generation: target.backend_generation,
            expected_workspace_ids: target.affected_workspace_ids, desired_workspace_ids: [] };
        }
        const endpoint = action === 'stop' ? 'restart-force' : 'start';
        const response = await fetch(`/api/gateway/workspaces/${fixture.workspaceId}/runtime/${endpoint}`, {
          method: 'POST', headers: { 'X-Local-Token': credential.data.token,
            'Content-Type': 'application/json',
            'X-BoxTeam-Target-Gateway-Id': fixture.gatewayId, 'X-BoxTeam-Workspace-Id': fixture.workspaceId },
          body: body ? JSON.stringify(body) : undefined,
        });
        return { status: response.status, requestId: response.headers.get('X-Request-ID'), body: await response.json() };
      }, { fixture, action });
      (result.lifecycle ??= []).push({ action, ...receipt });
      expect(receipt.status, JSON.stringify(receipt)).toBe(200);
      expect(receipt.requestId).toBeTruthy();
      expect(receipt.body.request_id).toBe(receipt.requestId);
      expect(receipt.body.data.status).toBe(action === 'stop' ? 'stopped' : 'started');
      return receipt;
    };
    result.phase = 'disconnected-cached-resource';
    const beforeDisconnect = raw(cssUri).length;
    const cachedBeforeDisconnect = (await inspectCache()).find(item => item.url === cachedStyle.url);
    const stopped = await runtime('stop');
    const unavailable = page.waitForResponse(response => response.status() === 503
      && new URL(response.url()).pathname.endsWith('/resources/navigation')
      && response.request().postDataJSON()?.uri === cssUri);
    await command('markdown.preview.refresh');
    const rejection = await unavailable;
    const rejectionBody = await rejection.json(), rejectionHeaders = await rejection.allHeaders();
    expect(rejectionHeaders['x-request-id']).toBeTruthy();
    expect(rejectionBody.request_id).toBe(rejectionHeaders['x-request-id']);
    expect(rejectionBody.detail).toContain(fixture.workspaceId);
    await expect.poll(() => result.responses.some(item => item.phase === 'disconnected-cached-resource'
      && item.url === cachedStyle.url && item.fromServiceWorker && item.status === 404)).toBe(true);
    await expect(probe()).not.toHaveCSS('color', 'rgb(101, 112, 123)');
    expect(raw(cssUri)).toHaveLength(beforeDisconnect);
    const cachedWhileDisconnected = (await inspectCache()).find(item => item.url === cachedStyle.url);
    expect(cachedWhileDisconnected).toEqual(cachedBeforeDisconnect);
    result.disconnection = { stopped, cachedBefore: cachedBeforeDisconnect, cachedDuring: cachedWhileDisconnected,
      rejection: { status: rejection.status(), headers: selectedHeaders(rejectionHeaders), body: rejectionBody },
      rawReadsBefore: beforeDisconnect, rawReadsWhileDisconnected: raw(cssUri).length };
    result.phase = 'reconnected-cached-resource';
    result.disconnection.started = await runtime('start');
    await command('markdown.preview.refresh');
    await expect(probe()).toHaveCSS('color', 'rgb(101, 112, 123)');
    await expect.poll(() => result.responses.some(item => item.phase === 'reconnected-cached-resource'
      && item.url === cachedStyle.url && item.fromServiceWorker && item.status === 200)).toBe(true);
    expect(result.requests.some(item => item.phase === 'reconnected-cached-resource' && item.body
      && JSON.parse(item.body).uri === cssUri && JSON.parse(item.body).operation === 'describe')).toBe(true);
    expect(raw(cssUri)).toHaveLength(beforeDisconnect);
    result.disconnection.cachedAfter = (await inspectCache()).find(item => item.url === cachedStyle.url);
    expect(result.disconnection.cachedAfter).toEqual(cachedBeforeDisconnect);
    result.disconnection.rawReadsAfterReconnect = raw(cssUri).length;
    result.phase = 'credentials-revoke-visible-webview';
    const entriesBeforeRevocation = await inspectCache();
    const readsBeforeRevocation = raw(cssUri).length;
    let credentialRejection;
    const credentialPattern = '**/api/gateway/workspaces?check_health=false';
    const reject = async route => {
      // 只改变这次真实请求的凭据；401及request_id仍由 Gateway 产生。
      const response = await route.fetch({ headers: { ...route.request().headers(),
        'x-local-token': 'expired-webview-cache-credential' } });
      const body = await response.json();
      credentialRejection = { status: response.status(), detail: body.detail, requestId: body.request_id,
        header: response.headers()['x-request-id'] };
      expect(credentialRejection.status).toBe(401);
      expect(credentialRejection.detail).toBe('invalid local token');
      expect(credentialRejection.requestId).toBeTruthy();
      expect(credentialRejection.requestId).toBe(credentialRejection.header);
      await route.fulfill({ response });
    };
    await page.route(credentialPattern, reject, { times: 1 });
    try {
      const action = await command('boxteam.workspace.activate', {
        gatewayId: fixture.gatewayId, workspaceId: fixture.workspaceId,
      }).then(() => ({ succeeded: true }), cause => ({ error: String(cause) }));
      expect(action.error).toContain('Gateway 或访问凭据已改变');
      expect(credentialRejection).toBeTruthy();
      await expect(page.locator('iframe.webview')).toHaveCount(0);
      await expect(page.locator('.notifications-toasts')).toContainText('Gateway 或访问凭据已改变，请重新打开预览。');
    } finally { await page.unroute(credentialPattern, reject); }
    expect(raw(cssUri)).toHaveLength(readsBeforeRevocation);
    result.phase = 'credentials-reopen-validates-original-owner';
    await command('_workbench.open', uri, [undefined, { pinned: true }]);
    await command('markdown.showPreview');
    await expect(probe()).toHaveText('原文件 CSS 与字体', { timeout: 60000 });
    await expect(probe()).toHaveCSS('color', 'rgb(101, 112, 123)');
    await expect.poll(() => result.requests.some(item => item.phase === result.phase && item.body
      && JSON.parse(item.body).uri === cssUri && JSON.parse(item.body).operation === 'describe')).toBe(true);
    expect(raw(cssUri)).toHaveLength(readsBeforeRevocation);
    const entriesAfterRevocation = await inspectCache();
    expect(entriesAfterRevocation).toEqual(expect.arrayContaining(entriesBeforeRevocation));
    result.identity = { rejection: credentialRejection, revokedDisplay: true, reopenedWithOriginalOwner: true,
      entriesBefore: entriesBeforeRevocation, entriesAfter: entriesAfterRevocation,
      rawReadsBefore: readsBeforeRevocation, rawReadsAfter: raw(cssUri).length };
    // TODO：固定隔离宿主的 trackFocus 在 iframe body 建立前可能抛错；保留断言，待上游修复。
    expect(result.pageErrors).toEqual([]);
    result.phase = 'complete';
  } catch (error) {
    failure = error;
    result.error = String(error?.stack ?? error);
    if (page) {
      result.currentFrames = page.frames().map(frame => frame.url());
      result.frameDiagnostics = await Promise.allSettled(page.frames().filter(frame =>
        frame.url().includes('.vscode-cdn.net/')).map(async (frame, index) => {
        const path = join(artifacts, `webview-frame-${index}.html`);
        await writeFile(path, await frame.content());
        return { url: frame.url(), path };
      }));
      try { await page.screenshot({ path: join(artifacts, 'webview-cache-failure.png') }); }
      catch (diagnosticError) { failure = new AggregateError([failure, diagnosticError], 'Webview 验证及截图失败'); }
    }
  } finally {
    try { await run.close(); }
    catch (cleanupError) { failure = failure ? new AggregateError([failure, cleanupError], 'Webview 验证及清理失败') : cleanupError; }
    await writeFile(required('BOXTEAM_BROWSER_RESULT_PATH'), JSON.stringify(result, null, 2) + '\n');
  }
  if (failure) throw failure;

}

async function launchWeb({ run, result, usePage }) {
  const profile = required('BOXTEAM_WEBVIEW_PROFILE');
  await mkdir(profile, { recursive: true });
  let context;
  const open = async () => {
    const instance = await chromium.launchPersistentContext(profile, {
      executablePath: required('PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH'),
      headless: true, args: ['--no-sandbox', '--disable-dev-shm-usage'],
    });
    context = instance;
    run.addCleanup('webview-persistent-browser', () => instance.close());
    usePage(instance.pages()[0] ?? await instance.newPage());
  };
  await open();
  result.boundary += '；独立 Web profile，正常关闭 Chromium 后同来源重启';
  return { baseUrl: required('BOXTEAM_BROWSER_BASE_URL'),
    async reload() { await context.close(); await open(); },
  };
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  await runWebviewResourceCache({
    run: TestRunContext.fromTestFile('tests/integration/clients/web/quick_access/test_webview_resource_cache.py'),
    launch: launchWeb,
  });
}
