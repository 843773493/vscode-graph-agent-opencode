import assert from "node:assert/strict";
import { readFile, writeFile } from "node:fs/promises";
import { chromium } from "playwright";

const phase = process.argv[2];
assert.ok(["before-unload", "after-unload"].includes(phase));
const required = (name) => {
  const value = process.env[name];
  assert.ok(value, `缺少环境变量 ${name}`);
  return value;
};
const baseUrl = required("BOXTEAM_BROWSER_BASE_URL");
const workspaceId = required("BOXTEAM_BROWSER_WORKSPACE_ID");
const artifacts = required("BOXTEAM_BROWSER_ARTIFACT_ROOT");
const result = { phase, started_at: new Date().toISOString(), checks: [], page_errors: [], console_errors: [], failed_requests: [] };
const browser = await chromium.launch({
  executablePath: required("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH"),
  headless: true, downloadsPath: artifacts,
});
const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
context.on("page", (opened) => {
  opened.on("pageerror", (error) => result.page_errors.push(error.message));
  opened.on("console", (message) => {
    if (message.type() === "error") result.console_errors.push(message.text());
  });
  opened.on("requestfailed", (request) => result.failed_requests.push({
    path: new URL(request.url()).pathname, error: request.failure()?.errorText,
  }));
});
const page = await context.newPage();
page.setDefaultTimeout(30000);

