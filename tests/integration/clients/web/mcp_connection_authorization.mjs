import assert from "node:assert/strict";
import { writeFile } from "node:fs/promises";
import path from "node:path";
import { chromium } from "playwright";
import { expect } from "playwright/test";

const fixture = JSON.parse(process.env.BOXTEAM_BROWSER_FIXTURE);
const artifacts = process.env.BOXTEAM_BROWSER_ARTIFACTS;
assert(artifacts, "需要显式浏览器产物目录");
const browser = await chromium.launch({ executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH, headless: true });
let context;
let page;
try {
  context = await browser.newContext({ viewport: { width: 1600, height: 1050 } });
  page = await context.newPage();
  const errors = [];
  const consoleMessages = [];
  const requests = [];
  const callbacks = [];
  const callbackPosts = [];
  const callbackResponses = [];
  context.on("response", (response) => {
    if (new URL(response.url()).pathname === "/mcp-oauth-callback.html") callbackResponses.push({ status: response.status(), headers: response.headers() });
  });
  context.on("page", (opened) => {
    opened.on("pageerror", (error) => errors.push(String(error)));
    opened.on("console", (message) => consoleMessages.push(message.text()));
  });
  page.on("pageerror", (error) => errors.push(String(error)));
  context.on("request", (request) => {
    const url = new URL(request.url());
    if (url.pathname === "/mcp-oauth-callback.html") callbacks.push(url);
    if (url.pathname.endsWith("/callback") && request.method() === "POST") callbackPosts.push(request);
    requests.push({ url, headers: request.headers() });
  });
  const target = new URL(fixture.baseUrl);
  await page.goto(target.href, { waitUntil: "domcontentloaded" });
  // Cookie 交由真实浏览器处理，避免 Bun 的 Node HTTP 兼容层改变响应 URL。
  const access = await page.evaluate(async () => {
    const credential = await fetch("/api/gateway/auth/local-credential");
    if (!credential.ok) throw new Error("读取本机凭据失败");
    const response = await fetch("/api/gateway/users/guest", {
      method: "POST", headers: { "Content-Type": "application/json", "X-Local-Token": (await credential.json()).data.token },
      body: JSON.stringify({ tracking: { source: "mcp-workbench-browser" } }),
    });
    return { status: response.status, body: await response.text() };
  });
  assert.equal(access.status, 200, access.body);
  await page.reload({ waitUntil: "domcontentloaded" });
  await expect(page.locator("#workbench .monaco-workbench")).toBeVisible();
  const session = page.locator(`button[data-session-id="${fixture.sessionId}"]`);
  await expect(session).toContainText(fixture.sessionTitle);
  await session.dblclick();
  const thread = page.locator(`section.boxteam-thread[data-session-id="${fixture.sessionId}"][data-thread-id="${fixture.threadId}"]`);
  await expect(thread).toBeVisible();
  // 完整页面初始化之后，经实际同源 Gateway 再核对目标和 request_id。
  const initialization = await page.evaluate(async (workspaceId) => {
    const credential = await fetch("/api/gateway/auth/local-credential");
    if (!credential.ok) throw new Error("本地管理凭证读取失败");
    const token = (await credential.json()).data.token;
    const results = [];
    let owner;
    let gatewayId;
    for (const url of ["/api/gateway/health", "/api/gateway/workspaces", "/api/v1/workspace"]) {
      const headers = { "X-Local-Token": token };
      if (url === "/api/v1/workspace") {
        if (!owner) throw new Error("Gateway 未登记完整目标工作区");
        headers["X-BoxTeam-Target-Gateway-Id"] = owner.gateway_id;
        headers["X-BoxTeam-Workspace-Id"] = owner.workspace_id;
      }
      const response = await fetch(url, { headers });
      const body = await response.json();
      if (url === "/api/gateway/health") gatewayId = body.data.gateway_id;
      if (url === "/api/gateway/workspaces") {
        const candidates = body.data.items.filter(item => item.gateway_id === gatewayId && item.workspace_id === workspaceId);
        if (candidates.length !== 1) throw new Error("Gateway／UUID 目标必须唯一登记");
        [owner] = candidates;
      }
      results.push({ url, status: response.status, requestId: response.headers.get("x-request-id"), body });
    }
    return { results, owner };
  }, fixture.workspaceId);
  for (const result of initialization.results) {
    assert.equal(result.status, 200, result.url);
    assert(result.requestId);
    assert.equal(result.requestId, result.body.request_id);
  }
  await expect(thread).toHaveAttribute("data-workspace-id", fixture.workspaceId);
  async function mcpPanel() {
    await page.evaluate(() => globalThis.workbench.commands.executeCommand("boxteam.mcp.focus"));
    const panel = page.locator(`.native-workspace-panel[data-workspace-id="${initialization.owner.workspace_id}"][data-panel-kind="mcp"] .native-workspace-panel-content:not([hidden])`);
    await expect(panel).toBeVisible();
    return panel;
  }
  async function send(content) {
    await thread.getByTestId("composer-input").fill(content);
    const accepted = page.waitForResponse((response) => new URL(response.url()).pathname.endsWith("/messages") && response.request().method() === "POST");
    await thread.getByTestId("composer-send").click();
    const response = await accepted;
    assert.equal(response.status(), 200);
    const request = response.request();
    assert.equal(new URL(request.url()).pathname, `/api/v1/sessions/${fixture.sessionId}/threads/${fixture.threadId}/messages`);
    const headers = await request.allHeaders();
    assert.equal(headers["x-boxteam-target-gateway-id"], initialization.owner.gateway_id);
    assert.equal(headers["x-boxteam-workspace-id"], initialization.owner.workspace_id);
    const body = await response.json();
    assert.equal(response.headers()["x-request-id"], body.request_id);
    return body.data.job_id;
  }
  if (fixture.mode === "errors") {
    const panel = await mcpPanel();
    const secure = panel.getByRole("region", { name: "MCP Server secure", exact: true });
    const sibling = panel.getByRole("region", { name: "MCP Server schema", exact: true });
    await sibling.getByText("连接可用", { exact: true }).waitFor();
    const operations = [];
    async function submit(name) {
      const accepted = page.waitForResponse((response) => new URL(response.url()).pathname === "/api/v1/mcp/connection-operations" && response.request().method() === "POST");
      await secure.getByRole("button", { name, exact: true }).click();
      const response = await accepted;
      assert.equal(response.status(), 200);
      const body = await response.json();
      assert.equal(response.headers()["x-request-id"], body.request_id);
      operations.push(body.data);
      return body.data;
    }
    async function authorize() {
      const opened = page.waitForEvent("popup");
      await secure.getByRole("button", { name: "打开授权页面", exact: true }).click();
      const popup = await opened;
      if (!popup.isClosed()) await popup.waitForEvent("close", { timeout: 30000 });
    }
    await submit("连接");
    await authorize();
    await panel.getByRole("alert").filter({ hasText: "授权已拒绝" }).waitFor();
    assert.doesNotMatch(await secure.innerText(), /连接可用/);
    assert.equal(callbackPosts.length, 0);
    assert.equal(callbacks.length, 1);
    assert.equal(callbacks[0].searchParams.get("error"), "access_denied");
    await secure.getByRole("button", { name: "取消连接操作", exact: true }).click();
    await secure.getByRole("status").filter({ hasText: "连接：已取消" }).waitFor();

    // 使用真实产品300秒期限，不改时钟或持久记录；页面通过原owner轮询取得过期事实。
    const expiring = await submit("连接");
    assert(Math.abs(expiring.deadline - expiring.created_at - 300) < 1);
    await secure.getByRole("alert").filter({ hasText: "deadline_exceeded" }).waitFor({ timeout: 330000 });
    const expiryObservedAt = Date.now() / 1000;
    assert(expiryObservedAt >= expiring.deadline);
    await secure.getByRole("status").filter({ hasText: "连接：已过期" }).waitFor();
    assert.doesNotMatch(await secure.innerText(), /连接可用/);

    await submit("连接");
    await authorize();
    await secure.getByText("连接可用", { exact: true }).waitFor({ timeout: 30000 });
    await submit("断开");
    await secure.getByRole("status").filter({ hasText: "断开：操作已完成" }).waitFor();
    assert.doesNotMatch(await secure.innerText(), /连接可用/);
    assert.equal(await secure.getByRole("button", { name: "断开", exact: true }).isDisabled(), true);
    assert.equal(await secure.getByRole("button", { name: "连接", exact: true }).isEnabled(), true);
    await sibling.getByText("连接可用", { exact: true }).waitFor();
    assert.equal(callbacks.length, 2);
    assert.equal(callbackPosts.length, 1);
    await writeFile(path.join(artifacts, "connection-errors-result.json"), JSON.stringify({
      operationIds: operations.map((operation) => operation.operation_id),
      deadline: expiring.deadline, expiryObservedAt, denialVisible: true, expiryVisible: true,
      disconnectVerified: true, siblingReady: true, noPageErrors: errors.length === 0,
    }, null, 2));
  } else if (fixture.mode === "authorize") {
    const jobId = await send("发现 secure 工具并发起工作区连接，等待用户授权。");
    const managementPromise = page.waitForEvent("popup");
    await thread.getByRole("link", { name: "查看此工作区的 MCP 授权与连接状态" }).first().click({ timeout: 60000 });
    const management = await managementPromise;
    await management.getByRole("heading", { name: "工作区 MCP 连接与授权", exact: true }).waitFor();
    assert.deepEqual(JSON.parse(new URL(management.url()).searchParams.get("mcp_workspace")), [initialization.owner.gateway_id, initialization.owner.workspace_id]);
    assert.equal(await management.locator(".monaco-workbench").count(), 0);
    const secure = management.getByRole("region", { name: "MCP Server secure", exact: true });
    await secure.getByRole("button", { name: "打开授权页面", exact: true }).waitFor({ timeout: 30000 });
    assert.match(await secure.innerText(), /等待用户授权/);
    assert.match(await management.getByRole("region", { name: "MCP Server schema", exact: true }).innerText(), /连接可用/);
    const popupPromise = management.waitForEvent("popup");
    await secure.getByRole("button", { name: "打开授权页面", exact: true }).click();
    const popup = await popupPromise;
    if (!popup.isClosed()) await popup.waitForEvent("close", { timeout: 30000 });
    await secure.getByText("连接可用", { exact: true }).waitFor({ timeout: 30000 });
    await secure.getByText(/新工具在允许的上下文边界生效/).waitFor();
    // 面板与独立入口读取同一后端事实，原版布局持久化在当前浏览器。
    const panel = await mcpPanel();
    await panel.getByRole("region", { name: "MCP Server secure", exact: true }).getByText("连接可用", { exact: true }).waitFor();
    await page.evaluate(() => globalThis.boxteamWorkbenchLifecycle.prepareToClose());
    await page.reload({ waitUntil: "domcontentloaded" });
    await expect(page.getByRole("tab", { name: "MCP 连接", exact: true })).toHaveAttribute("aria-selected", "true");
    await expect(panel).toBeVisible();
    await panel.getByRole("region", { name: "MCP Server secure", exact: true }).getByText("连接可用", { exact: true }).waitFor();
    await expect(thread).toBeVisible();
    assert.equal(callbacks.length, 1);
    assert.equal(callbackPosts.length, 1);
    const callback = callbacks[0];
    const payload = callbackPosts[0].postDataJSON();
    assert.equal(payload.code, callback.searchParams.get("code"));
    assert.equal(payload.state, callback.searchParams.get("state"));
    const headers = await callbackPosts[0].allHeaders();
    assert.equal(headers["x-boxteam-target-gateway-id"], initialization.owner.gateway_id);
    assert.equal(headers["x-boxteam-workspace-id"], initialization.owner.workspace_id);
    const secrets = [payload.code, payload.state];
    for (const request of requests) {
      assert(secrets.every((secret) => !request.headers.referer?.includes(secret)), "Referer 含私有回调参数");
    }
    assert(secrets.every((secret) => !consoleMessages.some((message) => message.includes(secret))), "浏览器控制台含私有参数");
    for (const opened of [page, management]) {
      const persisted = await opened.evaluate(() => JSON.stringify({ local: { ...localStorage }, session: { ...sessionStorage }, html: document.body.innerText, url: location.href }));
      const surfaces = opened.locator(".boxteam-react-surface");
      assert(await surfaces.count() > 0, "必须检查真实 React 页面而非空文档");
      const shadowText = await surfaces.evaluateAll(hosts => hosts.map(host => host.shadowRoot.textContent).join("\n"));
      assert(secrets.every((secret) => !persisted.includes(secret) && !shadowText.includes(secret)), "页面或浏览器存储含私有回调参数");
    }
    await writeFile(path.join(artifacts, "authorization-result.json"), JSON.stringify({ jobId, initialization: true, callbackPostCount: callbackPosts.length, requestId: true, privateReferrerAbsent: true, privateStorageAbsent: true, backendReady: true, bottomTabPersisted: true, noPageErrors: errors.length === 0 }, null, 2));
  } else {
    const message = "授权已完成，读取完整 secure 定义并执行 probe 验证。";
    const jobId = await send(message);
    // 历史加载和虚拟列表会改变回复数量，只等待本轮用户输入所在的真实 Turn。
    const turn = thread.locator("article[data-turn-id]").filter({ has: page.getByText(message, { exact: true }) });
    await turn.getByText("已核查目录与连接事实。", { exact: true }).waitFor({ timeout: 60000 });
    // 网络收据、canonical 与 hash 由 Python 在执行结束后核验。
    await writeFile(path.join(artifacts, "execution-result.json"), JSON.stringify({ jobId, initialization: true, noPageErrors: errors.length === 0 }, null, 2));
  }
  assert.equal(callbackResponses.length, callbacks.length);
  for (const response of callbackResponses) {
    assert.equal(response.status, 200);
    assert.equal(response.headers["cache-control"], "no-store");
    assert.equal(response.headers["referrer-policy"], "no-referrer");
  }
  assert.deepEqual(errors, []);
} catch (error) {
  if (page) await page.screenshot({ path: path.join(artifacts, `${fixture.mode}-failure.png`), fullPage: true });
  throw error;
} finally {
  try { await context?.close(); } finally { await browser.close(); }
}
