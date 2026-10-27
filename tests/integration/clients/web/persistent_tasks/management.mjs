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
  // 五类入口使用现有Goal视图或同一组件表单；切换模板不创建后台任务。
  await panel.getByRole("button", { name: "新建持久任务", exact: true }).click();
  const templates = panel.getByRole("region", { name: "选择持久任务模板", exact: true });
  for (const title of ["Goal", "定时执行", "脚本条件", "模型判断", "自定义"]) assert.equal(await templates.getByRole("button", { name: title, exact: true }).count(), 1);
  await templates.getByRole("button", { name: "Goal", exact: true }).click();
  await page.getByRole("region", { name: "当前 Goal", exact: true }).getByText("同一owner的暂停目标", { exact: true }).waitFor();
  const goalCard = page.getByRole("region", { name: "当前 Goal", exact: true });
  await goalCard.getByRole("button", { name: "编辑", exact: true }).click();
  const goalEditor = page.getByRole("dialog", { name: "编辑 Goal", exact: true });
  const objective = goalEditor.getByRole("textbox");
  assert.equal(await objective.inputValue(), "同一owner的暂停目标");
  await objective.fill("同一owner的编辑目标");
  await goalEditor.getByRole("button", { name: "保存 Goal", exact: true }).click();
  await panel.getByRole("button", { name: /同一owner的编辑目标/ }).waitFor();
  await page.getByRole("region", { name: "当前会话关联任务", exact: true }).getByRole("button", { name: "同一owner的编辑目标", exact: true }).waitFor();
  for (const [title, field] of [["脚本条件", "程序"], ["模型判断", "判断说明"], ["自定义", "触发方式"]]) {
    await panel.getByRole("button", { name: "新建持久任务", exact: true }).click();
    await panel.getByRole("region", { name: "选择持久任务模板", exact: true }).getByRole("button", { name: title, exact: true }).click();
    const form = panel.getByRole("form", { name: "创建持久任务" });
    await form.getByLabel(field, { exact: true }).waitFor();
    if (title === "自定义") {
      assert.equal(await form.getByLabel("触发方式", { exact: true }).inputValue(), "builtin.manual");
      assert.equal(await form.locator("details").getAttribute("open"), "");
    } else assert.equal(await form.getByLabel("启用附加条件", { exact: true }).isChecked(), true);
    await form.getByRole("button", { name: "取消编辑", exact: true }).click();
  }
  // 从实际底部面板入口预览生成导航；不点击创建，不替产品预写 catalog。
  await panel.getByRole("button", { name: "新建持久任务", exact: true }).click();
  await panel.getByRole("region", { name: "选择持久任务模板", exact: true }).getByRole("button", { name: "定时执行", exact: true }).click();
  const generator = panel.getByRole("form", { name: "创建持久任务" });
  await generator.getByLabel("任务名称", { exact: true }).fill("导航预览任务");
  await generator.getByLabel("工作动作", { exact: true }).selectOption("builtin.session_generation");
  await generator.getByLabel("标题模板", { exact: true }).fill("{generator.name}-输出");
  await generator.getByLabel("导航路径模板", { exact: true }).fill("自动化/预览");
  const previewReceipt = page.waitForResponse((response) => new URL(response.url()).pathname === "/api/v1/session-generations/preview-placement");
  await generator.getByRole("button", { name: "预览生成位置", exact: true }).click();
  const previewResponse = await previewReceipt;
  assert.equal(previewResponse.status(), 200);
  const preview = (await previewResponse.json()).data;
  assert.deepEqual(preview.navigation_path, ["自动化", "预览", "导航预览任务-输出"]);
  assert.equal(preview.workspace_id, fixture.workspaceId);
  assert.equal(preview.physical_location, "unallocated");
  await generator.getByText("导航预览任务-输出 · 自动化/预览/导航预览任务-输出", { exact: true }).waitFor();
  await page.screenshot({ path: path.join(artifacts, "generation-navigation-preview.png"), fullPage: true });
  await generator.getByRole("button", { name: "取消编辑", exact: true }).click();
  await panel.getByRole("button", { name: "新建持久任务", exact: true }).click();
  await panel.getByRole("region", { name: "选择持久任务模板", exact: true }).getByRole("button", { name: "定时执行", exact: true }).click();
  let form = panel.getByRole("form", { name: "创建持久任务" });
  assert.equal(await form.getByLabel("启用附加条件", { exact: true }).isChecked(), false);
  assert.equal(await form.getByLabel("判断方式", { exact: true }).count(), 0);
  await form.getByLabel("任务名称", { exact: true }).fill("浏览器创建的任务");
  await form.getByLabel("间隔（秒）", { exact: true }).fill("3600");
  await form.getByLabel("工作 Thread URI", { exact: true }).fill(fixture.targetUri);
  await form.getByLabel("提示内容", { exact: true }).fill("浏览器创建的提示正文");
  await form.getByLabel("动作", { exact: true }).selectOption("context_only");
  const created = page.waitForResponse((response) => new URL(response.url()).pathname === "/api/v1/persistent-tasks" && response.request().method() === "POST");
  await form.getByRole("button", { name: "保存任务", exact: true }).click();
  const createdResponse = await created;
  assert.equal(createdResponse.status(), 200);
  const createdBody = await createdResponse.json();
  assert.equal(createdBody.data.configuration.condition.type_id, "builtin.always");
  assert.equal(createdBody.data.configuration.condition.version, "1");
  assert.equal(createdResponse.headers()["x-request-id"], createdBody.request_id);
  const taskId = createdBody.data.task_id;
  await panel.getByRole("button", { name: "检查试运行", exact: true }).click();
  await panel.getByText("检查试运行 · 检查已结束", { exact: true }).waitFor();
  assert.equal(await panel.getByText("内容已保存，尚不代表模型已消费", { exact: true }).count(), 0);
  await panel.getByRole("button", { name: "编辑任务", exact: true }).click();
  form = panel.getByRole("form", { name: "编辑持久任务" });
  await form.getByLabel("启用附加条件", { exact: true }).check();
  await form.getByLabel("判断方式", { exact: true }).selectOption("builtin.rule");
  await form.getByLabel("输入 JSON Pointer", { exact: true }).fill("/trigger/type");
  await form.getByLabel("比较值（JSON）", { exact: true }).fill('"manual"');
  await form.getByLabel("提示内容", { exact: true }).fill("浏览器编辑后的提示正文");
  const updated = page.waitForResponse((response) => new URL(response.url()).pathname === `/api/v1/persistent-tasks/${taskId}` && response.request().method() === "PUT");
  await form.getByRole("button", { name: "保存任务", exact: true }).click();
  const updatedResponse = await updated;
  assert.equal(updatedResponse.status(), 200);
  const updatedTask = (await updatedResponse.json()).data;
  assert.equal(updatedTask.task_id, taskId);
  assert.equal(updatedTask.next_run_at, createdBody.data.next_run_at);
  assert.deepEqual(updatedTask.configuration.trigger, createdBody.data.configuration.trigger);
  await panel.getByRole("button", { name: "立即检查", exact: true }).click();
  await panel.getByText("内容已保存，尚不代表模型已消费", { exact: false }).waitFor();
  const deliveredRow = panel.locator(".persistent-task-runs > li").filter({ hasText: "内容已保存，尚不代表模型已消费" });
  const inspected = page.waitForResponse((response) => new URL(response.url()).pathname.startsWith(`/api/v1/persistent-tasks/${taskId}/runs/`));
  await deliveredRow.getByRole("button", { name: "查看运行详情", exact: true }).click();
  const inspectedResponse = await inspected;
  assert.equal(inspectedResponse.status(), 200);
  const detail = (await inspectedResponse.json()).data;
  assert.equal(detail.run.definition_revision, 2);
  assert.equal(detail.configuration.context.content, "浏览器编辑后的提示正文");
  assert(detail.action_content.includes("浏览器编辑后的提示正文"));
  assert.equal(detail.consumption, null);
  await deliveredRow.getByText("尚无输入进入派发请求的证据。", { exact: true }).waitFor();
  const decisionResult = deliveredRow.locator("details").filter({ has: page.locator("summary", { hasText: "结构化判断结果" }) });
  await decisionResult.locator("summary").click();
  assert.deepEqual(JSON.parse(await decisionResult.locator("pre").innerText()), { pointer: "/trigger/type", found: true, actual: "manual", expected: "manual" });
  await page.screenshot({ path: path.join(artifacts, "run-detail.png"), fullPage: true });
  await deliveredRow.getByRole("button", { name: "打开工作 Thread", exact: true }).click();
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
  await panel.getByRole("button", { name: /同一owner的编辑目标/ }).click();
  await panel.getByRole("button", { name: "管理会话 Goal", exact: true }).click();
  const linked = page.getByRole("region", { name: "当前会话关联任务", exact: true });
  await linked.getByRole("button", { name: "另一客户端修改的任务", exact: true }).click();
  await panel.getByRole("heading", { name: "另一客户端修改的任务", exact: true }).waitFor();
  // 切换会话重建原owner镜像；读取失败不能显示成“没有任务”。
  await page.locator(`[data-session-id="${fixture.otherSessionId}"]`).first().click();
  await linked.getByText("当前会话没有关联任务。", { exact: true }).waitFor();
  assert.equal(await linked.getByRole("button", { name: "另一客户端修改的任务", exact: true }).count(), 0);
  await page.route("**/persistent-tasks/events?session_id=*", (route) => route.abort("failed"), { times: 1 });
  await page.locator("[data-session-id]").filter({ hasText: fixture.sessionTitle }).first().click();
  await linked.getByRole("alert").waitFor();
  assert.equal(await linked.getByText("当前会话没有关联任务。", { exact: true }).count(), 0);
  await linked.getByRole("button", { name: "刷新关联任务", exact: true }).click();
  await linked.getByRole("button", { name: "另一客户端修改的任务", exact: true }).waitFor();
  await linked.getByRole("button", { name: "另一客户端修改的任务", exact: true }).click();
  await panel.getByRole("heading", { name: "另一客户端修改的任务", exact: true }).waitFor();
  assert(conflict);
  assert.deepEqual(errors, []);
  const sash = await page.getByRole("button", { name: "调整底部面板高度", exact: true }).boundingBox();
  assert(sash);
  await page.mouse.move(sash.x + sash.width / 2, sash.y + sash.height / 2);
  await page.mouse.down();
  await page.mouse.move(sash.x + sash.width / 2, sash.y - 280, { steps: 10 });
  await page.mouse.up();
  await page.screenshot({ path: path.join(artifacts, "task-management.png"), fullPage: true });
  await writeFile(path.join(artifacts, "actual.json"), JSON.stringify({ taskId, initialization, conflict, preview, noPageErrors: true }, null, 2));
} catch (error) {
  if (page) {
    await page.screenshot({ path: path.join(artifacts, "failure.png"), fullPage: true });
    await writeFile(path.join(artifacts, "failure.html"), await page.content());
  }
  throw error;
} finally {
  try { await context?.close(); } finally { await browser.close(); }
}
