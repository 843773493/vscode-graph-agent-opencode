import assert from "node:assert/strict";
import { createHash, randomUUID } from "node:crypto";
import { readFile, unlink, writeFile } from "node:fs/promises";
import { join } from "node:path";
import { chromium } from "playwright";
import { expect } from "playwright/test";
import { TestRunContext } from "../../../../harness/js/run-context.mjs";
import { webSelectors } from "../../../../clients/selectors/web-selectors.mjs";
import { testControl } from "../../../../support/e2e_control/browser_client.mjs";
import { openWorkbench, selectThread } from "../persistent_tasks/workbench.mjs";

function requiredEnvironment(name) {
  const value = process.env[name];
  assert(value, `缺少环境变量 ${name}`);
  return value;
}

const baseUrl = requiredEnvironment("BOXTEAM_BROWSER_BASE_URL");
const artifacts = requiredEnvironment("BOXTEAM_BROWSER_ARTIFACT_ROOT");
const owner = JSON.parse(requiredEnvironment("BOXTEAM_BROWSER_OWNER"));
const resource = JSON.parse(requiredEnvironment("BOXTEAM_BROWSER_CONTEXT_RESOURCE"));
const agentsFile = requiredEnvironment("BOXTEAM_BROWSER_AGENTS_FILE");
const versions = JSON.parse(requiredEnvironment("BOXTEAM_BROWSER_AGENTS_VERSIONS"));
const boundary = requiredEnvironment("BOXTEAM_BROWSER_AGENTS_BOUNDARY");
assert(["turn", "model_call"].includes(boundary));
const firstReply = requiredEnvironment("BOXTEAM_BROWSER_FIRST_REPLY");
const mixedSkill = process.env.BOXTEAM_BROWSER_MIXED_SKILL ? JSON.parse(process.env.BOXTEAM_BROWSER_MIXED_SKILL) : undefined;
const policyUpdate = process.env.BOXTEAM_BROWSER_POLICY_UPDATE ? JSON.parse(process.env.BOXTEAM_BROWSER_POLICY_UPDATE) : undefined;
const firstCallCount = mixedSkill ? 3 : 2;
const interactions = JSON.parse(requiredEnvironment("BOXTEAM_BROWSER_MODEL_CASSETTES")).map(cassette => cassette.interactions[0]);
const resources = TestRunContext.fromTestFile(requiredEnvironment("BOXTEAM_BROWSER_TEST_OWNER"));
const facts = { phase: "setup", owner, boundary, modelCalls: [], pageErrors: [], responses: [], browserRequests: [], consoleErrors: [], inspectedAssemblies: [] };
let page;
let token;
let executionError;

function phase(value) {
  facts.phase = value;
  process.stdout.write(`${JSON.stringify({ phase: value, at: new Date().toISOString(), memory: process.memoryUsage() })}\n`);
}

async function api(pathname, data, method = data === undefined ? "GET" : "POST") {
  const requestId = randomUUID();
  const response = await page.request.fetch(new URL(pathname, baseUrl).href, {
    method, headers: { "X-Local-Token": token, "X-Request-ID": requestId, "X-BoxTeam-Target-Gateway-Id": owner.gatewayId, "X-BoxTeam-Workspace-Id": owner.workspaceId },
    ...(data === undefined ? {} : { data }), timeout: 60_000,
  });
  try {
    const body = await response.json();
    assert.equal(response.headers()["x-request-id"], requestId);
    assert.equal(body.request_id, requestId);
    facts.responses.push({ path: pathname, status: response.status(), requestId });
    assert.equal(response.status(), 200, JSON.stringify(body));
    return body.data;
  } finally { await response.dispose(); }
}

async function send(thread, prompt) {
  await thread.locator(webSelectors.composer).fill(prompt);
  const [response] = await Promise.all([
    page.waitForResponse(response => response.request().method() === "POST"
      && new URL(response.url()).pathname === `/api/v1/sessions/${owner.sessionId}/threads/${owner.threadId}/messages`
      && response.request().headers()["x-boxteam-workspace-id"] === owner.workspaceId),
    thread.locator(webSelectors.sendButton).click(),
  ]);
  const body = await response.json();
  assert.equal(response.status(), 200, JSON.stringify(body));
  assert.equal(typeof body.request_id, "string");
  assert(body.request_id.length > 0);
  assert.equal(response.headers()["x-request-id"], body.request_id);
  assert.equal(body.data.thread_id, owner.threadId);
  assert.equal(response.request().postDataJSON().message.content, prompt);
  return body.data;
}

async function completed(thread, accepted, reply) {
  await expect(thread.locator(`.chat-turn[data-turn-id="${accepted.turn_id}"]`).getByText(reply, { exact: true }))
    .toBeVisible({ timeout: 30_000 });
  await expect.poll(async () => (await api(`/api/v1/jobs/${accepted.job_id}`)).status, { timeout: 30_000 }).toBe("completed");
}

