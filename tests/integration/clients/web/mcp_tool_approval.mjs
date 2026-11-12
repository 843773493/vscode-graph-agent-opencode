import assert from "node:assert/strict";
import { mkdir, writeFile } from "node:fs/promises";
import path from "node:path";
import { chromium } from "playwright";
import { expect } from "playwright/test";

const fixture = JSON.parse(process.env.BOXTEAM_BROWSER_FIXTURE);
const artifacts = process.env.BOXTEAM_BROWSER_ARTIFACTS;
assert(artifacts, "需要显式浏览器产物目录");
await mkdir(artifacts, { recursive: true });
let browser;
let context;
let page;
try {
  browser = await chromium.launch({ executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH, headless: true });
  context = await browser.newContext({ viewport: { width: 1600, height: 1050 } });
  page = await context.newPage();
  const errors = [];
  const approvalReads = [];
  page.on("request", (request) => {
    if (new URL(request.url()).pathname === "/api/v1/jobs/tool-approvals") approvalReads.push(request.url());
  });
  page.on("pageerror", (error) => errors.push(String(error)));
  const target = new URL(fixture.baseUrl);
  await page.goto(target.href, { waitUntil: "domcontentloaded" });
  await page.reload({ waitUntil: "domcontentloaded" });
  await expect(page.locator("#workbench .monaco-workbench")).toBeVisible();
  const session = page.locator(`button[data-session-id="${fixture.sessionId}"]`);
  await expect(session).toContainText(fixture.sessionTitle);
  await session.dblclick();
  const thread = page.locator(`section.boxteam-thread[data-session-id="${fixture.sessionId}"][data-thread-id="${fixture.threadId}"]`);
  await expect(thread).toBeVisible();
  const initialization = await page.evaluate(async (workspaceId) => {
    const tokenResponse = await fetch("/api/gateway/auth/local-credential");
    if (!tokenResponse.ok) throw new Error("读取本机凭证失败");
    const token = (await tokenResponse.json()).data.token;
    const results = [];
    let owner;
    let gatewayId;
    for (const url of ["/api/gateway/health", "/api/gateway/workspaces", "/api/v1/workspace"]) {
      const headers = { "X-Local-Token": token };
      if (url === "/api/v1/workspace") {
        if (!owner) throw new Error("缺少已注册的目标工作区");
        headers["X-BoxTeam-Target-Gateway-Id"] = owner.gateway_id;
        headers["X-BoxTeam-Workspace-Id"] = owner.workspace_id;
      }
      const response = await fetch(url, { headers });
      const body = await response.json();
      if (url === "/api/gateway/health") gatewayId = body.data.gateway_id;
      if (url === "/api/gateway/workspaces") {
        const candidates = body.data.items.filter(item => item.gateway_id === gatewayId && item.workspace_id === workspaceId);
        if (candidates.length !== 1) throw new Error("Gateway／UUID 目标必须唯一登记");
        [owner] = candidates;
      }
      results.push({ url, status: response.status, requestId: response.headers.get("x-request-id"), body });
    }
    return { results, owner };
  }, fixture.workspaceId);
  for (const result of initialization.results) {
    assert.equal(result.status, 200, result.url);
    assert(result.requestId);
    assert.equal(result.requestId, result.body.request_id);
  }
  // 覆盖旧轮询周期：没有等待确认事件时，真实入口不能发出审批查询。
  await page.waitForTimeout(1700);
  assert.equal(approvalReads.length, 0, "普通会话不应轮询工具审批");
  await thread.getByTestId("composer-input").fill("发现并发出两个 probe，等待我逐一审批。");
  const accepted = page.waitForResponse((response) => new URL(response.url()).pathname.endsWith("/messages") && response.request().method() === "POST");
  await thread.getByTestId("composer-send").click();
  const acceptance = await accepted;
  assert.equal(acceptance.status(), 200);
  assert.equal(new URL(acceptance.url()).pathname, `/api/v1/sessions/${fixture.sessionId}/threads/${fixture.threadId}/messages`);
  const messageHeaders = await acceptance.request().allHeaders();
  const acceptedOwner = initialization.owner;
  assert.equal(messageHeaders["x-boxteam-target-gateway-id"], acceptedOwner.gateway_id);
  assert.equal(messageHeaders["x-boxteam-workspace-id"], acceptedOwner.workspace_id);
  await expect(thread).toHaveAttribute("data-workspace-id", fixture.workspaceId);
  const jobId = (await acceptance.json()).data.job_id;
  const panel = thread.getByRole("complementary", { name: "待确认的工具操作" });
  await panel.getByRole("heading", { name: "确认工具操作" }).waitFor({ timeout: 60000 });
  // 刷新后从目标后端重新读取同一检查点，不依赖内存表单状态。
  await page.evaluate(() => globalThis.boxteamWorkbenchLifecycle.prepareToClose());
  await page.reload({ waitUntil: "domcontentloaded" });
  await expect(thread).toBeVisible();
  await panel.getByRole("heading", { name: "确认工具操作" }).waitFor({ timeout: 30000 });
  const actions = panel.locator("fieldset");
  assert.equal(await actions.count(), 2);
  await actions.nth(0).getByLabel("操作决定").selectOption("edit");
  await actions.nth(0).getByLabel("参数 JSON").fill('{"proof":"approved-in-browser"}');
  await actions.nth(1).getByLabel("操作决定").selectOption("reject");
  await actions.nth(1).getByLabel("拒绝原因").fill("浏览器明确拒绝第二个调用");
  const submitted = page.waitForResponse((response) => new URL(response.url()).pathname === `/api/v1/jobs/${jobId}/approvals` && response.request().method() === "POST");
  await panel.getByRole("button", { name: "提交审批决定", exact: true }).click();
  const result = await submitted;
  const body = await result.json();
  assert.equal(result.status(), 200, JSON.stringify(body));
  assert.equal(result.headers()["x-request-id"], body.request_id);
  assert.equal(body.data.status, "accepted");
  const approvalHeaders = await result.request().allHeaders();
  assert.equal(approvalHeaders["x-boxteam-target-gateway-id"], acceptedOwner.gateway_id);
  assert.equal(approvalHeaders["x-boxteam-workspace-id"], acceptedOwner.workspace_id);
  const request = result.request().postDataJSON();
  assert.equal(request.responses[0].decisions[0].type, "edit");
  assert.equal(request.responses[0].decisions[1].type, "reject");
  await thread.getByText("工具审批已按原调用逐一结算。", { exact: false }).first().waitFor({ timeout: 60000 });
  assert.equal(await panel.count(), 0);
  assert.deepEqual(errors, []);
  await page.screenshot({ path: path.join(artifacts, "approval-complete.png"), fullPage: true });
  await writeFile(path.join(artifacts, "approval-result.json"), JSON.stringify({ jobId, request, body, initialization, errors }, null, 2));
} catch (error) {
  if (page) {
    await page.screenshot({ path: path.join(artifacts, "approval-failure.png"), fullPage: true });
    await writeFile(path.join(artifacts, "approval-failure.html"), await page.content());
  }
  throw error;
} finally {
  try { await context?.close(); }
  finally { await browser?.close(); }
}
