import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { appendFileSync } from "node:fs";
import { chmod, readFile, stat, writeFile } from "node:fs/promises";
import { join } from "node:path";
import { chromium } from "playwright";
import { expect } from "playwright/test";
import { TestRunContext } from "../../../../harness/js/run-context.mjs";
import { webSelectors } from "../../../../clients/selectors/web-selectors.mjs";
import { testControl } from "../../../../support/e2e_control/browser_client.mjs";
import { openWorkbench, selectThread } from "../persistent_tasks/workbench.mjs";

function environment(name) {
  assert(process.env[name], `缺少环境变量 ${name}`);
  return process.env[name];
}

const baseUrl = environment("BOXTEAM_BROWSER_BASE_URL");
const artifacts = environment("BOXTEAM_BROWSER_ARTIFACT_ROOT");
const owner = JSON.parse(environment("BOXTEAM_BROWSER_OWNER"));
const lockPath = environment("BOXTEAM_BROWSER_LOCK_PATH");
const canonicalPath = environment("BOXTEAM_BROWSER_CANONICAL_PATH");
const interactions = JSON.parse(environment("BOXTEAM_BROWSER_MODEL_CASSETTES")).map(cassette => cassette.interactions[0]);
const resources = TestRunContext.fromTestFile(environment("BOXTEAM_BROWSER_TEST_OWNER"));
const facts = { phase: "setup", owner, modelCalls: [], pageErrors: [], responses: [] };
let page;
let token;
let executionError;
let contextOrdinal = 0;

function stage(value) {
  facts.phase = value;
  appendFileSync(join(artifacts, "browser-stages.jsonl"), JSON.stringify({
    phase: value, at: new Date().toISOString(), memory: process.memoryUsage(),
  }) + "\n");
}

async function api(path, data) {
  const sent = randomUUID();
  const response = await page.request.fetch(new URL(path, baseUrl).href, {
    method: data === undefined ? "GET" : "POST", timeout: 30_000,
    headers: { "X-Local-Token": token, "X-BoxTeam-Target-Gateway-Id": owner.gatewayId, "X-BoxTeam-Workspace-Id": owner.workspaceId, "X-Request-ID": sent },
    ...(data === undefined ? {} : { data }),
  });
  try {
    const body = await response.json();
    assert.equal(response.headers()["x-request-id"], sent);
    assert.equal(body.request_id, sent);
    facts.responses.push({ path, status: response.status(), requestId: sent });
    assert.equal(response.status(), 200, JSON.stringify(body));
    return body.data;
  } finally { await response.dispose(); }
}

async function history() {
  const body = await api(`/api/v1/sessions/${owner.sessionId}/threads/${owner.threadId}/history`, {
    direction: "tail", turns: 10, include: ["user", "text", "reasoning_detail", "tool_call", "tool_result", "final_response"],
  });
  assert.equal(body.thread_id, owner.threadId);
  assert.equal(body.has_more, false);
  assert(body.items.every(turn => turn.session_id === owner.sessionId
    && turn.source_view_id === body.source_view_id && turn.source_view_revision === body.source_view_revision));
  return body;
}

async function send(thread, prompt, reply, planned) {
  facts.modelCalls.push(...await testControl({ action: "plan_model", session_id: owner.sessionId,
    thread_id: owner.threadId, interactions: planned }));
  const accepted = page.waitForResponse(response => response.request().method() === "POST"
    && new URL(response.url()).pathname === `/api/v1/sessions/${owner.sessionId}/threads/${owner.threadId}/messages`
    && response.request().headers()["x-boxteam-workspace-id"] === owner.workspaceId);
  await thread.locator(webSelectors.composer).fill(prompt);
  await thread.locator(webSelectors.sendButton).click();
  const response = await accepted;
  const body = await response.json();
  assert.equal(response.status(), 200, JSON.stringify(body));
  assert.equal(response.headers()["x-request-id"], body.request_id);
  assert.equal(body.data.thread_id, owner.threadId);
  assert.equal(response.request().postDataJSON().message.content, prompt);
  await expect(thread.locator(`.chat-turn[data-turn-id="${body.data.turn_id}"]`).getByText(reply, { exact: true }))
    .toBeVisible({ timeout: 30_000 });
  await expect.poll(async () => (await api(`/api/v1/jobs/${body.data.job_id}`)).status, { timeout: 30_000 }).toBe("completed");
  return body.data;
}

