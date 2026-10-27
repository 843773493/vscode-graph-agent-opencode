import assert from "node:assert/strict";
import { writeFile } from "node:fs/promises";
import path from "node:path";
import { chromium } from "playwright";
import { testControl } from "../../../../support/e2e_control/browser_client.mjs";

const fixture = JSON.parse(process.env.BOXTEAM_BROWSER_FIXTURE);
const artifacts = process.env.BOXTEAM_BROWSER_ARTIFACTS;
assert(artifacts, "必须指定浏览器产物目录");
const browser = await chromium.launch({ executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH, headless: true });
let context;
let page;
let barrier;
try {
  context = await browser.newContext({ viewport: { width: 1600, height: 1050 } });
  page = await context.newPage();
  const errors = [];
  page.on("pageerror", (error) => errors.push(String(error)));
  await page.goto(fixture.baseUrl, { waitUntil: "domcontentloaded" });
  await page.getByRole("button", { name: "用户视图", exact: true }).click();
  await page.getByPlaceholder("新用户名称").fill("任务恢复验收用户");
  await page.getByRole("button", { name: "创建", exact: true }).click();
  await page.locator("[data-session-id]").filter({ hasText: fixture.sessionTitle }).first().click();
  const initialization = await page.evaluate(async (workspaceId) => {
    const credential = await fetch("/api/gateway/auth/local-credential");
    if (!credential.ok) throw new Error(`凭据读取失败：${credential.status}`);
    const headers = { "X-Local-Token": (await credential.json()).data.token };
    const receipts = [];
    for (const url of ["/api/gateway/health", "/api/gateway/workspaces", "/api/v1/workspace"]) {
      const response = await fetch(url, { headers });
      if (!response.ok) throw new Error(`初始化失败：${url}/${response.status}`);
      const body = await response.json();
      receipts.push({ url, requestId: response.headers.get("x-request-id"), bodyRequestId: body.request_id });
      if (url === "/api/gateway/workspaces") {
        const owner = body.data.items.find((item) => item.backend_workspace_id === workspaceId);
        if (!owner) throw new Error("Gateway未登记目标");
        headers["X-BoxTeam-Workspace-Id"] = owner.workspace_id;
        headers["X-BoxTeam-Backend-Workspace-Id"] = owner.backend_workspace_id;
      }
    }
    return { headers, receipts };
  }, fixture.workspaceId);
  initialization.receipts.forEach((receipt) => { assert(receipt.requestId); assert.equal(receipt.requestId, receipt.bodyRequestId); });
  const request = async (url, body) => {
    const response = await context.request.fetch(fixture.baseUrl + url, {
      method: body ? "POST" : "GET", headers: initialization.headers, ...(body ? { data: body } : {}),
    });
    assert.equal(response.status(), 200, await response.text());
    return response.json();
  };
  const root = "/api/v1/persistent-tasks";
  const initial = (await request(root)).data;
  assert.equal(initial.runtime.state, "running");
  const task = initial.items.find((item) => item.configuration?.name === "恢复期间暂停的任务");
  assert(task);
  const toggle = page.getByRole("button", { name: "切换底部面板", exact: true });
  if (await toggle.getAttribute("aria-pressed") !== "true") await toggle.click();
  await page.getByRole("tab", { name: "自动化", exact: true }).click();
  const panel = page.getByRole("region", { name: "工作区持久任务", exact: true });
  await panel.getByRole("button", { name: /恢复期间暂停的任务/ }).click();
  await request("/api/v1/_test/task-runtime/stop", {});
  await panel.getByRole("status").filter({ hasText: "运行时已停止" }).waitFor();
  barrier = "web-task-recovery";
  await testControl({ action: "arm_match", barrier_id: barrier, phase: "task.recovery.started", details: { task_id: task.task_id } });
  await request("/api/v1/_test/task-runtime/start", {});
  await testControl({ action: "acknowledged_match", barrier_id: barrier });
  await panel.getByRole("status").filter({ hasText: "工作区已就绪；1 个任务仍在后台恢复" }).waitFor();
  const queued = (await request(`${root}/${task.task_id}/checks`, { expected_revision: task.revision, idempotency_key: "during-recovery", check_only: false })).data;
  const pauseReceipt = page.waitForResponse((response) => new URL(response.url()).pathname.endsWith(`/${task.task_id}/control`));
  await panel.getByRole("button", { name: "暂停任务", exact: true }).click();
  const pauseResponse = await pauseReceipt;
  assert.equal(pauseResponse.status(), 200, await pauseResponse.text());
  await panel.getByRole("button", { name: "恢复任务", exact: true }).waitFor();
  const paused = (await request(`${root}/${task.task_id}`)).data;
  assert.equal(paused.lifecycle, "paused");
  assert.equal(paused.recovering, true);
  await page.screenshot({ path: path.join(artifacts, "recovering-paused.png") });
  await testControl({ action: "release_match", barrier_id: barrier });
  barrier = undefined;
  await panel.getByRole("status").filter({ hasText: "后台恢复已完成" }).waitFor();
  const final = (await request(`${root}/${task.task_id}`)).data;
  assert.equal(final.lifecycle, "paused");
  assert.equal(final.revision, paused.revision);
  assert.equal(final.recovering, false);
  const runs = (await request(`${root}/${task.task_id}/runs`)).data.items;
  const suppressed = runs.find((run) => run.run_id === queued.run_id);
  assert.equal(suppressed.phase, "suppressed");
  assert.equal(suppressed.delivery, null);
  const sidebar = page.getByRole("button", { name: "切换右侧侧边栏", exact: true });
  if (await sidebar.getAttribute("aria-pressed") !== "true") await sidebar.click();
  await page.getByRole("tab", { name: "Goal", exact: true }).click();
  await request("/api/v1/_test/task-runtime/cancel", {});
  await panel.getByRole("alert").filter({ hasText: "持久任务后台运行失败（RuntimeError）" }).waitFor();
  const summary = page.getByRole("region", { name: "当前会话关联任务", exact: true });
  await summary.getByRole("alert").filter({ hasText: "持久任务后台运行失败" }).waitFor();
  await panel.getByRole("button", { name: "刷新任务", exact: true }).click();
  await panel.getByRole("alert").filter({ hasText: "持久任务后台运行失败" }).waitFor();
  assert.equal(await panel.getByText("持久任务运行时正常，后台恢复已完成。", { exact: true }).count(), 0);
  const failure = (await request(root)).data;
  assert.equal(failure.runtime.state, "failed");
  const health = await context.request.get(fixture.baseUrl + "/api/v1/health", { headers: initialization.headers });
  assert.equal(health.status(), 500);
  await page.screenshot({ path: path.join(artifacts, "worker-failed.png") });
  assert.deepEqual(errors, []);
  await writeFile(path.join(artifacts, "actual.json"), JSON.stringify({ initialization: initialization.receipts, paused, final, queued: suppressed, failure }, null, 2));
} catch (error) {
  if (page) {
    await page.screenshot({ path: path.join(artifacts, "failure.png") });
    await writeFile(path.join(artifacts, "failure.html"), await page.content());
  }
  throw error;
} finally {
  if (barrier) await testControl({ action: "release_match", barrier_id: barrier });
  try { if (context) await context.close(); }
  finally { await browser.close(); }
}
