import assert from "node:assert/strict";

/** 从原请求的 CDP 回执取正文，避免 Playwright 为长流程缓存每份完整历史。 */
export async function readDiagnosticResponseBody(response, nativeResponses, cdp, waitUntil) {
  const requestId = response.headers()["x-request-id"];
  assert.ok(requestId, `诊断响应缺少 X-Request-ID: ${response.status()} ${response.url()}`);
  let nativeRequestId;
  let native;
  await waitUntil(() => {
    const candidates = [...nativeResponses.entries()].filter(([, item]) =>
      item.serverRequestId === requestId && item.url === response.url()
      && item.status === response.status() && item.request?.method === response.request().method());
    assert.ok(candidates.length <= 1, `诊断响应的 CDP 身份不唯一: ${requestId}`);
    if (!candidates.length) return false;
    [nativeRequestId, native] = candidates[0];
    return native.finishedAt !== undefined || native.failure !== undefined;
  }, `原响应正文 ${requestId}`);
  assert.equal(native.failure, undefined, `原响应未完整结束: ${requestId}: ${native.failure}`);
  const result = await cdp.send("Network.getResponseBody", { requestId: nativeRequestId });
  return Buffer.from(result.body, result.base64Encoded ? "base64" : "utf8");
}

/** 使用真实 Chromium 和正式诊断入口，观察完成采集后仍被驱动持有的正文。 */
export async function runDiagnosticResponseLifetimeSelfTest(chromium, executablePath, install, drain, responses) {
  const { createServer } = await import("node:http");
  const payload = "x".repeat(1024 * 1024);
  const sessionId = "11111111-1111-4111-8111-111111111111";
  const threadId = "22222222-2222-4222-8222-222222222222";
  const server = createServer((request, response) => {
    const staticFailure = request.url === "/assets/failure.js";
    response.statusCode = staticFailure ? 502 : 200;
    response.setHeader("content-type", "application/json");
    response.setHeader("x-request-id", staticFailure ? "diagnostic-static-failure"
      : `diagnostic-${new URL(request.url, "http://localhost").searchParams.get("index")}`);
    response.end(request.url.startsWith("/api/")
      ? JSON.stringify({ request_id: response.getHeader("x-request-id"), data: {
        session: { session_id: sessionId, workspace_id: "workspace-probe" }, thread_id: threadId,
        projection_epoch: 17, turns: [payload],
      } }) : "{}");
  });
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  let browser;
  try {
    browser = await chromium.launch({ executablePath, headless: true });
    const context = await browser.newContext();
    const page = await context.newPage();
    const tracking = await install(page);
    await page.controlledRestartCdpSetup;
    await page.goto(`http://127.0.0.1:${server.address().port}/`);
    globalThis.gc();
    const initialBytes = process.memoryUsage().arrayBuffers;
    const count = 160;
    for (let index = 0; index < count; index += 1) {
      await page.evaluate(async ({ sessionId, threadId, index }) => {
        const response = await fetch(`/api/v1/sessions/${sessionId}/threads/${threadId}/history?index=${index}`);
        await response.text();
        const source = await fetch(`/assets/source-${index}.js`);
        await source.text();
      }, { sessionId, threadId, index });
      await drain(page);
    }
    await page.evaluate(async () => { await (await fetch("/assets/failure.js")).text(); });
    const staticErrors = [...tracking.responses.values()].filter(item => !new URL(item.url).pathname.startsWith("/api/"));
    assert.deepEqual(staticErrors.map(item => ({ status: item.status, requestId: item.serverRequestId,
      method: item.request?.method, pathname: new URL(item.url).pathname })), [
      { status: 502, requestId: "diagnostic-static-failure", method: "GET", pathname: "/assets/failure.js" },
    ], "静态资源的原始 502 仍须保留给错误归因");
    assert.equal(responses.length, count);
    for (const [index, fact] of responses.entries()) {
      assert.equal(fact.bodyError, undefined);
      assert.deepEqual(fact.body, { request_id: `diagnostic-${index}`, sessionId, threadId,
        workspaceId: "workspace-probe", projectionEpoch: 17 });
    }
    globalThis.gc();
    const retainedBytes = process.memoryUsage().arrayBuffers - initialBytes;
    const nativeRequests = tracking.requests.size;
    const nativeResponses = tracking.responses.size;
    console.log(JSON.stringify({ count, payloadBytes: payload.length, retainedBytes,
      completeBodies: responses.length, nativeRequests, nativeResponses }));
    assert.ok(retainedBytes < count * payload.length / 4,
      `诊断完成后不应继续持有整批正文: ${retainedBytes} bytes`);
    assert.equal(nativeRequests, 0, "已经收到响应的请求不应继续占用在途请求表");
    assert.equal(nativeResponses, count + 1, "静态资源成功响应不应进入业务诊断响应表，502 原件必须保留");
    await context.close();
  } finally {
    try { if (browser) await browser.close(); }
    finally { await new Promise(resolve => server.close(resolve)); }
  }
}
