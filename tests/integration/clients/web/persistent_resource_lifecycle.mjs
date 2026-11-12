import assert from "node:assert/strict";
import { readFile, writeFile } from "node:fs/promises";
import { chromium } from "playwright";
import { planChatCalls } from "../../../support/e2e_control/browser_client.mjs";

const phase = process.argv[2];
assert.ok(["before-unload", "after-unload", "workspace-browser"].includes(phase));
const required = (name) => {
  const value = process.env[name];
  assert.ok(value, `缺少环境变量 ${name}`);
  return value;
};
const baseUrl = required("BOXTEAM_BROWSER_BASE_URL");
const workspaceId = required("BOXTEAM_BROWSER_WORKSPACE_ID");
const gatewayId = required("BOXTEAM_BROWSER_GATEWAY_ID");
const artifacts = required("BOXTEAM_BROWSER_ARTIFACT_ROOT");
const result = { phase, started_at: new Date().toISOString(), checks: [], page_errors: [], console_errors: [], failed_requests: [], http_errors: [], deleted_sessions: [] };
const pendingResponses = new Set();
let step = "initialize";
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
let activeOwner;
let activeThread;

function recordHttpError(response) {
  if (response.status() < 400) return;
  const request = response.request();
  const record = {
    phase, step, method: request.method(), path: new URL(response.url()).pathname,
    query: new URL(response.url()).search, status: response.status(),
    gateway_id: gatewayId, workspace_id: workspaceId,
    active_owner: activeOwner ? { ...activeOwner } : null,
    request_id: response.headers()["x-request-id"],
  };
  const deletion = result.deleted_sessions.find((item) => record.method === "GET"
    && record.path === `/api/v1/sessions/${item.session_id}`);
  // 只冻结响应发生时已经取得的删除回执，禁止以之后的删除追认较早的404。
  record.deletion_proof = deletion ? { ...deletion } : null;
  result.http_errors.push(record);
  // 异步响应采集必须排空；正文读取失败也保留，不能仅凭状态码放行。
  const capture = (async () => {
    const headers = await request.allHeaders();
    record.request_workspace_id = headers["x-boxteam-workspace-id"] ?? null;
    record.request_gateway_id = headers["x-boxteam-target-gateway-id"] ?? null;
    record.request_body = request.postData();
    record.body = await response.text();
  })().catch((error) => { record.collection_error = error.stack || String(error); });
  pendingResponses.add(capture);
  void capture.then(() => pendingResponses.delete(capture));
}
context.on("response", recordHttpError);

