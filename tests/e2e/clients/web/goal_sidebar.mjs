import assert from "node:assert/strict";
import { writeFile } from "node:fs/promises";
import { join } from "node:path";
import { chromium, expect } from "playwright/test";

const baseUrl = process.env.BOXTEAM_E2E_BASE_URL;
const artifacts = process.env.BOXTEAM_E2E_ARTIFACTS;
if (!baseUrl || !artifacts) throw new Error("Goal E2E 缺少服务地址或产物目录");
const browser = await chromium.launch({
  executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH || undefined,
  headless: true,
});
const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
const page = await context.newPage();
let token;
let panel;
const writes = [];
page.on("request", (request) => {
  if (request.method() === "PUT" && new URL(request.url()).pathname.endsWith("/goal")) {
    writes.push(request.postDataJSON());
  }
});

async function api(path, method = "GET", data) {
  const response = await context.request.fetch(`${baseUrl}${path}`, {
    method, data, headers: token ? { "X-Local-Token": token } : {},
  });
  assert.equal(response.status(), 200, `${method} ${path}: ${await response.text()}`);
  const body = await response.json();
  assert.equal(body.request_id, response.headers()["x-request-id"]);
  return body.data;
}

async function selectSession(session) {
  await page.locator(`button[data-session-id="${session.session_id}"]`).click();
  const thread = page.locator(`section.boxteam-thread[data-workspace-id="${session.workspace_id}"][data-session-id="${session.session_id}"][data-thread-id="${session.thread_id}"]`);
  await expect(thread).toBeVisible();
  const toggle = thread.getByRole("button", { name: "切换会话资源", exact: true });
  if (await toggle.getAttribute("aria-expanded") !== "true") await toggle.click();
  await thread.getByRole("tab", { name: "任务", exact: true }).click();
  panel = thread.getByRole("tabpanel", { name: "任务", exact: true });
}