async function newPage(browser) {
  const context = await browser.newContext({ viewport: { width: 1600, height: 1100 } });
  const ordinal = ++contextOrdinal;
  let traceStarted = false;
  let closed = false;
  const close = async () => {
    if (closed) return;
    closed = true;
    const errors = [];
    if (traceStarted) {
      try { await context.tracing.stop({ path: join(artifacts, `browser-trace-${ordinal}.zip`) }); }
      catch (error) { errors.push(error); }
    }
    try { await context.close(); } catch (error) { errors.push(error); }
    if (errors.length) throw new AggregateError(errors, "历史故障 context 清理失败");
  };
  resources.addCleanup("历史故障 browser context", close);
  await context.tracing.start({ screenshots: true, snapshots: false, sources: false });
  traceStarted = true;
  page = await context.newPage();
  page.on("pageerror", error => facts.pageErrors.push(String(error)));
  await openWorkbench(page, baseUrl);
  token = await page.evaluate(async () => {
    const response = await fetch('/api/gateway/auth/local-credential');
    if (!response.ok) throw new Error(`本机凭据读取失败：${response.status}`);
    return (await response.json()).data.token;
  });
  return { context, close };
}

try {
  stage("setup");
  const browser = await chromium.launch({ executablePath: environment("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH"), headless: true });
  resources.addCleanup("历史故障 Chromium", () => browser.close());
  const firstContext = await newPage(browser);
  const registry = await api("/api/gateway/workspaces");
  assert.deepEqual(registry.active_workspace, { gateway_id: owner.gatewayId, workspace_id: owner.workspaceId });
  await api("/api/gateway/health");
  assert.equal((await api("/api/v1/workspace")).workspace_id, owner.workspaceId);
  const thread = await selectThread(page, owner, 0);
  stage("produce-tool-history");
  facts.initialTurn = await send(thread, "读取 README.md，生成用于故障恢复的完整工具历史。",
    "存储故障前的完整工具历史。", interactions.slice(0, 2));
  facts.initialHistory = await history();
  assert.deepEqual(facts.initialHistory.items.map(turn => turn.turn_id), [facts.initialTurn.turn_id]);
  const originalBytes = await readFile(canonicalPath);
  const originalLock = await stat(lockPath);
  assert(originalLock.isFile());
  await firstContext.close();
  // 只改变当前隔离 Thread 的访问权限，不替换稳定锁文件，不改数据库或执行记录。
  resources.addCleanup("恢复原生锁权限", () => chmod(lockPath, originalLock.mode & 0o777));
  await chmod(lockPath, 0);
  await assert.rejects(readFile(lockPath), error => {
    facts.nativeFailure = { code: error.code, errno: error.errno, path: error.path };
    return error.code === "EACCES" && error.path === lockPath;
  });
  stage("real-storage-failure");
  // 新 BrowserContext 没有之前的 IndexedDB 缓存，不能让缓存正文掩盖读取失败。
  await newPage(browser);
  const isBootstrap = response => {
    const url = new URL(response.url());
    return url.pathname === `/api/v1/sessions/${owner.sessionId}/bootstrap`
      && url.searchParams.get("thread_id") === owner.threadId
      && response.request().headers()["x-boxteam-workspace-id"] === owner.workspaceId;
  };
  const failedResponse = page.waitForResponse(isBootstrap);
  const failedThread = await selectThread(page, owner, 0);
  const failed = await failedResponse;
  const failedBody = await failed.json();
  facts.failure = { status: failed.status(), body: failedBody, requestId: failed.headers()["x-request-id"] };
  assert.equal(failed.status(), 500, JSON.stringify(failedBody));
  assert.equal(failedBody.request_id, facts.failure.requestId);
  assert(facts.failure.requestId);
  assert.equal(failedBody.detail.code, "turn_projection_corrupted");
  assert.equal(failedBody.detail.session_id, owner.sessionId);
  assert(failedBody.detail.message.includes("stage=history_read, error=PermissionError, errno=13"));
  assert(!JSON.stringify(failedBody).includes(lockPath));
  await expect(failedThread.getByRole("alert")).toContainText(failedBody.detail.message);
  facts.failure.visibleMessage = await failedThread.getByRole("alert").innerText();
  assert(!(await failedThread.innerText()).includes(lockPath));
  await expect(failedThread.getByText("该会话暂无历史消息", { exact: true })).toHaveCount(0);
  await expect(failedThread.getByText("存储故障前的完整工具历史。", { exact: true })).toHaveCount(0);
  const draft = "恢复后继续原 Thread，保留之前的工具结果。";
  await failedThread.locator(webSelectors.composer).fill(draft);
  assert.deepEqual(await readFile(canonicalPath), originalBytes);
  assert.equal((await stat(lockPath)).ino, originalLock.ino);
  stage("retry-history");
  await chmod(lockPath, originalLock.mode & 0o777);
  const retriedResponse = page.waitForResponse(isBootstrap);
  await failedThread.getByRole("button", { name: "重试加载", exact: true }).click();
  const retried = await retriedResponse;
  const retriedBody = await retried.json();
  assert.equal(retried.status(), 200, JSON.stringify(retriedBody));
  assert.equal(retried.headers()["x-request-id"], retriedBody.request_id);
  assert.equal(retriedBody.data.thread_id, owner.threadId);
  assert.equal(retriedBody.data.session.session_id, owner.sessionId);
  assert.equal(retriedBody.data.session.workspace_id, owner.workspaceId);
  facts.retry = { status: retried.status(), body: retriedBody, requestId: retriedBody.request_id };
  await expect(failedThread.getByText("存储故障前的完整工具历史。", { exact: true })).toBeVisible({ timeout: 30_000 });
  await expect(failedThread.getByRole("alert")).toHaveCount(0);
  await expect(failedThread.locator(webSelectors.composer)).toHaveValue(draft);
  facts.recoveredHistory = await history();
  assert.deepEqual(facts.recoveredHistory, facts.initialHistory);
  assert.deepEqual(await readFile(canonicalPath), originalBytes);
  assert.equal((await stat(lockPath)).ino, originalLock.ino);
  assert.equal((await stat(lockPath)).mode & 0o777, originalLock.mode & 0o777);
  stage("continue-original-thread");
  facts.successor = await send(failedThread, draft, "权限恢复后原 Thread 继续成功。", interactions.slice(2));
  facts.continuedHistory = await history();
  assert.deepEqual(facts.continuedHistory.items.map(turn => turn.turn_id), [facts.initialTurn.turn_id, facts.successor.turn_id]);
  // 新 Turn 推进当前视图坐标；原 Turn 的身份、顺序、正文和时间等其它字段必须逐项不变。
  assert(facts.continuedHistory.source_view_revision > facts.initialHistory.source_view_revision);
  assert.deepEqual(facts.continuedHistory.items[0], { ...facts.initialHistory.items[0],
    source_view_id: facts.continuedHistory.source_view_id,
    source_view_revision: facts.continuedHistory.source_view_revision });
  assert.deepEqual((await readFile(canonicalPath)).subarray(0, originalBytes.length), originalBytes);
  assert.deepEqual(facts.pageErrors, []);
  await page.screenshot({ path: join(artifacts, "history-storage-recovered.png"), fullPage: true });
  stage("complete");
} catch (error) {
  executionError = error;
  facts.error = String(error);
  if (page && !page.isClosed()) {
    try { await page.screenshot({ path: join(artifacts, "history-storage-failure.png"), fullPage: true }); }
    catch (diagnosticError) { executionError = new AggregateError([error, diagnosticError], "历史故障验收及截图失败"); }
  }
} finally {
  const failures = executionError ? [executionError] : [];
  try { await resources.close(); } catch (error) { failures.push(error); }
  try { await writeFile(join(artifacts, "history-storage-result.json"), JSON.stringify(facts, null, 2)); }
  catch (error) { failures.push(error); }
  if (failures.length) throw new AggregateError(failures, "历史故障验收失败，详见原始回执");
}