async function contextItems(view, assemblyRef) {
  const items = [];
  let cursor;
  let revision;
  let fragment = "";
  for (let count = 0; count < 30; count += 1) {
    const result = await api("/api/v1/context/read", { ...resource, view, ...(assemblyRef ? { assembly_ref: assemblyRef } : {}),
      include: ["visible_text", "reasoning", "tool_calls", "tool_results", "system", "raw_record"],
      limit: 200, max_chars: 65536, ...(cursor ? { cursor, revision } : {}) });
    assert.equal(result.resource_identity, resource.resource_identity);
    assert.equal(result.uri, resource.uri);
    assert.equal(result.scope, resource.scope);
    assert.equal(result.view, view);
    assert.deepEqual(result.partial_errors, []);
    assert.equal(result.omitted_partial_error_count, 0);
    assert.equal(result.truncated, result.has_more);
    assert(result.returned_chars <= 65536);
    if (revision) assert.equal(result.revision, revision);
    revision = result.revision;
    for (const item of result.items) {
      if (item.kind.endsWith("_chunk")) {
        assert.equal(item.data.chunk_start, [...fragment].length);
        assert.equal(typeof item.text, "string");
        fragment += item.text;
        if (!item.truncated) {
          const restored = JSON.parse(fragment);
          assert.equal(restored.kind, item.kind.slice(0, -6));
          for (const key of ["resource_identity", "uri", "scope", "assembly_ref"]) {
            assert.equal(restored[key], item[key]);
          }
          items.push(restored);
          fragment = "";
        }
      } else {
        assert.equal(fragment, "");
        items.push(item);
      }
    }
    if (!result.has_more) {
      assert.equal(fragment, "");
      return items;
    }
    assert(result.next_cursor && result.next_cursor !== cursor);
    cursor = result.next_cursor;
  }
  throw new Error("冻结上下文分页未收敛");
}

async function loadInspectionPages(inspector, buttonName, loadingText) {
  for (let count = 0; count < 100; count += 1) {
    await expect(inspector.getByRole("status").filter({ hasText: loadingText })).toHaveCount(0);
    assert.equal(await inspector.getByRole("alert").count(), 0, await inspector.innerText());
    const next = inspector.getByRole("button", { name: buttonName, exact: true });
    if (!await next.count()) return;
    await next.click();
  }
  throw new Error("冻结上下文页面分页未收敛");
}

async function assemblies() {
  const entries = await contextItems("assemblies");
  const result = [];
  for (const entry of entries) {
    const items = await contextItems("assembly", entry.assembly_ref);
    const header = items.find(item => item.kind === "assembly");
    assert(header);
    result.push({ assemblyId: header.data.assembly_id, assemblyRef: entry.assembly_ref,
      selection: items.filter(item => item.kind === "selection").map(item => item.data), resources: header.resource_refs, items });
  }
  return result;
}

