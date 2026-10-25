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
  await page.locator(`[data-session-id="${session.session_id}"]`).click();
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
  // 已有四标签布局必须由设置 owner 补齐 Goal，并保留用户的相对排序。
  const settings = await api("/api/gateway/ui-settings", "PUT", { layout: {
    auxiliary_visible: true,
    auxiliary_tab: "files",
    auxiliary_tab_order: ["files", "changes", "debug", "resources"],
  } });
  assert.deepEqual(settings.layout.auxiliary_tab_order, ["files", "changes", "goal", "debug", "resources"]);

  await page.goto(baseUrl, { waitUntil: "domcontentloaded" });
  await selectSession(first);
  const tab = page.getByRole("tab", { name: "Goal", exact: true });
  await tab.click();
  const panel = page.getByRole("tabpanel", { name: "Goal", exact: true });
  await expect(panel).toContainText("验证原始 Goal");
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
  assert.deepEqual(writes, [{ objective: edited.objective, status: "paused" }]);
  assert.equal((await api("/api/gateway/ui-settings")).layout.auxiliary_tab, "goal");
  await page.reload({ waitUntil: "domcontentloaded" });
  await expect(tab).toHaveAttribute("aria-selected", "true");
  await selectSession(first);
  await expect(panel).toContainText(edited.objective);

  await panel.getByRole("button", { name: "编辑", exact: true }).click();
  await page.getByRole("dialog", { name: "编辑 Goal", exact: true })
    .getByRole("button", { name: "关闭", exact: true }).click();
  await selectSession(second);
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await expect(panel).toContainText("当前会话没有 Goal");
  await expect(panel).not.toContainText(edited.objective);
  await panel.getByRole("button", { name: "新建 Goal", exact: true }).click();
  dialog = page.getByRole("dialog", { name: "新建 Goal", exact: true });
  await expect(dialog.getByRole("button", { name: "开始 Goal", exact: true })).toBeVisible();
  await dialog.getByRole("button", { name: "关闭", exact: true }).click();
  await selectSession(first);
  await expect(panel).toContainText(edited.objective);
  await panel.getByRole("button", { name: "清除", exact: true }).click();
  const confirmation = page.getByRole("dialog");
  await confirmation.getByRole("button", { name: "清除 Goal", exact: true }).click();
  await expect(panel).toContainText("当前会话没有 Goal");
  assert.equal(await api(`/api/v1/sessions/${first.session_id}/goal`), null);
  await page.screenshot({ path: join(artifacts, "goal-sidebar.png") });
  await writeFile(join(artifacts, "result.json"), JSON.stringify({
    sessionId: first.session_id, mainThreadId: first.thread_id,
    settingsRestored: true, editedThroughUI: true, sessionSwitchIsolated: true,
    clearedThroughUI: true, modelCalls: 0,
  }, null, 2));
} catch (error) {
  await page.screenshot({ path: join(artifacts, "goal-sidebar-failure.png") });
  throw error;
} finally {
  await browser.close();
}
