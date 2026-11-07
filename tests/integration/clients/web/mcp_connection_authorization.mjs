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
let stage = "create-browser-context";
const errors = [];
const consoleMessages = [];
try {
  context = await browser.newContext({ viewport: { width: 1600, height: 1050 } });
  page = await context.newPage();
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
  let initialCatalogFailures = 0;
  let initialCatalogUnavailable = true;
  const initialCatalogError = "MCP 首次目录读取暂不可用（测试替身）";
  if (fixture.mode === "authorize") {
    // 初次加载时保留显式失败，待原列表实际展示错误后恢复真实 Gateway。
    await context.route(url => url.pathname === "/api/v1/mcp/servers", async (route) => {
      const sender = new URL(route.request().frame().url());
      if (sender.searchParams.has("mcp_workspace") && initialCatalogUnavailable) {
        const headers = await route.request().allHeaders();
        assert.equal(headers["x-boxteam-target-gateway-id"], initialization.owner.gateway_id);
        assert.equal(headers["x-boxteam-workspace-id"], initialization.owner.workspace_id);
        ++initialCatalogFailures;
        await route.fulfill({ status: 503, contentType: "application/json", headers: { "X-Request-ID": "mcp-initial-catalog-unavailable" },
          body: JSON.stringify({ code: 503, message: initialCatalogError, request_id: "mcp-initial-catalog-unavailable" }) });
      } else await route.continue();
    });
  }
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
  stage = "prepare-target-api-readiness";
  // 完整页面初始化之后，经实际同源 Gateway 再核对目标和 request_id。
  const initialization = await page.evaluate(async (workspaceId) => {
    const credential = await fetch("/api/gateway/auth/local-credential");
    if (!credential.ok) throw new Error("本地管理凭证读取失败");
    const token = (await credential.json()).data.token;
    const results = [];
    let owner;
    let relayGatewayId;
    // MCP 专项先经产品入口等待 SDK 配置检查就绪；冷启动 Thread 的性能另验。
    for (const url of ["/api/gateway/health", "/api/gateway/workspaces", "/api/v1/workspace", "/api/v1/agents"]) {
      const headers = { "X-Local-Token": token };
      if (url.startsWith("/api/v1/")) {
        if (!owner) throw new Error("Gateway 未登记完整目标工作区");
        headers["X-BoxTeam-Target-Gateway-Id"] = owner.gateway_id;
        headers["X-BoxTeam-Workspace-Id"] = owner.workspace_id;
      }
      const started = performance.now();
      const response = await fetch(url, { headers });
      const body = await response.json();
      if (url === "/api/gateway/health") relayGatewayId = body.data.gateway_id;
      if (url === "/api/gateway/workspaces") {
        const selected = body.data.active_workspace;
        if (!selected || selected.workspace_id !== workspaceId) throw new Error("活动工作区与本次验收目标不一致");
        const candidates = body.data.items.filter(item => item.gateway_id === selected.gateway_id && item.workspace_id === selected.workspace_id);
        if (candidates.length !== 1) throw new Error("Gateway／UUID 目标必须唯一登记");
        [owner] = candidates;
      }
      results.push({ url, status: response.status, requestId: response.headers.get("x-request-id"), elapsedMs: performance.now() - started, body });
    }
    return { results, owner, relayGatewayId };
  }, fixture.workspaceId);
  for (const result of initialization.results) {
    assert.equal(result.status, 200, result.url);
    assert(result.requestId);
    assert.equal(result.requestId, result.body.request_id);
  }
  await writeFile(path.join(artifacts, "target-readiness.json"), JSON.stringify(initialization.results.map(({ body, ...result }) => result), null, 2));
  stage = "initialize-target-thread";
  const session = page.locator(`button[data-session-id="${fixture.sessionId}"]`);
  await expect(session).toContainText(fixture.sessionTitle);
  await session.dblclick();
  const thread = page.locator(`section.boxteam-thread[data-session-id="${fixture.sessionId}"][data-thread-id="${fixture.threadId}"]`);
  await expect(thread).toBeVisible();
  await expect(thread).toHaveAttribute("data-workspace-id", fixture.workspaceId);
  let targetSelected = false;
  async function mcpPanel() {
    stage = "open-main-mcp-panel";
    if (!targetSelected && initialization.owner.gateway_id !== initialization.relayGatewayId) {
      await page.evaluate(() => { void globalThis.workbench.commands.executeCommand('boxteam.mcp.selectTarget'); });
      const quick = page.locator('.quick-input-widget');
      await expect(quick).toBeVisible();
      const target = quick.locator('.monaco-list-row').filter({ hasText: initialization.owner.gateway_id });
      await expect(target).toHaveCount(1);
      await target.click();
      await expect(quick).not.toBeVisible();
      targetSelected = true;
    }
    // 原 Focus 命令在已聚焦时会关闭面板；此处只确保原面板展开。
    await page.evaluate(() => globalThis.workbench.commands.executeCommand("boxteam.mcp.focus", { preserveFocus: true }));
    const panel = page.locator(".part.panel .mcp-servers-list").filter({ visible: true });
    await expect(panel).toBeVisible();
    return panel;
  }
  function serverRow(panel, name) {
    return panel.getByRole("listitem", { name, exact: true });
  }
  function detailsBody(host) {
    // 原版详情通过顶层 overlay 承载双层 Webview，不属于编辑器节点的正文。
    return host.frameLocator("iframe.webview:visible").frameLocator('iframe#active-frame[title="Readme"]').locator("body");
  }
  async function openDetails(host, panel, name) {
    stage = host === page ? "open-main-mcp-details" : "open-authorization-mcp-details";
    await serverRow(panel, name).dblclick();
    const editor = host.locator(".mcp-server-editor").filter({ has: host.getByRole("heading", { name, exact: true }) }).filter({ visible: true });
    await expect(editor).toBeVisible();
    await expect(detailsBody(host)).toContainText(initialization.owner.workspace_id, { timeout: 30000 });
    await expect(detailsBody(host)).toContainText(initialization.owner.gateway_id);
    return editor;
  }
  async function send(content) {
    stage = "send-target-thread-message";
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
    const sibling = serverRow(panel, "schema");
    await expect(sibling).toContainText("连接可用");
    const secure = await openDetails(page, panel, "secure");
    const details = detailsBody(page);
    const operations = [];
    async function submit(name) {
      const accepted = page.waitForResponse((response) => new URL(response.url()).pathname === "/api/v1/mcp/connection-operations" && response.request().method() === "POST");
      await secure.getByRole("button", { name, exact: true }).click();
      const response = await accepted;
      assert.equal(response.status(), 200);
      const body = await response.json();
      assert.equal(response.headers()["x-request-id"], body.request_id);
      assert.equal(body.protocol_version, 1);
      assert.deepEqual(body.target, { kind: "workspace", gateway_id: initialization.owner.gateway_id, workspace_id: initialization.owner.workspace_id });
      const headers = await response.request().allHeaders();
      assert.equal(headers["x-boxteam-target-gateway-id"], initialization.owner.gateway_id);
      assert.equal(headers["x-boxteam-workspace-id"], initialization.owner.workspace_id);
      operations.push(body.data);
      return body.data;
    }
    async function authorize() {
      const opened = page.waitForEvent("popup");
      await secure.getByRole("button", { name: "Authorize", exact: true }).click();
      const popup = await opened;
      if (!popup.isClosed()) await popup.waitForEvent("close", { timeout: 30000 });
    }
    await submit("Start Server");
    await authorize();
    await expect(page.locator(".notifications-toasts")).toContainText("授权已拒绝");
    assert.doesNotMatch(await details.innerText(), /连接可用/);
    assert.equal(callbackPosts.length, 0);
    assert.equal(callbacks.length, 1);
    assert.equal(callbacks[0].searchParams.get("error"), "access_denied");
    await secure.getByRole("button", { name: "Cancel Connection Operation", exact: true }).click();
    await expect(details).toContainText("已取消");

    // 使用真实产品300秒期限，不改时钟或持久记录；页面通过原owner轮询取得过期事实。
    const expiring = await submit("Start Server");
    assert(Math.abs(expiring.deadline - expiring.created_at - 300) < 1);
    await expect(details).toContainText("deadline_exceeded", { timeout: 330000 });
    const expiryObservedAt = Date.now() / 1000;
    assert(expiryObservedAt >= expiring.deadline);
    await expect(details).toContainText("已过期");
    assert.doesNotMatch(await details.innerText(), /连接可用/);

    await submit("Start Server");
    await authorize();
    await expect(details).toContainText("连接可用", { timeout: 30000 });
    await submit("Stop Server");
    await expect(details).toContainText("操作已完成");
    assert.doesNotMatch(await details.innerText(), /连接可用/);
    await expect(secure.getByRole("button", { name: "Stop Server", exact: true })).toHaveCount(0);
    await expect(secure.getByRole("button", { name: "Start Server", exact: true })).toBeEnabled();
    await expect(sibling).toContainText("连接可用");
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
    stage = "authorization-workbench-initialization";
    await expect(management.locator("#workbench .monaco-workbench")).toBeVisible({ timeout: 30000 });
    assert.deepEqual(JSON.parse(new URL(management.url()).searchParams.get("mcp_workspace")), [initialization.owner.gateway_id, initialization.owner.workspace_id]);
    stage = "initial-catalog-error-visible";
    await expect(management.locator('.notifications-toasts')).toContainText(initialCatalogError);
    initialCatalogUnavailable = false;
    stage = "initial-catalog-recovery";
    // 深链只打开原workspace对象；普通管理列表继续保持本地Gateway选择。
    const secure = management.locator('.mcp-server-editor').filter({ has: management.getByRole('heading', { name: 'secure', exact: true }) }).filter({ visible: true });
    await expect(secure).toBeVisible({ timeout: 30000 });
    await expect(detailsBody(management)).toContainText('等待用户授权');
    await expect(detailsBody(management)).toContainText(initialization.owner.workspace_id);
    await expect(detailsBody(management)).toContainText(initialization.owner.gateway_id);
    assert(initialCatalogFailures >= 1);
    if (initialization.owner.gateway_id !== initialization.relayGatewayId) {
      const panel = management.locator('.part.panel .mcp-servers-list').filter({ visible: true });
      await expect(serverRow(panel, 'secure')).toHaveCount(0);
    }
    stage = "authorize-from-original-details";
    const details = detailsBody(management);
    await secure.getByRole("button", { name: "Authorize", exact: true }).waitFor({ timeout: 30000 });
    await expect(details).toContainText("等待用户授权");
    const popupPromise = management.waitForEvent("popup");
    await secure.getByRole("button", { name: "Authorize", exact: true }).click();
    const popup = await popupPromise;
    if (!popup.isClosed()) await popup.waitForEvent("close", { timeout: 30000 });
    await expect(details).toContainText("连接可用", { timeout: 30000 });
    // 面板与独立入口读取同一后端事实，原版布局持久化在当前浏览器。
    const panel = await mcpPanel();
    stage = "main-panel-shares-ready-owner";
    await expect(serverRow(panel, "secure")).toContainText("连接可用");
    // 原列表命令和底部入口必须指向同一服务，选项沿原 Quick Input 呈现。
    await page.evaluate(() => { void globalThis.workbench.commands.executeCommand("workbench.mcp.listServer"); });
    const quick = page.locator(".quick-input-widget");
    await expect(quick).toBeVisible();
    await expect(quick).toContainText(initialization.owner.gateway_id);
    await quick.locator(".monaco-list-row").filter({ has: page.getByText("secure", { exact: true }) }).click();
    await expect(quick.getByText("Stop Server", { exact: true })).toBeVisible();
    await expect(quick.getByText("Show Details", { exact: true })).toBeVisible();
    await quick.locator("input").press("Escape");
    await expect(quick).not.toBeVisible();
    const unavailableCommands = await page.evaluate(async () => {
      const results = [];
      for (const command of ["workbench.mcp.addConfiguration", "workbench.mcp.openUserMcpJson", "workbench.mcp.setOAuthClientSecret"]) {
        try {
          await globalThis.workbench.commands.executeCommand(command);
          results.push({ command, error: null });
        } catch (error) {
          results.push({ command, error: String(error) });
        }
      }
      return results;
    });
    for (const result of unavailableCommands) assert.match(result.error ?? "", /配置与凭据写入.*尚未接入/, result.command);
    stage = "restore-main-mcp-panel";
    await page.evaluate(() => globalThis.boxteamWorkbenchLifecycle.prepareToClose());
    await page.reload({ waitUntil: "domcontentloaded" });
    await expect(page.getByRole("tab", { name: "MCP 连接", exact: true })).toHaveAttribute("aria-selected", "true");
    await expect(panel).toBeVisible();
    await expect(serverRow(panel, "secure")).toContainText("连接可用");
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
    if (initialization.owner.gateway_id !== initialization.relayGatewayId) {
      const original = requests.find(request => request.url.pathname === '/api/v1/mcp/servers'
        && request.headers['x-boxteam-target-gateway-id'] === initialization.owner.gateway_id);
      assert(original?.headers['x-boxteam-gateway-connection-id']);
      assert.equal(headers['x-boxteam-gateway-connection-id'], original.headers['x-boxteam-gateway-connection-id']);
    }
    const secrets = [payload.code, payload.state];
    for (const request of requests) {
      assert(secrets.every((secret) => !request.headers.referer?.includes(secret)), "Referer 含私有回调参数");
    }
    assert(secrets.every((secret) => !consoleMessages.some((message) => message.includes(secret))), "浏览器控制台含私有参数");
    for (const opened of [page, management]) {
      const persisted = await opened.evaluate(() => JSON.stringify({ local: { ...localStorage }, session: { ...sessionStorage }, html: document.body.innerText, url: location.href }));
      assert.equal(await opened.locator("#workbench .monaco-workbench").count(), 1, "必须检查真实 Workbench 而非空文档");
      const surfaces = opened.locator(".boxteam-react-surface");
      const shadowText = await surfaces.evaluateAll(hosts => hosts.map(host => host.shadowRoot.textContent).join("\n"));
      const readmeText = opened === management ? await detailsBody(opened).innerText() : "";
      assert(secrets.every((secret) => !persisted.includes(secret) && !shadowText.includes(secret) && !readmeText.includes(secret)), "页面或浏览器存储含私有回调参数");
    }
    await writeFile(path.join(artifacts, "authorization-result.json"), JSON.stringify({ jobId, initialization: true, owner: { gateway_id: initialization.owner.gateway_id, workspace_id: initialization.owner.workspace_id }, relayGatewayId: initialization.relayGatewayId,
      originalWorkbenchManagement: true, originalListCommand: true, initialCatalogFailures, unavailableCommands, callbackPostCount: callbackPosts.length, requestId: true,
      privateReferrerAbsent: true, privateStorageAbsent: true, backendReady: true, bottomTabPersisted: true, noPageErrors: errors.length === 0 }, null, 2));
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
  // 失败可能发生在授权工作台；保留各个已打开页面，避免只看到主窗口而误判时序。
  const diagnostics = await Promise.allSettled((context?.pages() ?? []).map(async (opened, index) => {
    const name = `${fixture.mode}-failure-${index}`;
    await writeFile(path.join(artifacts, `${name}.aria.txt`), await opened.locator("body").ariaSnapshot());
    await opened.screenshot({ path: path.join(artifacts, `${name}.png`), fullPage: true });
  }));
  await writeFile(path.join(artifacts, `${fixture.mode}-failure.json`), JSON.stringify({ stage, error: String(error), errors, consoleMessages,
    diagnosticErrors: diagnostics.filter(result => result.status === "rejected").map(result => String(result.reason)),
  }, null, 2));
  throw error;
} finally {
  try { await context?.close(); } finally { await browser.close(); }
}
