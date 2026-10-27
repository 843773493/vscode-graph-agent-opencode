import assert from "node:assert/strict";
import { writeFile } from "node:fs/promises";
import path from "node:path";
import { chromium } from "playwright";

const fixture = JSON.parse(process.env.BOXTEAM_BROWSER_FIXTURE);
const artifacts = process.env.BOXTEAM_BROWSER_ARTIFACTS;
assert(artifacts, "必须指定浏览器产物目录");
const browser = await chromium.launch({ executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH, headless: true });
let context;
let page;
try {
  context = await browser.newContext({ viewport: { width: 1600, height: 1050 } });
  page = await context.newPage();
  const errors = [];
  page.on("pageerror", (error) => errors.push(String(error)));
  await page.goto(fixture.baseUrl, { waitUntil: "domcontentloaded" });
  // 使用产品用户保存工作区面板偏好，刷新后仍由后端恢复同一入口。
  await page.getByRole("button", { name: "用户视图", exact: true }).click();
  await page.getByPlaceholder("新用户名称").fill("持久任务验收用户");
  await page.getByRole("button", { name: "创建", exact: true }).click();
  await page.locator("[data-session-id]").filter({ hasText: fixture.sessionTitle }).first().click();
  const initialization = await page.evaluate(async (workspaceId) => {
    const credential = await fetch("/api/gateway/auth/local-credential");
    assertResponse(credential);
    const headers = { "X-Local-Token": (await credential.json()).data.token };
    let owner;
    const results = [];
    for (const url of ["/api/gateway/health", "/api/gateway/workspaces", "/api/v1/workspace"]) {
      const response = await fetch(url, { headers });
      assertResponse(response);
      const body = await response.json();
      if (url === "/api/gateway/workspaces") {
        owner = body.data.items.find((item) => item.backend_workspace_id === workspaceId);
        if (!owner) throw new Error("Gateway未登记测试目标");
        headers["X-BoxTeam-Workspace-Id"] = owner.workspace_id;
        headers["X-BoxTeam-Backend-Workspace-Id"] = owner.backend_workspace_id;
      }
      results.push({ url, requestId: response.headers.get("x-request-id"), bodyRequestId: body.request_id });
    }
    return results;
    function assertResponse(response) { if (!response.ok) throw new Error(`产品初始化失败：${response.status}`); }
  }, fixture.workspaceId);
  initialization.forEach((item) => { assert(item.requestId); assert.equal(item.requestId, item.bodyRequestId); });
  async function openAutomation() {
    const toggle = page.getByRole("button", { name: "切换底部面板", exact: true });
    if (await toggle.getAttribute("aria-pressed") !== "true") await toggle.click();
    await page.getByRole("tab", { name: "自动化", exact: true }).click();
    return page.getByRole("region", { name: "工作区持久任务", exact: true });
  }
  let panel = await openAutomation();
  await panel.getByRole("button", { name: /同一owner的暂停目标/ }).waitFor();
  await panel.getByRole("button", { name: "新建持久任务", exact: true }).click();
  let form = panel.getByRole("form", { name: "创建持久任务" });
  await form.getByLabel("任务名称", { exact: true }).fill("浏览器创建的任务");
  await form.getByLabel("触发方式", { exact: true }).selectOption("builtin.manual");
  await form.getByLabel("工作 Thread URI", { exact: true }).fill(fixture.targetUri);
  await form.getByLabel("提示内容", { exact: true }).fill("浏览器创建的提示正文");
  await form.getByLabel("动作", { exact: true }).selectOption("context_only");
  const created = page.waitForResponse((response) => new URL(response.url()).pathname === "/api/v1/persistent-tasks" && response.request().method() === "POST");
  await form.getByRole("button", { name: "保存任务", exact: true }).click();
  const createdResponse = await created;
  assert.equal(createdResponse.status(), 200);
  const createdBody = await createdResponse.json();
  assert.equal(createdResponse.headers()["x-request-id"], createdBody.request_id);
  const taskId = createdBody.data.task_id;
  await panel.getByRole("button", { name: "检查试运行", exact: true }).click();
  await panel.getByText("检查试运行 · 检查已结束", { exact: true }).waitFor();
  assert.equal(await panel.getByText("内容已保存，尚不代表模型已消费", { exact: true }).count(), 0);
  await panel.getByRole("button", { name: "编辑任务", exact: true }).click();
  form = panel.getByRole("form", { name: "编辑持久任务" });
  await form.getByLabel("判断方式", { exact: true }).selectOption("builtin.rule");
  await form.getByLabel("输入 JSON Pointer", { exact: true }).fill("/trigger/type");
  await form.getByLabel("比较值（JSON）", { exact: true }).fill('"manual"');
  await form.getByLabel("提示内容", { exact: true }).fill("浏览器编辑后的提示正文");
  await form.getByRole("button", { name: "保存任务", exact: true }).click();
  await panel.getByRole("button", { name: "立即检查", exact: true }).click();
  await panel.getByText("内容已保存，尚不代表模型已消费", { exact: false }).waitFor();
  // 模拟另一真实客户端在本次请求发出后编辑；原请求继续到后端产生真实409。
  let conflict = false;
  await page.route(`**/persistent-tasks/${taskId}/control`, async (route) => {
    const headers = await route.request().allHeaders();
    delete headers["content-length"];
    delete headers.host;
    const current = await context.request.get(`${fixture.baseUrl}/api/v1/persistent-tasks/${taskId}`, { headers });
    assert.equal(current.status(), 200);
    const task = (await current.json()).data;
    const changed = await context.request.put(`${fixture.baseUrl}/api/v1/persistent-tasks/${taskId}`, { headers, data: { expected_revision: task.revision, configuration: { ...task.configuration, name: "另一客户端修改的任务" } } });
    assert.equal(changed.status(), 200);
    conflict = true;
    await route.continue();
  }, { times: 1 });
  const rejected = page.waitForResponse((response) => new URL(response.url()).pathname.endsWith(`/${taskId}/control`) && response.status() === 409);
  await panel.getByRole("button", { name: "暂停任务", exact: true }).click();
  await rejected;
  await panel.getByRole("alert").filter({ hasText: /revision/ }).waitFor();
  await panel.getByRole("heading", { name: "另一客户端修改的任务", exact: true }).waitFor();
  await panel.getByRole("button", { name: "暂停任务", exact: true }).click();
  await panel.getByRole("button", { name: "恢复任务", exact: true }).click();
  await panel.getByRole("button", { name: "暂停任务", exact: true }).waitFor();
  await panel.getByRole("button", { name: "终止任务", exact: true }).click();
  await page.getByRole("dialog").getByRole("button", { name: "终止任务", exact: true }).click();
  await panel.getByRole("button", { name: "终止任务", exact: true }).waitFor({ state: "visible" });
  await page.waitForFunction(() => [...document.querySelectorAll(".persistent-task-actions button")].some((button) => button.textContent === "终止任务" && button.disabled));
  const preferences = page.waitForResponse((response) => new URL(response.url()).pathname === "/api/gateway/ui-settings" && response.request().method() === "GET");
  await page.reload({ waitUntil: "domcontentloaded" });
  assert.equal((await preferences).status(), 200);
  await page.locator("[data-session-id]").filter({ hasText: fixture.sessionTitle }).first().click();
  panel = await openAutomation();
  await panel.getByRole("button", { name: /另一客户端修改的任务/ }).click();
  assert.match(await panel.innerText(), /已终止/);
  assert(conflict);
  assert.deepEqual(errors, []);
  const sash = await page.getByRole("button", { name: "调整底部面板高度", exact: true }).boundingBox();
  assert(sash);
  await page.mouse.move(sash.x + sash.width / 2, sash.y + sash.height / 2);
  await page.mouse.down();
  await page.mouse.move(sash.x + sash.width / 2, sash.y - 280, { steps: 10 });
  await page.mouse.up();
  await page.screenshot({ path: path.join(artifacts, "task-management.png"), fullPage: true });
  await writeFile(path.join(artifacts, "actual.json"), JSON.stringify({ taskId, initialization, conflict, noPageErrors: true }, null, 2));
} catch (error) {
  if (page) {
    await page.screenshot({ path: path.join(artifacts, "failure.png"), fullPage: true });
    await writeFile(path.join(artifacts, "failure.html"), await page.content());
  }
  throw error;
} finally {
  try { await context?.close(); } finally { await browser.close(); }
}