try {
  token = (await api("/api/gateway/auth/local-credential")).token;
  const user = await api("/api/gateway/users", "POST", { display_name: "Goal 侧边栏测试" });
  await api(`/api/gateway/users/${user.user_id}/access`, "POST", { client_label: "Goal E2E" });
  await api("/api/gateway/health");
  await api("/api/gateway/workspaces");
  await api("/api/v1/workspace");
  const first = await api("/api/v1/sessions", "POST", { title: "Goal 管理验证" });
  const second = await api("/api/v1/sessions", "POST", { title: "无 Goal 的会话" });
  // 保持暂停，验证管理界面时不触发模型调用。
  const original = await api(`/api/v1/sessions/${first.session_id}/goal`, "PUT", {
    objective: "验证原始 Goal", status: "paused", token_budget: 1000,
  });
  const target = new URL(baseUrl);
  await page.goto(target.href, { waitUntil: "domcontentloaded" });
  await selectSession(first);
  await page.locator(`button[data-session-id="${first.session_id}"]`).dblclick();
  const tab = page.locator(`section.boxteam-thread[data-session-id="${first.session_id}"][data-thread-id="${first.thread_id}"]`)
    .getByRole("tab", { name: "任务", exact: true });
  await expect(panel).toContainText("验证原始 Goal");
  await panel.getByRole("button", { name: "验证原始 Goal", exact: true }).click();
  await expect(panel).toContainText("已暂停");
  await expect(panel).toContainText("Token 0 / 1,000");
  await panel.getByRole("button", { name: "编辑", exact: true }).click();
  let dialog = page.getByRole("dialog", { name: "编辑 Goal", exact: true });
  await dialog.getByRole("textbox", { name: "目标" }).fill("通过界面编辑的 Goal");
  await dialog.getByRole("button", { name: "保存 Goal", exact: true }).click();
  await expect(dialog).toHaveCount(0);
  await expect(panel).toContainText("通过界面编辑的 Goal");
  const edited = await api(`/api/v1/sessions/${first.session_id}/goal`);
  assert.equal(edited.goal_id, original.goal_id);
  assert.equal(edited.thread_id, first.thread_id);
  assert.equal(edited.status, "paused");
  assert.equal(edited.objective, "通过界面编辑的 Goal");
  assert.equal(edited.token_budget, original.token_budget);
  assert.deepEqual(writes, [{ objective: edited.objective, token_budget: original.token_budget, status: "paused" }]);
  // 新工作台把会话资源展示态保存到对应 EditorInput，由原版存储恢复。
  await page.evaluate(() => globalThis.boxteamWorkbenchLifecycle.prepareToClose());
  await page.reload({ waitUntil: "domcontentloaded" });
  await expect(tab).toHaveAttribute("aria-selected", "true");
  await selectSession(first);
  await expect(panel).toContainText(edited.objective);
  await panel.getByRole("button", { name: edited.objective, exact: true }).click();

  await panel.getByRole("button", { name: "编辑", exact: true }).click();
  await page.getByRole("dialog", { name: "编辑 Goal", exact: true })
    .getByRole("button", { name: "关闭", exact: true }).click();
  await selectSession(second);
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await expect(panel).toContainText("当前会话没有关联任务。");
  await expect(panel).not.toContainText(edited.objective);
  await panel.getByRole("button", { name: "新建持久任务", exact: true }).click();
  await panel.getByRole("button", { name: "Goal", exact: true }).click();
  await panel.getByRole("button", { name: "新建 Goal", exact: true }).click();
  dialog = page.getByRole("dialog", { name: "新建 Goal", exact: true });
  await expect(dialog.getByRole("button", { name: "开始 Goal", exact: true })).toBeVisible();
  await dialog.getByRole("button", { name: "关闭", exact: true }).click();
  await selectSession(first);
  await expect(panel).toContainText(edited.objective);
  await panel.getByRole("button", { name: edited.objective, exact: true }).click();
  await panel.getByRole("button", { name: "删除 Goal", exact: true }).click();
  const confirmation = page.getByRole("dialog");
  await expect(confirmation).toContainText("会话消息保留");
  await confirmation.getByRole("button", { name: "取消", exact: true }).click();
  assert.equal((await api(`/api/v1/sessions/${first.session_id}/goal`)).goal_id, original.goal_id);
  await panel.getByRole("button", { name: "删除 Goal", exact: true }).click();
  await page.getByRole("dialog", { name: "删除当前 Goal？", exact: true })
    .getByRole("button", { name: "删除 Goal", exact: true }).click();
  await expect(panel).toContainText("当前会话没有关联任务。");
  assert.equal(await api(`/api/v1/sessions/${first.session_id}/goal`), null);
  // 从会话侧栏创建普通任务，验证默认目标、直接管理及两处完整镜像。
  await panel.getByRole("button", { name: "新建持久任务", exact: true }).click();
  await panel.getByRole("button", { name: "自定义", exact: true }).click();
  let taskForm = panel.getByRole("form", { name: "创建持久任务" });
  assert.equal(await taskForm.getByLabel("工作 Thread URI", { exact: true }).inputValue(),
    `boxteam://workspace/${first.workspace_id}/resources/session/${first.session_id}/threads/${first.thread_id}`);
  await taskForm.getByLabel("任务名称", { exact: true }).fill("会话侧栏创建的任务");
  await taskForm.getByLabel("提示内容", { exact: true }).fill("服务上线后检查状态");
  await taskForm.getByLabel("动作", { exact: true }).selectOption("context_only");
  const createdReceipt = page.waitForResponse((response) => new URL(response.url()).pathname === "/api/v1/persistent-tasks" && response.request().method() === "POST");
  await taskForm.getByRole("button", { name: "保存任务", exact: true }).click();
  const createdResponse = await createdReceipt;
  assert.equal(createdResponse.status(), 200);
  const created = (await createdResponse.json()).data;
  await panel.getByRole("button", { name: "暂停任务", exact: true }).click();
  await expect(panel.getByRole("button", { name: "恢复任务", exact: true })).toBeVisible();
  await panel.getByRole("button", { name: "编辑任务", exact: true }).click();
  taskForm = panel.getByRole("form", { name: "编辑持久任务" });
  await taskForm.getByLabel("任务名称", { exact: true }).fill("会话侧栏编辑的任务");
  await taskForm.getByRole("button", { name: "保存任务", exact: true }).click();
  await expect(panel.getByRole("heading", { name: "会话侧栏编辑的任务", exact: true })).toBeVisible();
  await page.evaluate(() => globalThis.workbench.commands.executeCommand("boxteam.automation.focus"));
  const workspaceTasks = page.getByRole("region", { name: "工作区持久任务", exact: true });
  await workspaceTasks.getByRole("button", { name: "会话侧栏编辑的任务", exact: true }).click();
  await expect(workspaceTasks.getByRole("button", { name: "恢复任务", exact: true })).toBeVisible();
  await workspaceTasks.getByRole("button", { name: "恢复任务", exact: true }).click();
  await expect(panel.getByRole("button", { name: "暂停任务", exact: true })).toBeVisible();
  const persisted = await api(`/api/v1/persistent-tasks/${created.task_id}`);
  assert.equal(persisted.task_id, created.task_id);
  assert.equal(persisted.lifecycle, "active");
  assert.equal(persisted.configuration.name, "会话侧栏编辑的任务");
  await panel.getByRole("button", { name: "返回任务列表", exact: true }).click();
  await selectSession(second);
  await expect(panel).toContainText("当前会话没有关联任务。");
  await expect(panel).not.toContainText(persisted.configuration.name);
  await selectSession(first);
  await panel.getByRole("button", { name: persisted.configuration.name, exact: true }).click();
  await expect(panel.getByRole("button", { name: "暂停任务", exact: true })).toBeVisible();
  await page.screenshot({ path: join(artifacts, "session-tasks.png") });
  await writeFile(join(artifacts, "result.json"), JSON.stringify({
    sessionId: first.session_id, mainThreadId: first.thread_id,
    editorViewRestored: true, editedThroughUI: true, sessionSwitchIsolated: true,
    deletedThroughUI: true, deletionCancelledThroughUI: true, taskId: created.task_id, sidebarCreateAndEdit: true, sharedState: true, modelCalls: 0,
  }, null, 2));
} catch (error) {
  await page.screenshot({ path: join(artifacts, "goal-sidebar-failure.png") });
  throw error;
} finally {
  await browser.close();
}
