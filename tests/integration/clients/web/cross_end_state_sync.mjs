import assert from "node:assert/strict";
import { writeFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";
import { expect } from "playwright/test";
import { TestRunContext } from "../../../harness/js/run-context.mjs";
import { Perf } from "../../../harness/js/performance.mjs";
import { observePage } from "../../../harness/js/browser-performance.mjs";
import { testControl } from "../../../support/e2e_control/browser_client.mjs";

const required = name => { assert(process.env[name], `缺少 ${name}`); return process.env[name]; };
const base = required("BOXTEAM_SYNC_BASE_URL");
const direct = required("BOXTEAM_SYNC_TARGET_URL");
const artifacts = required("BOXTEAM_SYNC_ARTIFACTS");
const owner = { gatewayId: required("BOXTEAM_SYNC_TARGET_GATEWAY"), workspaceId: required("BOXTEAM_SYNC_WORKSPACE") };
const headers = { "X-BoxTeam-Target-Gateway-Id": owner.gatewayId, "X-BoxTeam-Workspace-Id": owner.workspaceId, "Content-Type": "application/json" };
const context = await TestRunContext.fromTestFile(fileURLToPath(import.meta.url)).prepare();
const perf = new Perf(context, { mode: "core" });
const scope = required("BOXTEAM_SYNC_SCOPE");
const result = { status: "running", phase: "launch", owner, scope,
  fault: scope === "messages" ? "真实 POST 已提交后丢弃响应，并暂时阻断原身份 GET；不伪造业务响应" : "第二观察者真实断网后重连；不伪造业务响应" };
const errors = [];
const pages = [];

async function timed(action) {
  const start = performance.now();
  const value = await action();
  return { value, ms: performance.now() - start };
}

async function api(page, pathname, init = {}, expectedStatus = 200) {
  const value = await page.evaluate(async ({ pathname, init, ownerHeaders }) => {
    const start = performance.now();
    const credential = await fetch("/api/gateway/auth/local-credential");
    if (!credential.ok) throw new Error(`本机认证 ${credential.status}`);
    const token = (await credential.json()).data.token;
    const response = await fetch(pathname, { ...init, headers: { ...ownerHeaders, ...init.headers, "X-Local-Token": token } });
    const text = await response.text();
    return { status: response.status, body: JSON.parse(text), requestId: response.headers.get("x-request-id"), bytes: new TextEncoder().encode(text).length, ms: performance.now() - start };
  }, { pathname, init, ownerHeaders: headers });
  assert.equal(value.status, expectedStatus, JSON.stringify(value.body));
  assert(value.requestId);
  if (value.body.request_id) assert.equal(value.body.request_id, value.requestId);
  return value;
}

async function open(page, url) {
  await page.goto(url, { waitUntil: "domcontentloaded" });
  await expect(page.locator("#workbench .monaco-workbench")).toBeVisible({ timeout: 30_000 });
  for (const endpoint of ["/api/gateway/health", "/api/gateway/workspaces", "/api/v1/workspace"]) await api(page, endpoint);
}

async function thread(page) {
  await page.waitForFunction(() => typeof globalThis.workbench?.commands?.executeCommand === "function", undefined, { timeout: 30_000 });
  await expect(page.getByTestId(`workspace-node-${JSON.stringify([owner.gatewayId, owner.workspaceId])}`)).toBeVisible({ timeout: 30_000 });
  await page.evaluate(resource => globalThis.workbench.commands.executeCommand("_workbench.open", resource),
    `boxteam://${owner.gatewayId}/workspace/${owner.workspaceId}/resources/session/${owner.sessionId}/threads/${owner.threadId}`);
  const pane = page.locator(`section.boxteam-thread[data-workspace-id="${owner.workspaceId}"][data-session-id="${owner.sessionId}"][data-thread-id="${owner.threadId}"]`);
  await expect(pane).toBeVisible();
  await expect(pane.getByTestId("composer-input")).toBeEditable();
  return pane;
}

async function slash(pane, command) {
  const input = pane.getByTestId("composer-input");
  await input.fill(command);
  await input.press("Enter");
  await expect(input).toHaveValue("");
}

async function showTasks(pane) {
  const trigger = pane.locator('button[data-status-section="tasks"]');
  await expect(trigger).toBeVisible({ timeout: 35_000 });
  if (await trigger.getAttribute("aria-expanded") === "false") await trigger.click();
  return pane.locator('.composer-task-list [role="listitem"]');
}

async function navigation(page, kind, fields) {
  const snapshot = (await api(page, "/api/v1/session-catalog/snapshot")).body.data;
  const intent = { client_operation_id: `op_${crypto.randomUUID().replaceAll("-", "")}`, client_sequence: 1, kind,
    base_catalog_revision: snapshot.catalog_revision, ...fields };
  if (fields.target_node_id) {
    const nodes = (await api(page, `/api/v1/session-catalog/export?node_id=${fields.target_node_id}`)).body.data.items;
    intent.expected_revision = nodes.find(node => node.node_id === fields.target_node_id).node_revision;
  }
  await api(page, "/api/v1/session-catalog/operations:enqueue", { method: "POST", body: JSON.stringify({ intents: [intent] }) }, 202);
  let receipt;
  await expect.poll(async () => {
    receipt = (await api(page, `/api/v1/session-catalog/operations?operation_id=${intent.client_operation_id}`)).body.data.items[0];
    return receipt?.state;
  }, { timeout: 30_000 }).toBe("committed");
  return receipt;
}

let failure;
try {
  const browser = await chromium.launch({ executablePath: required("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH"), headless: true });
  context.addCleanup("同步验收 Chromium", () => browser.close());
  for (let index = 0; index < 2; index++) {
    const profile = await browser.newContext({ viewport: { width: 1600, height: 1000 } });
    context.addCleanup(`独立观察者 ${index}`, () => profile.close());
    const page = await profile.newPage();
    pages.push(page);
    page.on("pageerror", error => errors.push(String(error)));
    const observer = await observePage(page, perf, profile);
    if (observer.close) context.addCleanup(`页面观测 ${index}`, () => observer.close());
  }
  result.phase = "target-initialization";
  result.initialization = (await timed(() => Promise.all([open(pages[0], base), open(pages[1], direct)]))).ms;
  const created = (await api(pages[0], "/api/v1/sessions", { method: "POST", body: JSON.stringify({ title: "跨端同步验收", agent_id: "default" }) })).body.data;
  owner.sessionId = created.session_id;
  owner.threadId = (await api(pages[0], `/api/v1/sessions/${owner.sessionId}/main-thread`)).body.data.thread_id;
  const opened = await timed(() => Promise.all(pages.map(thread)));
  const [first, second] = opened.value;
  result.sessionReadyMs = opened.ms;

  if (scope === "goal_catalog") {
    result.phase = "goal-sync";
    // 暂停 Goal 经产品入口创建，后续真实编辑不会启动不属于本用例的自主执行。
    const goalPath = `/api/v1/sessions/${owner.sessionId}/goal`;
    const goalReadPath = `/api/v1/persistent-tasks?session_id=${encodeURIComponent(owner.sessionId)}`;
    const readGoal = async page => (await api(page, goalReadPath)).body.data.session_goal;
    const empty = await readGoal(pages[0]);
    await api(pages[0], goalPath, { method: "PUT", body: JSON.stringify({
      operation_id: `op_${crypto.randomUUID().replaceAll("-", "")}`, expected_control_revision: empty.control_revision,
      objective: "待编辑的暂停目标", status: "paused",
    }) });
    const firstGoal = await showTasks(first);
    const secondGoal = await showTasks(second);
    await expect(firstGoal).toContainText("待编辑的暂停目标", { timeout: 35_000 });
    await slash(first, "/goal edit");
    const editor = pages[0].getByRole("dialog", { name: "编辑 Goal", exact: true });
    await editor.getByRole("textbox").fill("跨 Gateway 同步目标");
    await editor.getByRole("button", { name: "保存 Goal", exact: true }).click();
    await expect(editor).toHaveCount(0);
    for (const row of [firstGoal, secondGoal]) await expect(row).toContainText("跨 Gateway 同步目标", { timeout: 35_000 });
    await expect(secondGoal).toContainText("暂停", { timeout: 35_000 });
    const before = (await readGoal(pages[0]));
    const operation = { operation_id: `op_${crypto.randomUUID().replaceAll("-", "")}`, expected_control_revision: before.control_revision, objective: "CAS 已应用目标", status: "paused" };
    const applied = (await api(pages[0], goalPath, { method: "PUT", body: JSON.stringify(operation) })).body.data;
    await api(pages[1], goalPath, { method: "PUT", body: JSON.stringify({ ...operation, operation_id: `op_${crypto.randomUUID().replaceAll("-", "")}`, objective: "不得覆盖的旧版本" }) }, 409);
    const retried = (await api(pages[1], goalPath, { method: "PUT", body: JSON.stringify(operation) })).body.data;
    assert.equal(retried.control_revision, applied.control_revision);
    for (const row of [firstGoal, secondGoal]) await expect(row).toContainText("CAS 已应用目标", { timeout: 35_000 });
    await slash(first, "/goal clear");
    for (const row of [firstGoal, secondGoal]) await expect(row).toHaveCount(0, { timeout: 35_000 });
    result.goal = { original: before.control_revision, applied: applied.control_revision, final: (await readGoal(pages[1])) };

    result.phase = "observer-reconnect";
    await pages[1].context().setOffline(true);
    const disconnected = await pages[1].evaluate(async () => {
      try { await fetch("/api/gateway/health"); return false; }
      catch { return true; }
    });
    assert(disconnected, "第二观察者必须确实断网");
    const disconnectedVersion = (await readGoal(pages[0])).control_revision;
    await api(pages[0], goalPath, { method: "PUT", body: JSON.stringify({
      operation_id: `op_${crypto.randomUUID().replaceAll("-", "")}`, expected_control_revision: disconnectedVersion,
      objective: "断网期间提交的目标", status: "paused",
    }) });
    await pages[1].context().setOffline(false);
    await expect(await showTasks(second)).toContainText("断网期间提交的目标", { timeout: 35_000 });
    await slash(first, "/goal clear");
    for (const row of [firstGoal, secondGoal]) await expect(row).toHaveCount(0, { timeout: 35_000 });
    result.reconnect = { actualNetworkFailure: disconnected, converged: true };

    result.phase = "catalog-mutations";
    const rename = await navigation(pages[0], "rename_node", { target_node_id: owner.sessionId, name: "两端确认后的名称" });
    const nodeId = `catalog-node-${JSON.stringify([owner.gatewayId, owner.workspaceId])}-${owner.sessionId}`;
    for (const page of pages) await expect(page.getByTestId(nodeId)).toContainText("两端确认后的名称", { timeout: 35_000 });
    const folder = await navigation(pages[0], "create_folder", { name: "同步目标目录", parent_node_id: null });
    const folderId = folder.created_node_id;
    assert(folderId, JSON.stringify(folder));
    const moved = await navigation(pages[0], "move_node", { target_node_id: owner.sessionId, parent_node_id: folderId });
    result.catalog = { rename, folder, moved };
    for (const page of pages) {
      const pageResult = await api(page, `/api/v1/session-catalog/children?parent_node_id=${folderId}&minimum_catalog_revision=${moved.committed_catalog_revision}`);
      assert(pageResult.body.data.catalog_revision >= moved.committed_catalog_revision);
      assert(pageResult.body.data.items.some(item => item.node_id === owner.sessionId));
      const folderNode = page.getByTestId(`catalog-node-${JSON.stringify([owner.gatewayId, owner.workspaceId])}-${folderId}`);
      await expect(folderNode).toBeVisible({ timeout: 35_000 });
      // 当前会话由产品目录定位链自动展开新祖先；此时再点 chevron 会把它折叠。
      await expect(page.getByTestId(nodeId)).toContainText("两端确认后的名称", { timeout: 35_000 });
    }

    result.phase = "warm-measurements";
    result.samples = {};
    for (const endpoint of [goalReadPath, `/api/v1/sessions/${owner.sessionId}/pending-requests?thread_id=${owner.threadId}`, `/api/v1/session-catalog/children?parent_node_id=${folderId}`]) {
      const samples = [];
      for (let index = 0; index < 21; index++) samples.push(await perf.stage("sync.warm-api", { path: endpoint, sample: index }, () => api(pages[0], endpoint)));
      result.samples[endpoint] = { first: samples[0], warm: samples.slice(1) };
    }
    result.scale = {
      workspaces: (await api(pages[0], "/api/gateway/workspaces")).body.data.items.length,
      catalogNodes: (await api(pages[0], "/api/v1/session-catalog/export")).body.data.items.length,
      scope: "单Session、单目录、空Goal／pending队列；不代表深历史或大型目录",
    };
    result.navigation = [];
    for (let index = 0; index < 21; index++) {
      const measured = await perf.stage("sync.navigation", { sample: index }, () => timed(async () => {
        await perf.stage("sync.navigation.close", {}, async () => {
          await pages[0].evaluate(() => globalThis.workbench.commands.executeCommand("workbench.action.closeActiveEditor"));
          await expect(first).toHaveCount(0);
        });
        const responses = [];
        for (const endpoint of ["/api/gateway/workspaces", "/api/v1/workspace", `/api/v1/session-catalog/children?parent_node_id=${folderId}`]) {
          responses.push(await perf.stage("sync.navigation.api", { path: endpoint }, () => api(pages[0], endpoint)));
        }
        await perf.stage("sync.navigation.locate", {}, () => expect(pages[0].getByTestId(nodeId)).toBeVisible());
        await perf.stage("sync.navigation.click", {}, () => pages[0].getByTestId(nodeId).getByTitle("两端确认后的名称", { exact: true }).click());
        await perf.stage("sync.navigation.editable", {}, () => expect(first.getByTestId("composer-input")).toBeEditable());
        return responses.map(({ ms, bytes, requestId }) => ({ ms, bytes, requestId }));
      }));
      result.navigation.push({ ms: measured.ms, hops: measured.value });
    }
  }
  if (scope === "messages") {
    result.phase = "message-response-loss";
    await testControl({ action: "plan_model", session_id: owner.sessionId, thread_id: owner.threadId,
      interactions: JSON.parse(required("BOXTEAM_SYNC_MODEL_INTERACTIONS")) });
    const messagePath = `/api/v1/sessions/${owner.sessionId}/threads/${owner.threadId}/messages`;
    let acceptance;
    let posts = 0;
    let blockQuery = true;
    const fault = async route => {
      const request = route.request();
      const pathname = new URL(request.url()).pathname;
      if (pathname === messagePath && request.method() === "POST") {
        posts++;
        const actual = await route.fetch();
        assert.equal(actual.status(), 200, await actual.text());
        acceptance = (await actual.json()).data;
        await route.abort("failed");
      } else if (blockQuery && pathname.includes(`/threads/${owner.threadId}/message-submissions/`)) await route.abort("failed");
      else await route.continue();
    };
    await pages[0].route("**/api/v1/**", fault);
    await first.getByTestId("composer-input").fill("响应丢失仍使用原操作身份 A");
    await first.getByTestId("composer-input").press("Enter");
    await first.getByTestId("composer-input").fill("后来编辑的草稿 B");
    await expect.poll(() => acceptance, { timeout: 30_000 }).toBeTruthy();
    const recovery = first.getByLabel("消息接收对账", { exact: true });
    await expect(recovery).toContainText("响应丢失仍使用原操作身份 A");
    await expect(first.getByTestId("composer-input")).toHaveValue("后来编辑的草稿 B");
    blockQuery = false;
    await pages[0].reload({ waitUntil: "domcontentloaded" });
    const restored = await thread(pages[0]);
    await expect(restored.getByLabel("消息接收对账", { exact: true })).toHaveCount(0, { timeout: 35_000 });
    await expect(restored.getByTestId("composer-input")).toHaveValue("后来编辑的草稿 B");
    await expect(restored).toContainText("原身份消息只执行一次。", { timeout: 35_000 });
    await expect(second).toContainText("原身份消息只执行一次。", { timeout: 35_000 });
    const recovered = (await api(pages[1], `${messagePath.replace(/\/messages$/, "")}/message-submissions/${acceptance.operation_id}`)).body.data.accepted;
    assert.equal(recovered.job_id, acceptance.job_id);
    assert.equal(posts, 1);
    await pages[0].unroute("**/api/v1/**", fault);
    result.message = { acceptance, recovered, posts, draftBPreserved: true };
  }
  assert.deepEqual(errors, []);
  result.status = "passed";
} catch (error) {
  failure = error;
  result.status = "failed";
  result.error = error.stack ?? String(error);
  for (let index = 0; index < pages.length; index++) {
    try { await pages[index].screenshot({ path: path.join(artifacts, `state-sync-${index}.png`), fullPage: true }); }
    catch (captureError) { console.error("保存失败截图:", captureError); }
  }
} finally {
  result.pageErrors = errors;
  try { await writeFile(path.join(artifacts, "state-sync.json"), JSON.stringify(result, null, 2) + "\n"); }
  finally { await context.close(); }
}
if (failure) throw failure;
