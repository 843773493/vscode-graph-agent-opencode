import assert from "node:assert/strict";
import { writeFile } from "node:fs/promises";
import path from "node:path";
import { chromium } from "playwright";

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
  await page.goto(fixture.baseUrl, { waitUntil: "domcontentloaded" });
  if (fixture.mode === "authorize") {
    // 游客偏好按原合同不持久保存；通过产品入口创建用户后验收工作区标签恢复。
    await page.getByRole("button", { name: "用户视图", exact: true }).click();
    await page.getByPlaceholder("新用户名称").fill("MCP 授权验收用户");
    await page.getByRole("button", { name: "创建", exact: true }).click();
  }
  await page.locator("[data-session-id]").filter({ hasText: fixture.sessionTitle }).first().click();
  // 完整页面初始化之后，经实际同源 Gateway 再核对目标和 request_id。
  const initialization = await page.evaluate(async (workspaceId) => {
    const credential = await fetch("/api/gateway/auth/local-credential");
    if (!credential.ok) throw new Error("本地管理凭证读取失败");
    const token = (await credential.json()).data.token;
    const results = [];
    let owner;
    for (const url of ["/api/gateway/health", "/api/gateway/workspaces", "/api/v1/workspace"]) {
      const headers = { "X-Local-Token": token };
      if (url === "/api/v1/workspace") {
        if (!owner) throw new Error("Gateway 未登记目标 Backend 工作区");
        headers["X-BoxTeam-Workspace-Id"] = owner.workspace_id;
        if (owner.connection_kind === "local") headers["X-BoxTeam-Backend-Workspace-Id"] = owner.backend_workspace_id;
      }
      const response = await fetch(url, { headers });
      const body = await response.json();
      if (url === "/api/gateway/workspaces") owner = body.data.items.find((item) => item.backend_workspace_id === workspaceId);
      results.push({ url, status: response.status, requestId: response.headers.get("x-request-id"), body });
    }
    return { results, owner };
  }, fixture.workspaceId);
  for (const result of initialization.results) {
    assert.equal(result.status, 200, result.url);
    assert(result.requestId);
    assert.equal(result.requestId, result.body.request_id);
  }
  async function send(content) {
    await page.getByTestId("composer-input").fill(content);
    const accepted = page.waitForResponse((response) => new URL(response.url()).pathname.endsWith("/messages") && response.request().method() === "POST");
    await page.getByTestId("composer-send").click();
    const response = await accepted;
    assert.equal(response.status(), 200);
    const body = await response.json();
    assert.equal(response.headers()["x-request-id"], body.request_id);
    return body.data.job_id;
  }
  if (fixture.mode === "authorize") {
    const jobId = await send("发现 secure 工具并发起工作区连接，等待用户授权。");
    const managementPromise = page.waitForEvent("popup");
    await page.getByRole("link", { name: "查看此工作区的 MCP 授权与连接状态" }).first().click({ timeout: 60000 });
    const management = await managementPromise;
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
    // 主窗口底部面板与会话入口使用同一工作区操作事实，标签由后端保存。
    if (await page.getByRole("button", { name: "切换底部面板", exact: true }).getAttribute("aria-pressed") !== "true") {
      await page.getByRole("button", { name: "切换底部面板", exact: true }).click();
    }
    const saved = page.waitForResponse((response) => {
      if (new URL(response.url()).pathname !== "/api/gateway/ui-settings" || response.request().method() !== "PUT") return false;
      return Object.values(response.request().postDataJSON().layout?.bottom_panel_by_workspace ?? {}).some((item) => item.tab === "mcp");
    });
    await page.getByRole("tab", { name: "MCP 连接", exact: true }).click();
    assert.equal((await saved).status(), 200);
    await page.getByTestId("mcp-connections-panel").getByRole("region", { name: "MCP Server secure", exact: true }).getByText("连接可用", { exact: true }).waitFor();
    await page.reload({ waitUntil: "domcontentloaded" });
    await page.getByRole("tab", { name: "MCP 连接", exact: true }).waitFor();
    assert.equal(await page.getByRole("tab", { name: "MCP 连接", exact: true }).getAttribute("aria-selected"), "true");
    assert.equal(callbacks.length, 1);
    assert.equal(callbackPosts.length, 1);
    const callback = callbacks[0];
    const payload = callbackPosts[0].postDataJSON();
    assert.equal(payload.code, callback.searchParams.get("code"));
    assert.equal(payload.state, callback.searchParams.get("state"));
    const headers = await callbackPosts[0].allHeaders();
    assert.equal(headers["x-boxteam-workspace-id"], initialization.owner.workspace_id);
    assert.equal(headers["x-boxteam-backend-workspace-id"], initialization.owner.connection_kind === "local" ? fixture.workspaceId : undefined);
    const secrets = [payload.code, payload.state];
    for (const request of requests) {
      assert(secrets.every((secret) => !request.headers.referer?.includes(secret)), "Referer 含私有回调参数");
    }
    assert(secrets.every((secret) => !consoleMessages.some((message) => message.includes(secret))), "浏览器控制台含私有参数");
    for (const opened of [page, management]) {
      const persisted = await opened.evaluate(() => JSON.stringify({ local: { ...localStorage }, session: { ...sessionStorage }, html: document.body.innerText, url: location.href }));
      assert(secrets.every((secret) => !persisted.includes(secret)), "页面或浏览器存储含私有回调参数");
    }
    await writeFile(path.join(artifacts, "authorization-result.json"), JSON.stringify({ jobId, initialization: true, callbackPostCount: callbackPosts.length, requestId: true, privateReferrerAbsent: true, privateStorageAbsent: true, backendReady: true, bottomTabPersisted: true, noPageErrors: errors.length === 0 }, null, 2));
  } else {
    const message = "授权已完成，读取完整 secure 定义并执行 probe 验证。";
    const jobId = await send(message);
    // 历史加载和虚拟列表会改变回复数量，只等待本轮用户输入所在的真实 Turn。
    const turn = page.locator("article[data-turn-id]").filter({ has: page.getByText(message, { exact: true }) });
    await turn.getByText("已核查目录与连接事实。", { exact: true }).waitFor({ timeout: 60000 });
    // 网络收据、canonical 与 hash 由 Python 在执行结束后核验。
    await writeFile(path.join(artifacts, "execution-result.json"), JSON.stringify({ jobId, initialization: true, noPageErrors: errors.length === 0 }, null, 2));
  }
  assert.deepEqual(errors, []);
} catch (error) {
  if (page) await page.screenshot({ path: path.join(artifacts, `${fixture.mode}-failure.png`), fullPage: true });
  throw error;
} finally {
  try { await context?.close(); } finally { await browser.close(); }
}
