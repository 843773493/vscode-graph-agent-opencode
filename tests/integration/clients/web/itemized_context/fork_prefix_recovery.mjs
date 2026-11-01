import assert from "node:assert/strict";
import { execFile } from "node:child_process";
import { randomUUID } from "node:crypto";
import { writeFile } from "node:fs/promises";
import { join } from "node:path";
import { promisify } from "node:util";
import { chromium } from "playwright";
import { expect } from "playwright/test";
import { TestRunContext } from "../../../../harness/js/run-context.mjs";
import { webSelectors } from "../../../../clients/selectors/web-selectors.mjs";
import { beginForkOperationDeadline, closeForkDeadlineWatchdog, testControl,
  waitForForkOperation } from "../../../../support/e2e_control/browser_client.mjs";
import { openWorkbench, selectThread } from "../persistent_tasks/workbench.mjs";

function requiredEnvironment(name) {
  const value = process.env[name];
  assert(value, `缺少环境变量 ${name}`);
  return value;
}

const baseUrl = requiredEnvironment("BOXTEAM_BROWSER_BASE_URL");
const artifacts = requiredEnvironment("BOXTEAM_BROWSER_ARTIFACT_ROOT");
const interactions = JSON.parse(requiredEnvironment("BOXTEAM_BROWSER_MODEL_CASSETTES")).map(cassette => cassette.interactions[0]);
const prompts = ["较早提问：读取 README.md，保留完整工具上下文。", "后续提问：再次读取 README.md，供前缀排除核验。",
  "分支提问：仅沿用较早回答的上下文继续。"];
const replies = ["较早回答：保留这次工具结果。", "后续回答：不应进入较早分支。", "分支续聊：只使用较早的完整工具历史。"];
const resources = TestRunContext.fromTestFile(requiredEnvironment("BOXTEAM_BROWSER_TEST_OWNER"));
const facts = { phase: "setup", prompts, modelCalls: [], sourceTurnIds: [], pageErrors: [], responses: [] };
let page;
let token;
let executionError;

async function responseBody(pathname, owner, data, method = data === undefined ? "GET" : "POST") {
  const sentRequestId = randomUUID();
  const response = await page.request.fetch(new URL(pathname, baseUrl).href, {
    method,
    headers: { "X-Local-Token": token, "X-Request-ID": sentRequestId,
      ...(owner ? { "X-BoxTeam-Workspace-Id": owner.routeId } : {}) },
    ...(data === undefined ? {} : { data }), timeout: 60_000,
  });
  try {
    const body = await response.json();
    const requestId = response.headers()["x-request-id"];
    assert.equal(requestId, sentRequestId);
    facts.responses.push({ path: pathname, status: response.status(), requestId });
    assert.equal(response.status(), 200, JSON.stringify(body));
    return { body, requestId };
  } finally { await response.dispose(); }
}

async function api(...args) {
  const { body, requestId } = await responseBody(...args);
  assert.equal(body.request_id, requestId);
  return body.data;
}

async function history(owner) {
  const data = await api(`/api/v1/sessions/${owner.sessionId}/threads/${owner.threadId}/history`, owner, {
    direction: "tail", turns: 10, include: ["user", "text", "reasoning_detail", "tool_call", "tool_result", "final_response"],
  });
  assert.equal(data.thread_id, owner.threadId);
  assert.equal(data.has_more, false);
  assert(data.items.every(turn => turn.session_id === owner.sessionId
    && turn.source_view_id === data.source_view_id && turn.source_view_revision === data.source_view_revision));
  return data;
}