async function api(path, method = "GET", body, expectedStatus = 200) {
  const reply = await page.evaluate(async ({ path, method, body, workspaceId }) => {
    const credential = await fetch("/api/gateway/auth/local-credential");
    if (!credential.ok) throw new Error(`凭据读取失败 ${credential.status}`);
    const token = (await credential.json()).data.token;
    const response = await fetch(path, {
      method, credentials: "include",
      headers: { "X-Local-Token": token, "X-BoxTeam-Workspace-Id": workspaceId,
        ...(body === undefined ? {} : { "Content-Type": "application/json" }) },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    });
    return { status: response.status, request_id: response.headers.get("X-Request-ID"),
      body: await response.json() };
  }, { path, method, body, workspaceId });
  assert.equal(reply.status, expectedStatus, `${path}: ${JSON.stringify(reply.body)}`);
  assert.equal(reply.body.request_id, reply.request_id, `${path}: request_id 不一致`);
  return reply.body.data;
}
async function waitUntil(label, predicate, timeout = 30000) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    if (await predicate()) return;
    await new Promise((resolve) => setTimeout(resolve, 150));
  }
  throw new Error(`等待超时：${label}`);
}
async function selectSession(sessionId) {
  await page.reload({ waitUntil: "domcontentloaded" });
  await page.locator(`button[data-session-id="${sessionId}"]`).click();
  await page.locator("#input").waitFor({ state: "visible" });
}
async function createSession(title) {
  const session = await api("/api/v1/sessions", "POST", { title, agent_id: "default" });
  const main = await api(`/api/v1/sessions/${session.session_id}/main-thread`);
  return { session_id: session.session_id, thread_id: main.thread_id };
}
async function terminalPanel() {
  const panel = page.getByTestId("terminal-panel");
  if (!(await panel.isVisible())) await page.getByRole("button", { name: "切换底部面板", exact: true }).first().click();
  await page.getByRole("tab", { name: /^终端/ }).click();
  await panel.waitFor({ state: "visible" });
  return panel;
}
async function command(surface, label, action, resourceId, expectedStatus = 200) {
  const replyPromise = surface.page().waitForResponse((reply) => reply.request().method() === "POST"
    && new URL(reply.url()).pathname === `/api/v1/resources/${resourceId}/${action}`);
  await surface.getByRole("button", { name: label, exact: true }).click();
  if (action === "delete") {
    const dialog = surface.page().getByRole("dialog");
    await dialog.getByRole("button", { name: "删除", exact: true }).click();
  }
  const reply = await replyPromise;
  assert.equal(reply.status(), expectedStatus, await reply.text());
  const payload = await reply.json();
  assert.equal(payload.request_id, reply.headers()["x-request-id"]);
  result.checks.push({ resource_id: resourceId, action, status: reply.status() });
  return payload.data;
}
async function browserRow(popup, resourceId) {
  const rows = popup.locator('.resource-tree-item[data-resource-kind="browser"]');
  await rows.first().waitFor({ state: "visible" });
  for (const row of await rows.all()) {
    const toggle = row.locator(".resource-tree-chevron");
    if (await toggle.getAttribute("aria-expanded") === "false") await toggle.click();
  }
  const row = rows.filter({ has: popup.locator(`[title="${resourceId}"]`) });
  await row.waitFor({ state: "visible" });
  return row;
}
async function openBrowser(resourceId, sessionId) {
  const popup = await context.newPage();
  const url = new URL("/extension", baseUrl);
  for (const [key, value] of Object.entries({ resourceType: "browser", resourceId, workspaceId, sessionId })) {
    if (value) url.searchParams.set(key, value);
  }
  await popup.goto(url.toString(), { waitUntil: "domcontentloaded" });
  if (sessionId) {
    // 扩展窗口隐藏会话树；核验实际 bootstrap 的当前会话，不点击隐藏控件。
    const currentSession = popup.locator('.session-resource-row[aria-current="true"]')
      .filter({ has: popup.locator(`button[data-session-id="${sessionId}"]`) });
    await currentSession.waitFor({ state: "attached" });
  }
  return popup;
}
async function terminalInput(marker) {
  const frame = page.frameLocator(".terminal-panel-frame");
  const moduleFailures = () => result.failed_requests.filter((request) =>
    request.path.startsWith("/api/gateway/attach/terminal/")
    && request.error === "net::ERR_NETWORK_CHANGED");
  await waitUntil("终端连接或模块网络失败", async () =>
    await frame.locator("#attach-toggle.is-attached").isVisible() || moduleFailures().length > 0);
  if (!(await frame.locator("#attach-toggle.is-attached").isVisible())) {
    assert.equal(result.terminal_initialization_recovery, undefined, "一次页面重新加载后终端仍未连接");
    const sessionId = await page.locator('.session-resource-row[aria-current="true"] button[data-session-id]')
      .getAttribute("data-session-id");
    assert.ok(sessionId);
    await page.screenshot({ path: `${artifacts}/${phase}-terminal-network-failure.png`, fullPage: true });
    result.terminal_initialization_recovery = { action: "user_page_reload", failed_requests: moduleFailures() };
    // 实际页面重载并通过可见会话入口恢复；不拦截模块响应或重放业务请求。
    await selectSession(sessionId);
    await terminalPanel();
  }
  await frame.locator("#attach-toggle.is-attached").waitFor({ state: "visible" });
  const input = frame.locator(".xterm-helper-textarea");
  await input.focus();
  // 参数分开拼接，断言目标不会只因命令回显而命中。
  await input.pressSequentially(`printf 'PERSIST_%s_%s\\n' 'READY' '${marker}'`, { delay: 1 });
  await input.press("Enter");
  await waitUntil("真实 PTY 输出", async () => (await frame.locator(".xterm-rows").innerText()).includes(`PERSIST_READY_${marker}`));
}
async function attachedBrowser(popup) {
  const frame = popup.frameLocator("iframe.workspace-runtime-preview-frame.browser");
  await frame.locator("#attach-toggle.is-attached, #attach-state-badge.failed").waitFor({ state: "attached" });
  if (await frame.locator("#attach-state-badge.failed").count()) {
    const failures = result.failed_requests.filter((request) => request.path.startsWith("/api/gateway/attach/")
      && request.error === "net::ERR_NETWORK_CHANGED");
    assert.ok(failures.length > 0, "Browser 初始化失败，未发现模块请求的网络变更证据");
    // 只执行一次产品已有的用户重新加载，保留首次失败和恢复操作证据。
    result.browser_initialization_recovery = { action: "user_reload", failed_requests: failures };
    await frame.locator("#screen-overlay").click();
  }
  await frame.locator("#attach-toggle.is-attached").waitFor({ state: "visible" });
  return frame;
}
async function deleteSession(sessionId) {
  const snapshot = await api("/api/v1/session-catalog/snapshot");
  const operationId = `op_${Bun.randomUUIDv7().replaceAll("-", "")}`;
  const accepted = await api("/api/v1/session-catalog/operations:enqueue", "POST", {
    intents: [{ client_operation_id: operationId, client_sequence: 1, kind: "delete_session",
      base_catalog_revision: snapshot.catalog_revision, target_node_id: sessionId }],
  }, 202);
  assert.equal(accepted.accepted_count, 1);
  await waitUntil("正式 Session 删除", async () => {
    const operations = await api(`/api/v1/session-catalog/operations?operation_id=${operationId}`);
    const receipt = operations.items.find((item) => item.operation_id === operationId);
    assert.ok(receipt);
    assert.ok(!["rejected", "cancelled", "dependency_failed"].includes(receipt.state), JSON.stringify(receipt));
    return receipt.state === "committed" && !receipt.pending_settlement;
  });
}
try {
  await page.goto(baseUrl, { waitUntil: "domcontentloaded" });
  const status = await page.evaluate(async () => (await fetch("/api/gateway/users/current")).status);
  if (status === 401) {
    await api("/api/gateway/users/guest", "POST", { tracking: { source: "persistent-resource-web-e2e" } });
    await page.reload({ waitUntil: "domcontentloaded" });
  } else assert.equal(status, 200);
  await api("/api/gateway/health");
  assert.equal((await api("/api/gateway/workspaces")).active_workspace_id, workspaceId);
  await api("/api/v1/workspace");
  if (phase === "before-unload") {
    const source = await createSession("持久资源真实卸载验收");
    await selectSession(source.session_id);
    const sentPromise = page.waitForResponse((reply) => reply.request().method() === "POST"
      && new URL(reply.url()).pathname === `/api/v1/sessions/${source.session_id}/messages`);
    await page.locator("#input").fill("只回复“就绪”，不要调用工具。");
    await page.locator("#sendButton").click();
    const sent = await sentPromise;
    assert.equal(sent.status(), 200, await sent.text());
    const jobId = (await sent.json()).data.job_id;
    await waitUntil("真实模型 Job 完成并使 Thread 驻留", async () => {
      const job = await api(`/api/v1/jobs/${jobId}`);
      assert.ok(!["failed", "cancelled", "timed_out"].includes(job.status), JSON.stringify(job));
      return ["completed", "succeeded"].includes(job.status);
    }, 120000);
    result.real_model_job_completed = true;
    result.source = source;
    result.job_id = jobId;
    const panel = await terminalPanel();
    const createTerminal = page.waitForResponse((reply) => reply.request().method() === "POST"
      && new URL(reply.url()).pathname === "/api/v1/resources/terminals");
    await panel.getByLabel("终端保留范围").selectOption("thread");
    await panel.getByRole("button", { name: "新建终端", exact: true }).click();
    const terminalReply = await createTerminal;
    assert.equal(terminalReply.status(), 200, await terminalReply.text());
    const terminal = (await terminalReply.json()).data;
    assert.deepEqual(terminal.source_thread, source);
    assert.equal(terminal.retention_scope, "thread");
    result.terminal_id = terminal.resource_id;
    await terminalInput("BEFORE");
    assert.equal((await command(panel, "保留到工作区", "promote", terminal.resource_id)).retention_scope, "workspace");
    await page.getByRole("tab", { name: "运行与连接", exact: true }).click();
    const connections = page.locator(".resource-panel").filter({ has: page.getByRole("button", { name: "新建连接", exact: true }) });
    await connections.getByRole("button", { name: "新建连接", exact: true }).click();
    const createdPromise = page.waitForResponse((reply) => reply.request().method() === "POST"
      && new URL(reply.url()).pathname === "/api/v1/resources/browsers");
    const popupPromise = page.waitForEvent("popup");
    await page.getByRole("menuitem", { name: /新建浏览器/ }).click();
    const created = await createdPromise;
    assert.equal(created.status(), 200, await created.text());
    const resource = (await created.json()).data;
    assert.deepEqual(resource.source_thread, source);
    result.browser_id = resource.resource_id;
    const popup = await popupPromise;
    await popup.waitForLoadState("domcontentloaded");
    const frame = await attachedBrowser(popup);
    const targetUrl = `${baseUrl}/api/gateway/health`;
    await frame.locator("#address-input").fill(targetUrl);
    await frame.locator("#address-input").press("Enter");
    await waitUntil("真实 Browser 导航", async () => (await api(`/api/v1/resources/${resource.resource_id}`)).metadata.url === targetUrl);
    const promoted = await command(await browserRow(popup, resource.resource_id), "保留到工作区", "promote", resource.resource_id);
    assert.equal(promoted.resource_id, resource.resource_id);
    assert.equal(promoted.retention_scope, "workspace");
    result.kinds = ["terminal", "browser"];
  } else {
    const previous = JSON.parse(await readFile(`${artifacts}/before-unload.json`, "utf8"));
    assert.equal(previous.passed, true);
    Object.assign(result, { source: previous.source, terminal_id: previous.terminal_id,
      browser_id: previous.browser_id, real_model_job_completed: previous.real_model_job_completed,
      kinds: previous.kinds });
    for (const resourceId of [result.terminal_id, result.browser_id]) {
      const resource = await api(`/api/v1/resources/${resourceId}`);
      assert.equal(resource.resource_id, resourceId);
      assert.equal(resource.retention_scope, "workspace");
      assert.equal(resource.status, "running");
      assert.deepEqual(resource.thread_associations, []);
      assert.deepEqual(resource.source_thread, result.source);
    }
    result.unload_associations_removed = true;
    const other = await createSession("持久资源另一 Thread");
    await selectSession(other.session_id);
    const panel = await terminalPanel();
    const terminal = await api(`/api/v1/resources/${result.terminal_id}`);
    await panel.getByRole("button", { name: String(terminal.metadata.title).replace(/^终端\s*[:：]?\s*/, "") }).click();
    const attached = await command(panel, "关联当前会话", "attach", result.terminal_id);
    assert.deepEqual(attached.thread_associations, [other]);
    await terminalInput("AFTER");
    assert.deepEqual((await command(panel, "解除关联", "detach", result.terminal_id)).thread_associations, []);
    const popup = await openBrowser(result.browser_id, other.session_id);
    let row = await browserRow(popup, result.browser_id);
    assert.deepEqual((await command(row, "关联当前会话", "attach", result.browser_id)).thread_associations, [other]);
    const beforeConflict = await api(`/api/v1/resources/${result.browser_id}`);
    const refreshPromise = popup.waitForResponse((reply) => new URL(reply.url()).pathname === "/api/gateway/resources" && reply.status() === 200);
    await command(row, "删除", "delete", result.browser_id, 409);
    await refreshPromise;
    await popup.locator(".resource-notice.is-error").waitFor({ state: "visible" });
    assert.deepEqual((await api(`/api/v1/resources/${result.browser_id}`)).thread_associations, beforeConflict.thread_associations);
    result.conflict_shown_and_owner_refetched = true;
    row = await browserRow(popup, result.browser_id);
    assert.deepEqual((await command(row, "解除关联", "detach", result.browser_id)).thread_associations, []);
    result.other_thread_attach_detach = true;
    await deleteSession(result.source.session_id);
    await selectSession(other.session_id);
    await terminalPanel();
    await terminalInput("SESSION_DELETED");
    await popup.close();
    const retained = await openBrowser(result.browser_id);
    const frame = await attachedBrowser(retained);
    for (const resourceId of [result.terminal_id, result.browser_id]) {
      assert.equal((await api(`/api/v1/resources/${resourceId}`)).status, "running");
    }
    result.session_deleted_resources_accessible = true;
    await command(await terminalPanel(), "停止", "stop", result.terminal_id);
    await frame.locator("#close-browser").click();
    await waitUntil("真实 Browser 停止", async () => (await api(`/api/v1/resources/${result.browser_id}`)).status === "closed");
    for (const resourceId of [result.terminal_id, result.browser_id]) {
      const resource = await api(`/api/v1/resources/${resourceId}`);
      await api(`/api/v1/resources/${resourceId}/delete`, "POST", { expected_revision: resource.owner_revision });
    }
    await deleteSession(other.session_id);
  }
  assert.deepEqual(result.page_errors, []);
  result.passed = true;
} catch (error) {
  result.passed = false;
  result.error = error.stack || String(error);
  result.pages = await Promise.all(context.pages().map(async (opened, index) => {
    const frames = await Promise.all(opened.frames().map(async (frame) => ({
      path: new URL(frame.url()).pathname,
      attachment: await frame.locator("#attach-toggle").evaluateAll((nodes) => nodes.map((node) => ({
        className: node.className, label: node.getAttribute("aria-label"),
        status: document.querySelector("#status-line")?.getAttribute("aria-label")
          || document.querySelector("#status-line")?.textContent,
      }))),
      notices: await frame.locator(".resource-notice, #status-line").allTextContents(),
    })));
    await opened.screenshot({ path: `${artifacts}/${phase}-failure-${index}.png`, fullPage: true });
    return { path: new URL(opened.url()).pathname, frames };
  }));
  process.exitCode = 1;
} finally {
  result.finished_at = new Date().toISOString();
  await writeFile(`${artifacts}/${phase}.json`, JSON.stringify(result, null, 2) + "\n");
  await browser.close();
}
