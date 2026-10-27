import assert from "node:assert/strict";
import { writeFile } from "node:fs/promises";
import path from "node:path";
import { chromium } from "playwright";
import { testControl } from "../../../../support/e2e_control/browser_client.mjs";

const fixture = JSON.parse(process.env.BOXTEAM_BROWSER_FIXTURE);
const artifacts = process.env.BOXTEAM_BROWSER_ARTIFACTS;
assert(artifacts, "必须指定浏览器产物目录");
const browser = await chromium.launch({ executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH, headless: true });
const context = await browser.newContext({ viewport: { width: 1600, height: 1050 } });
const page = await context.newPage();
const barriers = new Set();
try {
  const errors = [];
  page.on("pageerror", (error) => errors.push(String(error)));
  await page.goto(fixture.baseUrl, { waitUntil: "domcontentloaded" });
  await page.getByRole("button", { name: "用户视图", exact: true }).click();
  await page.getByPlaceholder("新用户名称").fill("会话动作验收用户");
  await page.getByRole("button", { name: "创建", exact: true }).click();
  await page.locator("[data-session-id]").filter({ hasText: fixture.sessionTitle }).first().click();
  const headers = await page.evaluate(async (workspaceId) => {
    const credential = await fetch("/api/gateway/auth/local-credential");
    if (!credential.ok) throw new Error(`凭据读取失败：${credential.status}`);
    const headers = { "X-Local-Token": (await credential.json()).data.token };
    for (const url of ["/api/gateway/health", "/api/gateway/workspaces", "/api/v1/workspace"]) {
      const response = await fetch(url, { headers });
      if (!response.ok) throw new Error(`初始化失败：${url}/${response.status}`);
      const body = await response.json();
      if (!body.request_id || body.request_id !== response.headers.get("x-request-id")) throw new Error("初始化请求身份不一致");
      if (url === "/api/gateway/workspaces") {
        const owner = body.data.items.find((item) => item.backend_workspace_id === workspaceId);
        if (!owner) throw new Error("Gateway未登记目标");
        headers["X-BoxTeam-Workspace-Id"] = owner.workspace_id;
        headers["X-BoxTeam-Backend-Workspace-Id"] = owner.backend_workspace_id;
      }
    }
    return headers;
  }, fixture.workspaceId);
  const request = async (url) => {
    const response = await context.request.get(fixture.baseUrl + url, { headers });
    assert.equal(response.status(), 200, await response.text());
    return (await response.json()).data;
  };
  const root = "/api/v1/persistent-tasks";
  const task = (await request(root)).items.find((item) => item.configuration?.name === "自动打开的会话任务");
  assert(task);
  const toggle = page.getByRole("button", { name: "切换底部面板", exact: true });
  if (await toggle.getAttribute("aria-pressed") !== "true") await toggle.click();
  await page.getByRole("tab", { name: "自动化", exact: true }).click();
  const panel = page.getByRole("region", { name: "工作区持久任务", exact: true });
  await panel.getByRole("button", { name: /自动打开的会话任务/ }).click();
  await panel.getByText("暂无运行记录。", { exact: true }).waitFor();
  for (const [id, phase, details] of [
    ["generation-created", "task.generation.session_created_before_record", { task_id: task.task_id }],
    ["generation-executing", "thread.runtime.acquired", { execution_origin: "user_turn" }],
  ]) {
    await testControl({ action: "arm_match", barrier_id: id, phase, details }); barriers.add(id);
  }
  await panel.getByRole("button", { name: "立即检查", exact: true }).click();
  await testControl({ action: "acknowledged_match", barrier_id: "generation-created" });
  // 读取真实中间状态；只阻塞产品阶段，不伪造接口或补写任何执行数据。
  const read = page.waitForResponse((response) => new URL(response.url()).pathname === `${root}/${task.task_id}/runs`);
  await panel.getByRole("button", { name: "刷新任务", exact: true }).click();
  const before = (await (await read).json()).data.items[0];
  assert.equal(before.generation.status, "queued"); assert.deepEqual(before.generation.outputs, []);
  await panel.getByText("会话动作：queued", { exact: true }).waitFor();
  await testControl({ action: "release_match", barrier_id: "generation-created" }); barriers.delete("generation-created");
  await testControl({ action: "acknowledged_match", barrier_id: "generation-executing" });
  const accepted = (await request(`${root}/${task.task_id}/runs`)).items[0];
  assert.equal(accepted.generation.status, "queued");
  assert.equal(accepted.generation.outputs[0].session_id, fixture.otherSessionId);
  // 没有点击输出会话；自动切换后其真实消息才会出现在中心会话区。
  await testControl({ action: "release_match", barrier_id: "generation-executing" }); barriers.delete("generation-executing");
  await page.getByText("自动激活验收完成。", { exact: true }).waitFor();
  await panel.getByText("会话动作：completed", { exact: true }).waitFor();
  assert.deepEqual(errors, []);
  await page.screenshot({ path: path.join(artifacts, "generation-auto-open.png"), fullPage: true });
  await writeFile(path.join(artifacts, "actual.json"), JSON.stringify({ before, accepted, noPageErrors: true }, null, 2));
} catch (error) {
  await page.screenshot({ path: path.join(artifacts, "failure.png"), fullPage: true });
  await writeFile(path.join(artifacts, "failure.html"), await page.content());
  throw error;
} finally {
  for (const id of barriers) await testControl({ action: "release_match", barrier_id: id });
  try { await context.close(); } finally { await browser.close(); }
}
