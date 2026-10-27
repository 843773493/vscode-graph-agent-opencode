import assert from "node:assert/strict";
import { writeFile } from "node:fs/promises";
import path from "node:path";
import { chromium } from "playwright";

const fixture = JSON.parse(process.env.BOXTEAM_BROWSER_FIXTURE);
const secondary = JSON.parse(process.env.BOXTEAM_SECONDARY_WORKSPACE);
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
  await page.getByRole("button", { name: "用户视图", exact: true }).click();
  await page.getByPlaceholder("新用户名称").fill("跨工作区任务验收用户");
  await page.getByRole("button", { name: "创建", exact: true }).click();
  const initialization = await page.evaluate(async () => {
    const response = await fetch("/api/gateway/auth/local-credential");
    if (!response.ok) throw new Error(`凭据读取失败：${response.status}`);
    const headers = { "X-Local-Token": (await response.json()).data.token };
    const receipts = [];
    for (const url of ["/api/gateway/health", "/api/gateway/workspaces", "/api/v1/workspace"]) {
      const result = await fetch(url, { headers });
      if (!result.ok) throw new Error(`初始化失败：${url}/${result.status}`);
      const body = await result.json();
      receipts.push({ url, requestId: result.headers.get("x-request-id"), bodyRequestId: body.request_id, data: body.data });
    }
    return receipts;
  });
  for (const receipt of initialization) {
    assert(receipt.requestId); assert.equal(receipt.requestId, receipt.bodyRequestId);
  }
  const primary = initialization.find((row) => row.url === "/api/gateway/workspaces").data.items.find((row) => row.backend_workspace_id === fixture.workspaceId);
  assert(primary);
  const activate = async (routeId, sessionId) => {
    const row = page.getByTestId(`workspace-node-${routeId}`);
    if (await row.getAttribute("aria-current") !== "location") {
      const response = page.waitForResponse((entry) => entry.request().method() === "POST"
        && new URL(entry.url()).pathname === `/api/gateway/workspaces/${routeId}/activate`);
      await row.locator(".workspace-label").click();
      assert.equal((await response).status(), 200);
    }
    await page.waitForFunction((id) => document.querySelector(`[data-testid="workspace-node-${id}"]`)?.getAttribute("aria-current") === "location", routeId);
    if (await row.locator("..").getAttribute("aria-expanded") === "false") await row.locator(".session-resource-chevron").click();
    await page.locator(`[data-session-id="${sessionId}"]`).first().click();
    const toggle = page.getByRole("button", { name: "切换底部面板", exact: true });
    if (await toggle.getAttribute("aria-pressed") !== "true") await toggle.click();
    await page.getByRole("tab", { name: "自动化", exact: true }).click();
    const sidebar = page.getByRole("button", { name: "切换右侧侧边栏", exact: true });
    if (await sidebar.getAttribute("aria-pressed") !== "true") await sidebar.click();
    await page.getByRole("tab", { name: "Goal", exact: true }).click();
    return page.getByRole("region", { name: "工作区持久任务", exact: true });
  };
  const firstSession = new URL(fixture.targetUri).pathname.split("/").at(-3);
  let panel = await activate(primary.workspace_id, firstSession);
  await panel.getByRole("button", { name: /工作区 A 原任务/ }).click();
  await panel.getByText("暂无运行记录。", { exact: true }).waitFor();
  const summary = page.getByRole("region", { name: "当前会话关联任务", exact: true });
  await summary.getByRole("button", { name: "工作区 A 原任务", exact: true }).waitFor();
  assert.equal(await page.getByRole("region", { name: "当前 Goal", exact: true }).count(), 1);

  panel = await activate(secondary.routeId, secondary.sessionId);
  await panel.getByRole("button", { name: /工作区 B 原任务/ }).click();
  await panel.getByRole("heading", { name: "工作区 B 原任务", exact: true }).waitFor();
  await summary.getByRole("button", { name: "工作区 B 原任务", exact: true }).waitFor();
  assert.equal(await page.getByRole("region", { name: "当前 Goal", exact: true }).count(), 1);
  assert.equal(await panel.getByRole("button", { name: /工作区 A 原任务/ }).count(), 0);
  assert.equal(await summary.getByRole("button", { name: "工作区 A 原任务", exact: true }).count(), 0);
  await page.getByRole("region", { name: "当前 Goal", exact: true }).getByText("工作区 B 暂停目标", { exact: true }).waitFor();
  await panel.getByRole("button", { name: "编辑任务", exact: true }).click();
  const form = panel.getByRole("form", { name: "编辑持久任务" });
  assert.equal(await form.getByLabel("工作 Thread URI", { exact: true }).inputValue(), secondary.targetUri);
  await form.getByLabel("任务名称", { exact: true }).fill("工作区 B 编辑任务");
  await form.getByRole("button", { name: "保存任务", exact: true }).click();
  await panel.getByRole("heading", { name: "工作区 B 编辑任务", exact: true }).waitFor();
  await panel.getByRole("button", { name: "检查试运行", exact: true }).click();
  await panel.getByText("检查试运行 · 检查已结束", { exact: true }).waitFor();
  await panel.getByRole("button", { name: "立即检查", exact: true }).click();
  await panel.getByText("内容已保存，尚不代表模型已消费", { exact: false }).waitFor();
  await panel.getByRole("button", { name: "暂停任务", exact: true }).click();
  await panel.getByRole("button", { name: "恢复任务", exact: true }).waitFor();
  await page.screenshot({ path: path.join(artifacts, "workspace-b.png"), fullPage: true });

  panel = await activate(primary.workspace_id, firstSession);
  await panel.getByRole("button", { name: /工作区 A 原任务/ }).click();
  await panel.getByText("暂无运行记录。", { exact: true }).waitFor();
  await summary.getByRole("button", { name: "工作区 A 原任务", exact: true }).waitFor();
  assert.equal(await page.getByRole("region", { name: "当前 Goal", exact: true }).count(), 1);
  assert.equal(await panel.getByRole("button", { name: /工作区 B 编辑任务/ }).count(), 0);
  assert.equal(await summary.getByRole("button", { name: "工作区 B 编辑任务", exact: true }).count(), 0);
  await page.getByRole("region", { name: "当前 Goal", exact: true }).getByText("工作区 A 暂停目标", { exact: true }).waitFor();
  await panel.getByRole("button", { name: "暂停任务", exact: true }).click();
  await panel.getByRole("button", { name: "恢复任务", exact: true }).waitFor();
  await page.screenshot({ path: path.join(artifacts, "workspace-a.png"), fullPage: true });
  assert.deepEqual(errors, []);
  await writeFile(path.join(artifacts, "actual.json"), JSON.stringify({ initialization, secondTaskId: secondary.task.task_id, noPageErrors: true }, null, 2));
} catch (error) {
  if (page) {
    await page.screenshot({ path: path.join(artifacts, "failure.png"), fullPage: true });
    await writeFile(path.join(artifacts, "failure.html"), await page.content());
  }
  throw error;
} finally {
  try { await context?.close(); } finally { await browser.close(); }
}