async function send(owner, thread, index, responses) {
  facts.modelCalls.push(...await testControl({ action: "plan_model", session_id: owner.sessionId,
    thread_id: owner.threadId, interactions: responses }));
  const responsePromise = page.waitForResponse(response => response.request().method() === "POST"
    && new URL(response.url()).pathname === `/api/v1/sessions/${owner.sessionId}/threads/${owner.threadId}/messages`
    && response.request().headers()["x-boxteam-workspace-id"] === owner.routeId);
  await thread.locator(webSelectors.composer).fill(prompts[index]);
  await thread.locator(webSelectors.sendButton).click();
  const response = await responsePromise;
  const accepted = await response.json();
  assert.equal(response.status(), 200, JSON.stringify(accepted));
  assert.equal(response.headers()["x-request-id"], accepted.request_id);
  assert.equal(accepted.data.thread_id, owner.threadId);
  assert.equal(response.request().postDataJSON().message.content, prompts[index]);
  const turn = thread.locator(`.chat-turn[data-turn-id="${accepted.data.turn_id}"]`);
  await expect(turn.getByText(replies[index], { exact: true })).toBeVisible({ timeout: 30_000 });
  await expect.poll(async () => (await api(`/api/v1/jobs/${accepted.data.job_id}`, owner)).status,
    { timeout: 30_000 }).toBe("completed");
  return accepted.data.turn_id;
}

async function openChild(owner) {
  const main = await selectThread(page, facts.source, 0);
  const toggle = main.getByRole("button", { name: "切换会话资源", exact: true });
  if (await toggle.getAttribute("aria-expanded") !== "true") await toggle.click();
  await main.getByRole("tab", { name: "运行与连接", exact: true }).click();
  await main.locator(`.child-thread-item[data-thread-id="${owner.threadId}"] .child-thread-main`).click();
  const child = page.locator(`section.boxteam-thread[data-workspace-id="${owner.workspaceId}"][data-session-id="${owner.sessionId}"][data-thread-id="${owner.threadId}"]`);
  await expect(child.locator(webSelectors.composer)).toBeVisible();
  return child;
}

async function assertPrefixVisible(thread, turnId) {
  await expect(thread.locator(`.chat-turn[data-turn-id="${turnId}"]`).getByText(replies[0], { exact: true })).toBeVisible();
  await expect(thread.locator(`.chat-turn[data-turn-id="${facts.sourceTurnIds[1]}"]`)).toHaveCount(0);
  await expect(thread.getByText(replies[1], { exact: true })).toHaveCount(0);
}

