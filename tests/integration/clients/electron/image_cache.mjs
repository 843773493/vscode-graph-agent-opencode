import { expect } from 'playwright/test';

/** 在真实 ASAR renderer 观察原图片编辑器；只保留条件头和字节计量，不采集凭据。 */
export async function observeImageCache(page, allReads) {
  const session = await page.context().newCDPSession(page), requests = new Map();
  // TODO：两轮真实 ASAR 热读均多出一次零正文304，结合 watcher 与调用来源定位后再决定是否精简。
  const item = id => {
    if (!requests.has(id)) requests.set(id, { encodedBodyBytes: 0 });
    return requests.get(id);
  };
  const headers = values => Object.fromEntries(Object.entries(values)
    .filter(([key]) => ['if-none-match', 'etag', 'cache-control', 'x-request-id'].includes(key.toLowerCase()))
    .map(([key, value]) => [key.toLowerCase(), value]));
  let phase;
  session.on('Network.requestWillBeSent', event => {
    const url = new URL(event.request.url);
    if (url.pathname !== '/api/v1/workspace/files/raw' || !url.searchParams.get('path')?.endsWith('/native-cache.png')) return;
    const read = item(event.requestId);
    Object.assign(read, { phase, url: event.request.url, request: headers(event.request.headers),
      timestamp: event.timestamp, initiator: { type: event.initiator.type, frames: (event.initiator.stack?.callFrames ?? [])
        .slice(0, 8).map(frame => ({ function: frame.functionName, url: frame.url, line: frame.lineNumber, column: frame.columnNumber })) } });
    allReads.push(read);
  });
  session.on('Network.requestWillBeSentExtraInfo', event => { item(event.requestId).wireRequest = headers(event.headers); });
  session.on('Network.responseReceivedExtraInfo', event => {
    Object.assign(item(event.requestId), { status: event.statusCode, response: headers(event.headers) });
  });
  session.on('Network.dataReceived', event => { item(event.requestId).encodedBodyBytes += event.encodedDataLength; });
  session.on('Network.loadingFinished', event => { item(event.requestId).finished = true; });
  await session.send('Network.enable');
  return async (owner, label, expectedStatus) => {
    phase = label;
    const uri = await page.evaluate(owner => globalThis.workbench.URI.from({ scheme: 'boxteam-file',
      authority: owner.gatewayId, path: `/${owner.workspaceId}/${encodeURIComponent(owner.rootUri)}/native-cache.png` }).toString(), owner);
    await page.evaluate(uri => globalThis.workbench.commands.executeCommand('_workbench.open', uri, [undefined, { pinned: true }]), uri);
    const image = page.locator('.image-preview-editor').filter({ visible: true }).locator('img');
    await expect(image).toBeVisible();
    await expect.poll(() => image.evaluate(element => [element.naturalWidth, element.naturalHeight])).toEqual([1, 1]);
    await expect.poll(() => allReads.some(read => read.phase === label && read.finished && read.status === expectedStatus)).toBe(true);
    const read = allReads.find(read => read.phase === label && read.finished && read.status === expectedStatus);
    expect(read.response['cache-control']).toBe('private, no-cache');
    expect(read.response['x-request-id']).toBeTruthy();
    if (expectedStatus === 304) {
      expect(read.wireRequest['if-none-match']).toBe(read.response.etag);
      expect(read.encodedBodyBytes).toBe(0);
    } else expect(read.encodedBodyBytes).toBeGreaterThan(0);
    await page.evaluate(() => globalThis.workbench.commands.executeCommand('workbench.action.closeActiveEditor'));
    return read;
  };
}