try {
  const browser = await chromium.launch({ executablePath: requiredEnvironment("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH"), headless: true });
  resources.addCleanup("来源冻结 Chromium", () => browser.close());
  const context = await browser.newContext({ viewport: { width: 1600, height: 1100 } });
  resources.addCleanup("来源冻结 context", () => context.close());
  await context.tracing.start({ screenshots: true, snapshots: false, sources: false });
  resources.addCleanup("来源冻结 trace", () => context.tracing.stop({ path: join(artifacts, "browser-trace.zip") }));
  page = await context.newPage();
  page.on("pageerror", error => facts.pageErrors.push(String(error)));
  page.on("console", entry => { if (entry.type() === "error") facts.consoleErrors.push(entry.text()); });
  page.on("request", request => {
    const path = new URL(request.url()).pathname;
    if (path.startsWith("/api/v1/sessions/") && !path.endsWith("/queue")) {
      facts.browserRequests.push({ path, method: request.method(), workspace: request.headers()["x-boxteam-workspace-id"] });
    }
  });
  await openWorkbench(page, baseUrl);
  const credential = await context.request.get(new URL("/api/gateway/auth/local-credential", baseUrl).href);
  try {
    assert.equal(credential.status(), 200);
    token = (await credential.json()).data.token;
  } finally { await credential.dispose(); }
  await api("/api/gateway/health");
  assert.deepEqual((await api("/api/gateway/workspaces")).active_workspace, { gateway_id: owner.gatewayId, workspace_id: owner.workspaceId });
  assert.equal((await api("/api/v1/workspace")).workspace_id, owner.workspaceId);
  // 本场景从初始编辑器恢复结束后开始；不把Welcome的异步打开与目标会话混为一次导航。
  // TODO：product-006中目标Session/main-thread请求均为200，但目标Tab未出现且仍显示Welcome；初始化导航时序原因待前端owner核实。
  const welcome = page.getByRole("tab", { name: "Welcome, preview", exact: true });
  await expect(welcome).toBeVisible();
  await welcome.getByRole("button", { name: "Close (Ctrl+W)", exact: true }).click();
  await expect(welcome).toHaveCount(0);
  let thread = await selectThread(page, owner, 0);
  assert.equal(await readFile(agentsFile, "utf8"), versions[0]);
  phase("tool-loop-source-update");
  const calls = await testControl({ action: "plan_model", session_id: owner.sessionId, thread_id: owner.threadId,
    interactions: interactions.slice(0, firstCallCount) });
  assert.equal(calls.length, firstCallCount);
  facts.modelCalls.push(...calls);
  const barriers = calls.slice(0, mixedSkill ? 2 : 1);
  for (const call of barriers) await testControl({ action: "arm", operation_id: call, phase: "model.response_ready" });
  try {
    facts.firstTurn = await send(thread, mixedSkill ? "两次加载Skill，核对原调用的正文版本。" : "读取 README.md；工具循环按配置的来源边界执行。");
    for (const [index, call] of barriers.entries()) {
      await testControl({ action: "acknowledged", operation_id: call, phase: "model.response_ready" });
      assert.equal((await api(`/api/v1/jobs/${facts.firstTurn.job_id}`)).status, "running");
      // 原watcher发布后才放行工具；不改registry、checkpoint、assembly或执行状态。
      const eventCount = mixedSkill ? (await testControl({ action: "events" })).length : 0;
      await writeFile(agentsFile, versions[index + 1]);
      assert.equal(await readFile(agentsFile, "utf8"), versions[index + 1]);
      if (policyUpdate) {
        const before = await api("/api/v1/config/reload-status");
        const frozen = await assemblies();
        assert.equal(frozen.length, 1);
        await writeFile(policyUpdate.path, policyUpdate.content);
        let after;
        await expect.poll(async () => {
          after = await api("/api/v1/config/reload-status");
          return after.revision !== before.revision || !after.healthy || after.restart_required;
        }, { timeout: 30_000 }).toBe(true);
        assert.equal(after.healthy, true);
        assert.equal(after.restart_required, false);
        assert.notEqual(after.revision, before.revision);
        const changed = "/context/resource_activation/default_boundary";
        const applied = (await api("/api/v1/config/events")).events.filter(event =>
          event.result === "applied" && event.changed_paths.includes(changed));
        assert.equal(applied.length, 1);
        assert.equal(applied[0].activation_scope, "next_job");
        assert.deepEqual(applied[0].changed_paths, [changed]);
        assert.deepEqual(applied[0].applied_paths, [changed]);
        assert.deepEqual(applied[0].deferred_paths, []);
        assert.deepEqual(await assemblies(), frozen);
        facts.policyReload = { before, after, event: applied[0] };
      }
      if (mixedSkill) {
        await writeFile(mixedSkill.path, mixedSkill.versions[index + 1]);
        const revision = `sha256:${createHash("sha256").update(mixedSkill.versions[index + 1]).digest("hex")}`;
        await expect.poll(async () => (await testControl({ action: "events" })).slice(eventCount).some(event =>
          event.phase === "workspace.source.watch.published" && event.operation_id === mixedSkill.uri
          && event.details.revision === revision && event.details.available === true), { timeout: 15_000 }).toBe(true);
        (facts.publications ??= []).push(...(await testControl({ action: "events" })).slice(eventCount).filter(event =>
          event.phase === "workspace.source.watch.published" && event.operation_id === mixedSkill.uri
          && event.details.revision === revision));
      }
      await testControl({ action: "release", operation_id: call, phase: "model.response_ready" });
    }
  } finally {
    for (const call of barriers) await testControl({ action: "release", operation_id: call, phase: "model.response_ready" });
  }
  await completed(thread, facts.firstTurn, firstReply);
  facts.firstAssemblies = await assemblies();
  assert.equal(facts.firstAssemblies.length, firstCallCount);
  phase("next-turn");
  facts.modelCalls.push(...await testControl({ action: "plan_model", session_id: owner.sessionId, thread_id: owner.threadId,
    interactions: interactions.slice(firstCallCount) }));
  facts.secondTurn = await send(thread, mixedSkill ? "新轮加载最新Skill，使用当前AGENTS。" : "下一轮继续，使用已更新的来源。");
  await completed(thread, facts.secondTurn, "下一轮采用新的来源版本。");
  facts.assemblies = await assemblies();
  assert.equal(facts.assemblies.length, interactions.length);
  assert.deepEqual(facts.assemblies.filter(entry => facts.firstAssemblies.some(old => old.assemblyId === entry.assemblyId)), facts.firstAssemblies);
  phase("cold-inspection");
  await writeFile(agentsFile, mixedSkill?.currentAgents ?? versions[2]);
  if (mixedSkill) await unlink(mixedSkill.path);
  await page.goto("about:blank");
  const registered = (await api("/api/gateway/workspaces")).items.find(entry => entry.gateway_id === owner.gatewayId && entry.workspace_id === owner.workspaceId);
  assert(registered?.managed && registered.backend_generation);
  facts.restart = await api(`/api/gateway/workspaces/${owner.workspaceId}/runtime/restart-safe`, {
    expected_backend_generation: registered.backend_generation, expected_workspace_ids: registered.affected_workspace_ids,
  });
  assert.equal(facts.restart.status, "restarted");
  assert.deepEqual(facts.restart.blockers, []);
  assert.notEqual(facts.restart.backend_generation, registered.backend_generation);
  facts.coldAssemblies = await assemblies();
  assert.deepEqual(facts.coldAssemblies, facts.assemblies);
  await openWorkbench(page, baseUrl);
  thread = await selectThread(page, owner, 0);
  await expect(thread.getByText(firstReply, { exact: true })).toBeVisible();
  await expect(thread.getByText("下一轮采用新的来源版本。", { exact: true })).toBeVisible();
  await thread.locator("#viewModeButton").click();
  await thread.getByRole("menuitemradio", { name: /^上下文状态/ }).click();
  await thread.getByRole("button", { name: "冻结请求", exact: true }).click();
  const inspector = thread.getByRole("region", { name: "冻结请求上下文" });
  await expect(inspector).toHaveAttribute("data-owner-session-id", owner.sessionId);
  const choices = inspector.getByLabel("选择冻结 assembly");
  // 首次挂载会异步读取目录，选项到达后才能判断是否还有后续页。
  await expect(choices.locator("option")).not.toHaveCount(1);
  await loadInspectionPages(inspector, "加载更早请求", "正在读取封存请求");
  await expect(choices.locator("option")).toHaveCount(facts.assemblies.length + 1);
  for (const assembly of facts.assemblies) {
    phase(`inspect-${assembly.assemblyId}`);
    await choices.selectOption(assembly.assemblyRef);
    const projection = inspector.locator(`.context-projection[data-assembly-id="${assembly.assemblyId}"]`);
    await expect.poll(async () => await projection.locator(".context-inspection-items > li").count()
      + await projection.getByRole("button", { name: "加载下一页上下文", exact: true }).count()).toBeGreaterThan(0);
    await loadInspectionPages(inspector, "加载下一页上下文", "正在读取冻结上下文");
    const details = projection.locator(".context-selection-entry details");
    await expect(details).toHaveCount(assembly.selection.length);
    for (const detail of await details.all()) await detail.locator("summary").click();
    await expect(projection.locator(".context-selection-entry pre")).toHaveCount(assembly.selection.length);
    assert.deepEqual((await projection.locator(".context-selection-entry pre").allTextContents()).map(value => JSON.parse(value)), assembly.selection);
    const entries = projection.locator('[aria-label="封存资源来源"] > li');
    await expect(entries).toHaveCount(assembly.resources.length);
    for (const [index, expected] of assembly.resources.entries()) {
      await expect(entries.nth(index)).toHaveAttribute("data-provenance-ref", expected.provenance_ref.token);
      await expect(entries.nth(index)).toContainText(expected.revision);
    }
    assert.equal(await inspector.getByRole("alert").count(), 0);
    assert(!(await inspector.innerText()).includes(mixedSkill?.currentAgents.trim() ?? "WEB_TURN_SOURCE_C_CURRENT_IS_NOT_HISTORY"));
    await page.screenshot({ path: join(artifacts, `assembly-${assembly.assemblyId}.png`) });
    facts.inspectedAssemblies.push(assembly.assemblyId);
  }
  assert.deepEqual(facts.inspectedAssemblies, facts.assemblies.map(assembly => assembly.assemblyId));
  assert.deepEqual(facts.pageErrors, []);
  phase("complete");
} catch (error) {
  executionError = error;
  facts.error = String(error);
  if (page) {
    try { await page.screenshot({ path: join(artifacts, "turn-source-failure.png"), fullPage: true }); }
    catch (diagnosticError) { executionError = new AggregateError([error, diagnosticError], "来源冻结执行与截图均失败"); }
  }
} finally {
  const failures = executionError ? [executionError] : [];
  try { await resources.close(); } catch (error) { failures.push(error); }
  try { await writeFile(join(artifacts, "turn-source-result.json"), JSON.stringify(facts, null, 2)); }
  catch (error) { failures.push(error); }
  if (failures.length) throw new AggregateError(failures, "来源冻结验收失败，详见原始回执");
}