try {
  const browser = await chromium.launch({ executablePath: requiredEnvironment("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH"), headless: true });
  resources.addCleanup("前缀分支 Chromium", () => browser.close());
  const context = await browser.newContext({ viewport: { width: 1600, height: 1100 } });
  resources.addCleanup("前缀分支 context", () => context.close());
  resources.addCleanup("fork 监督器", () => closeForkDeadlineWatchdog());
  await context.tracing.start({ screenshots: true, snapshots: false, sources: false });
  resources.addCleanup("前缀分支 trace", () => context.tracing.stop({ path: join(artifacts, "browser-trace.zip") }));
  page = await context.newPage();
  page.on("pageerror", error => facts.pageErrors.push(String(error)));
  await openWorkbench(page, baseUrl);
  const credential = await context.request.get(new URL("/api/gateway/auth/local-credential", baseUrl).href);
  assert.equal(credential.status(), 200);
  token = (await credential.json()).data.token;
  await credential.dispose();
  const registry = await api("/api/gateway/workspaces");
  const routeId = registry.active_workspace_id;
  assert.equal(typeof routeId, "string");
  await api("/api/gateway/health");
  const workspace = await api("/api/v1/workspace", { routeId });
  const created = await api("/api/v1/sessions", { routeId }, { title: "较早工具回答的前缀分支", agent_id: "default" });
  const updated = await api(`/api/v1/sessions/${created.session_id}`, { routeId }, { provider_id: "primary" }, "PATCH");
  assert.equal(updated.current_provider_id, "primary");
  const main = await api(`/api/v1/sessions/${created.session_id}/main-thread`, { routeId });
  const source = { routeId, workspaceId: created.workspace_id, sessionId: created.session_id, threadId: main.thread_id };
  assert.equal(workspace.workspace_id, source.workspaceId);
  facts.source = source;
  facts.phase = "source-product-turns";
  await page.reload({ waitUntil: "domcontentloaded" });
  const thread = await selectThread(page, source, 0);
  for (let index = 0; index < 2; index += 1) {
    facts.sourceTurnIds.push(await send(source, thread, index, interactions.slice(index * 2, index * 2 + 2)));
  }
  const before = await history(source);
  assert.deepEqual(before.items.map(turn => turn.turn_id), facts.sourceTurnIds);
  const earlier = before.items[0];
  const finalPart = earlier.response_parts.find(part => part.kind === "final_text" && part.final);
  assert(finalPart?.source.item_id, "较早回答必须有来自真实执行的稳定 canonical item");
  facts.sourceHistory = before;
  facts.phase = "earlier-prefix-fork";
  const query = new URLSearchParams({ thread_id: source.threadId, item_id: finalPart.source.item_id,
    source_view_id: earlier.source_view_id, source_view_revision: String(earlier.source_view_revision), mode: "view_prefix_fork" });
  const point = await api(`/api/v1/sessions/${source.sessionId}/fork-source-point?${query}`, source);
  assert.equal(point.anchor_item_id, finalPart.source.item_id);
  const request = { idempotency_key: "earlier-answer-prefix-regression", source_uri: point.source_uri,
    source_view_id: point.source_view_id, source_view_revision: point.source_view_revision,
    anchor_item_id: point.anchor_item_id, anchor_mode: "inclusive", mode: "view_prefix_fork",
    creation_kind: "new_child_thread", target_container_uri: point.child_container_uri };
  facts.forkRequest = request;
  const deadline = await beginForkOperationDeadline({ target: `${source.routeId}/${source.sessionId}`, operationKey: request.idempotency_key });
  let fork;
  try {
    fork = await waitForForkOperation(api(`/api/v1/sessions/${source.sessionId}/fork-context`, source, request), deadline, "较早回答前缀发布");
    assert.equal(fork.anchor_item_id, finalPart.source.item_id);
    assert.deepEqual([fork.mode, fork.creation_kind, fork.source_view_id, fork.source_view_revision],
      [request.mode, request.creation_kind, request.source_view_id, request.source_view_revision]);
    assert.equal(fork.session.session_id, source.sessionId);
    assert.notEqual(fork.thread_id, source.threadId);
  } finally { await deadline.complete(); }
  facts.fork = fork;
  const child = { ...source, threadId: fork.thread_id };
  facts.child = child;
  const imported = await history(child);
  assert.deepEqual(imported.items.map(turn => turn.turn_id), [earlier.turn_id]);
  assert.deepEqual(imported.items[0].response_parts, earlier.response_parts);
  facts.importedHistory = imported;
  await assertPrefixVisible(await openChild(child), earlier.turn_id);
  assert.deepEqual(await history(source), before, "分支发布与页面打开不能改写 source 历史");
  facts.phase = "refresh-prefix";
  // 真实刷新，不预写展示态或调用关闭 flush，也不以手工 checkpoint 帮助产品恢复。
  await page.reload({ waitUntil: "domcontentloaded" });
  const refreshedThread = await openChild(child);
  await assertPrefixVisible(refreshedThread, earlier.turn_id);
  facts.refreshedHistory = await history(child);
  assert.deepEqual(facts.refreshedHistory, imported);
  facts.phase = "backend-hard-exit";
  // health 沿用平铺响应；普通业务 API 继续严格核对 APIResponse 的 request_id。
  const beforeCrash = (await responseBody("/api/v1/health", source)).body;
  const registered = (await api("/api/gateway/workspaces")).items.find(item => item.workspace_id === source.routeId);
  assert(registered?.managed);
  assert.equal(registered.backend_workspace_id, source.workspaceId);
  assert.equal(typeof registered.backend_generation, "string");
  // 离开真实页面关闭 SSE；首次正常关闭之前强杀，不让 shutdown 帮产品落盘。
  await page.goto("about:blank");
  // 原生强杀只作用于本次 Gateway 的测试 Backend；不调用 shutdown 帮产品保存恢复状态。
  const gatewayIdentity = JSON.parse(requiredEnvironment("BOXTEAM_PREFIX_GATEWAY"));
  const killed = await promisify(execFile)(requiredEnvironment("BOXTEAM_PREFIX_PYTHON"), [
    "-m", "tests.integration.clients.web.itemized_context.test_fork_prefix_recovery",
    String(beforeCrash.process_id), String(gatewayIdentity.pid), String(gatewayIdentity.created_at),
  ], { timeout: 10_000, maxBuffer: 64 * 1024 });
  assert.equal(killed.stderr, "");
  const termination = JSON.parse(killed.stdout);
  assert.equal(termination.pid, beforeCrash.process_id);
  assert.equal(termination.alive, false);
  facts.backendCrash = { before: beforeCrash, termination, previousGeneration: registered.backend_generation };
  // 崩溃后的完整组恢复仍由公开 Gateway start 执行，保留原 generation 和成员前置条件。
  const started = await api(`/api/gateway/workspaces/${source.routeId}/runtime/start`, undefined, {
    expected_backend_generation: registered.backend_generation,
    expected_workspace_ids: registered.affected_workspace_ids,
  });
  assert.equal(started.status, "started");
  assert.deepEqual(started.blockers, []);
  assert.deepEqual(started.affected_workspace_ids, registered.affected_workspace_ids);
  assert.notEqual(started.backend_generation, registered.backend_generation);
  const afterCrash = (await responseBody("/api/v1/health", source)).body;
  assert.equal(beforeCrash.workspace_id, source.workspaceId);
  assert.equal(afterCrash.workspace_id, source.workspaceId);
  assert.equal(afterCrash.backend_generation, started.backend_generation);
  assert.notEqual(afterCrash.process_id, beforeCrash.process_id);
  Object.assign(facts.backendCrash, { receipt: started, after: afterCrash });
  facts.hardRestartedHistory = await history(child);
  assert.deepEqual(facts.hardRestartedHistory, imported);
  assert.deepEqual(await history(source), before, "Backend 硬退出恢复不能改写 source 历史");
  facts.phase = "backend-restart";
  // 再保留原安全重启回归，完整组前置条件不能因硬退出补验而被删去。
  const restart = await api(`/api/gateway/workspaces/${source.routeId}/runtime/restart-safe`, undefined, {
    expected_backend_generation: started.backend_generation,
    expected_workspace_ids: started.affected_workspace_ids,
  });
  assert.equal(restart.status, "restarted");
  assert.deepEqual(restart.blockers, []);
  assert.notEqual(restart.backend_generation, started.backend_generation);
  const afterRestart = (await responseBody("/api/v1/health", source)).body;
  assert.equal(afterRestart.workspace_id, source.workspaceId);
  assert.equal(afterRestart.backend_generation, restart.backend_generation);
  assert.notEqual(afterCrash.process_id, afterRestart.process_id, "必须真正更换 Backend 进程");
  facts.backendRestart = { before: afterCrash, after: afterRestart, receipt: restart,
    previousGeneration: started.backend_generation };
  // 新 Backend 冷读必须由原产品 owner 完成，不能借页面缓存或手工恢复数据通过。
  facts.restartedHistory = await history(child);
  assert.deepEqual(facts.restartedHistory, imported);
  assert.deepEqual(await history(source), before, "Backend 重启不能改写 source 历史");
  await openWorkbench(page, baseUrl);
  const restartedThread = await openChild(child);
  await assertPrefixVisible(restartedThread, earlier.turn_id);
  facts.phase = "child-successor";
  facts.successorTurnId = await send(child, restartedThread, 2, interactions.slice(4));
  const continued = await history(child);
  assert.deepEqual(continued.items.map(turn => turn.turn_id), [earlier.turn_id, facts.successorTurnId]);
  assert.deepEqual(await history(source), before, "child 续聊不能修改 source 历史");
  assert.deepEqual(facts.pageErrors, []);
  facts.phase = "complete";
} catch (error) {
  executionError = error;
  facts.error = String(error);
  if (page) {
    try { await page.screenshot({ path: join(artifacts, "fork-prefix-failure.png"), fullPage: true }); }
    catch (diagnosticError) { executionError = new AggregateError([error, diagnosticError], "前缀分支执行及截图均失败"); }
  }
} finally {
  const failures = executionError ? [executionError] : [];
  try { await resources.close(); } catch (error) { failures.push(error); }
  try { await writeFile(join(artifacts, "fork-prefix-result.json"), JSON.stringify(facts, null, 2)); }
  catch (error) { failures.push(error); }
  if (failures.length) throw new AggregateError(failures, "前缀分支验收失败，详见原始回执");
}