async function api(path, method = "GET", body, expectedStatus = 200) {
  const reply = await page.evaluate(async ({ path, method, body, workspaceId, gatewayId }) => {
    const credential = await fetch("/api/gateway/auth/local-credential");
    if (!credential.ok) throw new Error(`凭据读取失败 ${credential.status}`);
    const token = (await credential.json()).data.token;
    const response = await fetch(path, {
      method, credentials: "include",
      headers: { "X-Local-Token": token, "X-BoxTeam-Workspace-Id": workspaceId, "X-BoxTeam-Target-Gateway-Id": gatewayId,
        ...(body === undefined ? {} : { "Content-Type": "application/json" }) },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    });
    return { status: response.status, request_id: response.headers.get("X-Request-ID"),
      body: await response.json() };
  }, { path, method, body, workspaceId, gatewayId });
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
  const main = await api(`/api/v1/sessions/${sessionId}/main-thread`);
  activeOwner = { session_id: sessionId, thread_id: main.thread_id };
  await page.getByRole("button", { name: "刷新资源树", exact: true }).click();
  await page.locator(`button[data-session-id="${sessionId}"]`).dblclick();
  activeThread = page.locator(`section.boxteam-thread[data-workspace-id="${workspaceId}"][data-session-id="${sessionId}"][data-thread-id="${main.thread_id}"]`);
  await activeThread.locator("#input").waitFor({ state: "visible" });
}
async function createSession(title) {
  const session = await api("/api/v1/sessions", "POST", { title, agent_id: "default" });
  const main = await api(`/api/v1/sessions/${session.session_id}/main-thread`);
  return { session_id: session.session_id, thread_id: main.thread_id };
}
async function terminalPanel() {
  await page.evaluate(() => globalThis.workbench.commands.executeCommand("boxteam.terminal.focus"));
  const panel = page.locator(`.native-workspace-panel[data-panel-kind="terminal"][data-workspace-id="${workspaceId}"]`).getByTestId("terminal-panel");
  await panel.waitFor({ state: "visible" });
  return panel;
}
async function command(surface, label, action, resourceId, {
  expectedStatus = 200, expectedOwner, beforeConfirm, expectedRevision,
} = {}) {
  const [response, operation] = await Promise.allSettled([
    surface.page().waitForResponse((reply) => reply.request().method() === "POST"
      && new URL(reply.url()).pathname === `/api/v1/resources/${resourceId}/${action}`),
    (async () => {
      await surface.getByRole("button", { name: label, exact: true }).click();
      if (action === "delete") {
        const dialog = surface.page().getByRole("dialog", { name: "删除浏览器", exact: true });
        await dialog.waitFor({ state: "visible" });
        if (beforeConfirm) await beforeConfirm();
        await dialog.getByRole("button", { name: "删除", exact: true }).click();
      }
    })(),
  ]);
  if (operation.status === "rejected") throw operation.reason;
  if (response.status === "rejected") throw response.reason;
  const reply = response.value;
  const requestOwner = reply.request().postDataJSON();
  if (expectedOwner) {
    assert.deepEqual(requestOwner.thread, expectedOwner);
  }
  if (expectedRevision !== undefined) assert.equal(requestOwner.expected_revision, expectedRevision);
  assert.equal(reply.status(), expectedStatus, await reply.text());
  const payload = await reply.json();
  assert.equal(payload.request_id, reply.headers()["x-request-id"]);
  result.checks.push({ resource_id: resourceId, action, status: reply.status(),
    request_revision: requestOwner.expected_revision,
    ...(expectedOwner ? { request_owner: requestOwner.thread } : {}) });
  return payload.data;
}
async function browserRow(popup, resourceId) {
  const rows = popup.locator('.resource-tree-item[data-resource-kind="browser"]');
  await rows.first().waitFor({ state: "visible" });
  for (const row of await rows.all()) {
    const toggle = row.locator(".resource-tree-chevron");
    if (await toggle.getAttribute("aria-expanded") === "false") await toggle.click();
  }
  const row = rows.filter({ has: page.locator(`[title="${resourceId}"]`) });
  await row.waitFor({ state: "visible" });
  return row;
}
async function browserPanel() {
  await page.evaluate(() => globalThis.workbench.commands.executeCommand("boxteam.browser.focus"));
  const panel = page.locator(`.native-workspace-panel[data-panel-kind="browser"][data-workspace-id="${workspaceId}"]`).getByRole("region", { name: "工作区浏览器", exact: true });
  await panel.waitFor({ state: "visible" });
  return panel;
}
async function openBrowser(resourceId, sessionId) {
  if (sessionId) await selectSession(sessionId);
  const panel = await browserPanel();
  await (await browserRow(panel, resourceId)).locator(".resource-tree-main").click();
  await attachedBrowser(panel);
  return panel;
}
async function terminalInput(marker) {
  const frame = (await terminalPanel()).frameLocator(".terminal-panel-frame");
  const moduleFailures = () => result.failed_requests.filter((request) =>
    request.path.startsWith("/api/gateway/attach/terminal/")
    && request.error === "net::ERR_NETWORK_CHANGED");
  await waitUntil("终端连接或模块网络失败", async () =>
    await frame.locator("#attach-toggle.is-attached").isVisible() || moduleFailures().length > 0);
  if (!(await frame.locator("#attach-toggle.is-attached").isVisible())) {
    assert.equal(result.terminal_initialization_recovery, undefined, "一次页面重新加载后终端仍未连接");
    const sessionId = activeOwner.session_id;
    assert.ok(sessionId);
    await page.screenshot({ path: `${artifacts}/${phase}-terminal-network-failure.png`, fullPage: true });
    result.terminal_initialization_recovery = { action: "user_page_reload", failed_requests: moduleFailures() };
    // 实际页面重载并通过可见会话入口恢复；不拦截模块响应或重放业务请求。
    await page.reload({ waitUntil: "domcontentloaded" });
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
    assert.notEqual(receipt.settlement, "repair_required", `删除排空需修复: ${receipt.error_detail}`);
    const settled = receipt.state === "committed" && receipt.settlement === "settled";
    if (settled) result.deleted_sessions.push({ session_id: sessionId, operation_id: operationId,
      state: receipt.state, settlement: receipt.settlement });
    return settled;
  });
}
async function auditHttpErrors() {
  context.off("response", recordHttpError);
  await Promise.all([...pendingResponses]);
  for (const error of result.http_errors) {
    assert.equal(error.collection_error, undefined, JSON.stringify(error));
    if (!error.body) {
      const abortedAttachAsset = error.method === "GET" && error.status === 404
        && error.path === "/api/gateway/attach/browser/vendor/codicon/codicon.css"
        && error.request_id === null;
      assert.ok(abortedAttachAsset, JSON.stringify(error));
      error.audit = "aborted_attach_asset_request";
      continue;
    }
    const body = JSON.parse(error.body);
    assert.ok(typeof error.request_id === "string" && error.request_id.length > 0, JSON.stringify(error));
    assert.equal(body.request_id, error.request_id, JSON.stringify(error));
    const conflict = phase === "after-unload" && error.method === "POST"
      && error.path === `/api/v1/resources/${result.browser_id}/delete` && error.status === 409;
    if (conflict) {
      assert.match(body.detail, /版本冲突/);
      assert.equal(error.request_workspace_id, workspaceId);
      assert.equal(error.request_gateway_id, gatewayId);
      assert.equal(JSON.parse(error.request_body).expected_revision,
        result.conflict_concurrent_owner_change.before_revision);
      assert.deepEqual(error.active_owner, result.conflict_concurrent_owner_change.thread);
    }
    const deleted = error.method === "GET" && error.status === 404
      && error.deletion_proof?.state === "committed" && error.deletion_proof.settlement === "settled"
      && typeof error.deletion_proof.operation_id === "string" && error.deletion_proof.operation_id.length > 0
      && error.path === `/api/v1/sessions/${error.deletion_proof.session_id}`;
    error.audit = conflict ? "explicit_browser_revision_conflict" : deleted ? "committed_session_deleted" : "unclassified";
  }
  assert.deepEqual(result.http_errors.filter((error) => error.audit === "unclassified"), [],
    "存在未归因的HTTP错误，保留正文与精确请求身份，不按时序豁免");
}
try {
  await page.goto(new URL("/", baseUrl).href, { waitUntil: "domcontentloaded" });
  assert.equal((await api("/api/gateway/health")).gateway_id, gatewayId);
  assert.deepEqual((await api("/api/gateway/workspaces")).active_workspace,
    { gateway_id: gatewayId, workspace_id: workspaceId });
  assert.equal((await api("/api/v1/workspace")).workspace_id, workspaceId);
  if (phase === "before-unload") {
    const source = await createSession("持久资源真实卸载验收");
    await planChatCalls(source.session_id, source.thread_id, [{ label: "resource-ready", text: "就绪" }]);
    await selectSession(source.session_id);
    const sentPromise = page.waitForResponse((reply) => reply.request().method() === "POST"
      && new URL(reply.url()).pathname === `/api/v1/sessions/${source.session_id}/threads/${source.thread_id}/messages`);
    await activeThread.locator("#input").fill("只回复“就绪”，不要调用工具。");
    await activeThread.locator("#sendButton").click();
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
    const toggle = activeThread.getByRole("button", { name: "切换会话资源", exact: true });
    if (await toggle.getAttribute("aria-expanded") !== "true") await toggle.click();
    await activeThread.getByRole("tab", { name: "运行与连接", exact: true }).click();
    const connections = activeThread.locator(".resource-panel");
    await connections.getByRole("button", { name: "新建连接", exact: true }).click();
    const createdPromise = page.waitForResponse((reply) => reply.request().method() === "POST"
      && new URL(reply.url()).pathname === "/api/v1/resources/browsers");
    await page.getByRole("menuitem", { name: /新建浏览器/ }).click();
    const created = await createdPromise;
    assert.equal(created.status(), 200, await created.text());
    const resource = (await created.json()).data;
    assert.deepEqual(resource.source_thread, source);
    result.browser_id = resource.resource_id;
    const frame = await attachedBrowser(activeThread);
    const targetUrl = `${baseUrl}/api/gateway/health`;
    const browserOperationRevision = await frame.locator("html").getAttribute("data-browser-operation-revision");
    await frame.locator("#address-input").fill(targetUrl);
    await frame.locator("#address-input").press("Enter");
    await waitUntil("真实 Browser 导航 operationAck", async () => {
      const revision = await frame.locator("html").getAttribute("data-browser-operation-revision");
      return revision !== null && revision !== browserOperationRevision;
    });
    await waitUntil("真实 Browser 导航", async () => (await api(`/api/v1/resources/${resource.resource_id}`)).metadata.url === targetUrl);
    const promoted = await command(await browserRow(activeThread, resource.resource_id), "保留到工作区", "promote", resource.resource_id);
    assert.equal(promoted.resource_id, resource.resource_id);
    assert.equal(promoted.retention_scope, "workspace");
    result.kinds = ["terminal", "browser"];
  } else if (phase === "after-unload") {
    step = "restore-retained-resources";
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
    const attached = await command(panel, "关联当前会话", "attach", result.terminal_id, { expectedOwner: other });
    assert.deepEqual(attached.thread_associations, [other]);
    await terminalInput("AFTER");
    assert.deepEqual((await command(panel, "解除关联", "detach", result.terminal_id)).thread_associations, []);
    const popup = await openBrowser(result.browser_id, other.session_id);
    let row = await browserRow(popup, result.browser_id);
    assert.deepEqual((await command(row, "关联当前会话", "attach", result.browser_id, { expectedOwner: other })).thread_associations, [other]);
    assert.deepEqual((await command(row, "解除关联", "detach", result.browser_id)).thread_associations, []);
    row = await browserRow(popup, result.browser_id);
    const beforeConflict = await api(`/api/v1/resources/${result.browser_id}`);
    const refreshPromise = page.waitForResponse((reply) => new URL(reply.url()).pathname === "/api/v1/resources" && new URL(reply.url()).searchParams.get("kind") === "browser" && reply.status() === 200);
    await command(row, "删除", "delete", result.browser_id, {
      expectedStatus: 409, expectedRevision: beforeConflict.owner_revision, beforeConfirm: async () => {
      // 用户打开删除确认后，另一客户端真实关联资源，使原确认持有的 revision 失效。
      const changed = await api(`/api/v1/resources/${result.browser_id}/attach`, "POST", {
        expected_revision: beforeConflict.owner_revision, thread: other,
      });
      assert.deepEqual(changed.thread_associations, [other]);
      assert.ok(changed.owner_revision > beforeConflict.owner_revision);
      result.conflict_concurrent_owner_change = {
        before_revision: beforeConflict.owner_revision, after_revision: changed.owner_revision,
        thread: other,
      };
      },
    });
    await refreshPromise;
    await popup.locator(".resource-notice.is-error").waitFor({ state: "visible" });
    assert.deepEqual((await api(`/api/v1/resources/${result.browser_id}`)).thread_associations, [other]);
    result.conflict_shown_and_owner_refetched = true;
    row = await browserRow(popup, result.browser_id);
    assert.deepEqual((await command(row, "解除关联", "detach", result.browser_id)).thread_associations, []);
    result.other_thread_attach_detach = true;
    step = "delete-source-session";
    await deleteSession(result.source.session_id);
    await selectSession(other.session_id);
    await terminalPanel();
    await terminalInput("SESSION_DELETED");
    const retained = await openBrowser(result.browser_id);
    const frame = await attachedBrowser(retained);
    for (const resourceId of [result.terminal_id, result.browser_id]) {
      assert.equal((await api(`/api/v1/resources/${resourceId}`)).status, "running");
    }
    result.session_deleted_resources_accessible = true;
    step = "stop-retained-resources";
    await frame.locator("#close-browser").click();
    await waitUntil("真实 Browser 停止", async () => (await api(`/api/v1/resources/${result.browser_id}`)).status === "closed");
    await command(await terminalPanel(), "停止", "stop", result.terminal_id);
    step = "delete-retained-resources";
    for (const resourceId of [result.terminal_id, result.browser_id]) {
      const resource = await api(`/api/v1/resources/${resourceId}`);
      await api(`/api/v1/resources/${resourceId}/delete`, "POST", { expected_revision: resource.owner_revision });
    }
    step = "delete-other-session";
    await deleteSession(other.session_id);
  } else {
    const other = await createSession("工作区浏览器创建和删除");
    await selectSession(other.session_id);
    const workspaceBrowsers = await browserPanel();
    await workspaceBrowsers.getByLabel("浏览器保留范围", { exact: true }).selectOption("workspace");
    const [workspaceCreated] = await Promise.all([
      page.waitForResponse(reply => reply.request().method() === "POST"
        && new URL(reply.url()).pathname === "/api/v1/resources/browsers"),
      workspaceBrowsers.getByRole("button", { name: "新建浏览器", exact: true }).click(),
    ]);
    assert.equal(workspaceCreated.status(), 200, await workspaceCreated.text());
    assert.equal(workspaceCreated.request().headers()["x-boxteam-workspace-id"], workspaceId);
    assert.equal(workspaceCreated.request().headers()["x-boxteam-target-gateway-id"], gatewayId);
    const request = workspaceCreated.request().postDataJSON();
    assert.deepEqual({ session_id: request.session_id, thread_id: request.thread_id }, other);
    const createdBody = await workspaceCreated.json();
    assert.equal(createdBody.request_id, workspaceCreated.headers()["x-request-id"]);
    const createdBrowser = createdBody.data;
    assert.equal(createdBrowser.retention_scope, "workspace");
    assert.deepEqual(createdBrowser.source_thread, other);
    assert.deepEqual(createdBrowser.thread_associations, [other]);
    await attachedBrowser(workspaceBrowsers);
    const detachedBrowser = await command(await browserRow(workspaceBrowsers, createdBrowser.resource_id), "解除关联", "detach", createdBrowser.resource_id,
      { expectedRevision: createdBrowser.owner_revision });
    assert.deepEqual(detachedBrowser.thread_associations, []);
    const removedBrowser = await command(await browserRow(workspaceBrowsers, createdBrowser.resource_id), "删除", "delete", createdBrowser.resource_id,
      { expectedRevision: detachedBrowser.owner_revision });
    assert.equal(removedBrowser.status, "deleted");
    result.workspace_browser_created_and_deleted = true;
    await page.reload({ waitUntil: "domcontentloaded" });
    await page.getByRole("button", { name: "刷新资源树", exact: true }).waitFor({ state: "visible" });
    await deleteSession(other.session_id);
  }
  assert.deepEqual(result.page_errors, []);
  await auditHttpErrors();
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
  context.off("response", recordHttpError);
  await Promise.all([...pendingResponses]);
  result.finished_at = new Date().toISOString();
  await writeFile(`${artifacts}/${phase}.json`, JSON.stringify(result, null, 2) + "\n");
  await browser.close();
}
