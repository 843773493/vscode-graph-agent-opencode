import assert from "node:assert/strict";
import { writeFile } from "node:fs/promises";
import { chromium } from "playwright";
import { expect } from "playwright/test";
import { TestRunContext } from "../../../harness/js/run-context.mjs";
import { webSelectors } from "../../../clients/selectors/web-selectors.mjs";
import { selectThread } from "./persistent_tasks/workbench.mjs";

const required = name => {
  assert(process.env[name], `缺少环境变量 ${name}`);
  return process.env[name];
};
const baseUrl = required("BOXTEAM_BROWSER_BASE_URL");
const owner = JSON.parse(required("BOXTEAM_BROWSER_OWNER"));
const resultPath = required("BOXTEAM_BROWSER_RESULT_PATH");
const final = required("BOXTEAM_BROWSER_FINAL");
const modelCalls = JSON.parse(required("BOXTEAM_BROWSER_MODEL_CALL_IDS"));
const safeError = "ResourceIdentity resource_identity 不得携带路径/URI/credential 形态";
const run = await TestRunContext.fromTestFile("tests/integration/clients/web/test_resource_identity_tool_loop.py").prepare();
let page;
let executionError;
let actual = { owner, phase: "setup", pageErrors: [] };

async function inspectTurn(thread, turnId) {
  const turn = thread.locator(`.chat-turn[data-turn-id="${turnId}"]`);
  await expect(turn.getByText(final, { exact: true })).toBeVisible({ timeout: 45_000 });
  const expand = turn.getByRole("button", { name: /^展开 Turn 中间消息：/ });
  await expect(expand).toBeVisible();
  await expand.click();
  const history = await page.evaluate(async ({ owner, turnId }) => {
    const credential = await fetch("/api/gateway/auth/local-credential");
    if (!credential.ok) throw new Error(`本地凭据 HTTP ${credential.status}`);
    const response = await fetch(`/api/v1/sessions/${owner.sessionId}/threads/${owner.threadId}/history`, {
      method: "POST", headers: { "Content-Type": "application/json", "X-Local-Token": (await credential.json()).data.token,
        "X-BoxTeam-Target-Gateway-Id": owner.gatewayId, "X-BoxTeam-Workspace-Id": owner.workspaceId },
      body: JSON.stringify({ turn_ids: [turnId], include: ["tool_call", "tool_result"] }),
    });
    return { status: response.status, requestId: response.headers.get("x-request-id"), body: await response.json() };
  }, { owner, turnId });
  assert.equal(history.status, 200);
  assert.equal(history.body.request_id, history.requestId);
  assert.equal(history.body.data.thread_id, owner.threadId);
  const [detail] = history.body.data.items;
  assert.equal(detail.session_id, owner.sessionId);
  assert.equal(detail.turn_id, turnId);
  const parts = detail.response_parts.filter(part => ["tool_call", "tool_result"].includes(part.kind));
  const rejected = [0, 1, 2].map(index => `${modelCalls[0]}:tool-call:rejected-${index}`);
  const corrected = `${modelCalls[1]}:tool-call:corrected-0`;
  assert.deepEqual(parts.map(part => [part.kind, part.tool_call_id]), [
    ...rejected.map(id => ["tool_call", id]), ...rejected.map(id => ["tool_result", id]),
    ["tool_call", corrected], ["tool_result", corrected],
  ]);
  const rows = turn.locator(".chat-tool-row");
  // 并发调用与结果保持权威位置；只有最后一组相邻调用/结果合成一张卡片。
  await expect(rows).toHaveCount(7);
  await expect(turn.locator(".chat-tool-row.is-failed")).toHaveCount(6);
  await expect(turn.locator(".chat-tool-row.is-complete")).toHaveCount(1);
  const results = [];
  const sources = [];
  for (let index = 0; index < 7; index++) {
    const row = rows.nth(index);
    const references = JSON.parse(await row.getAttribute("data-response-sources"));
    sources.push(...references);
    const summary = row.locator(".chat-tool-summary");
    if (await summary.getAttribute("aria-expanded") !== "true") await summary.click();
    if (index < 3) {
      const input = row.locator(".chat-tool-details pre code").first();
      await expect(input).toBeVisible();
      // 原调用参数逐字保留其目标；错误输出与原生路径输入分别验收。
      assert.deepEqual(JSON.parse(await input.innerText()), JSON.parse(parts[index].arguments));
      continue;
    }
    const output = row.locator(".chat-tool-details pre code").last();
    await expect(output).toBeVisible();
    if (index < 6) {
      await expect(output).toHaveText(safeError);
      results.push((await output.innerText()).trim());
    } else {
      assert.equal(JSON.parse(await output.innerText()).scope, "workspace");
    }
  }
  assert.deepEqual(sources, parts.map(({ part_id, kind, source }) => ({ part_id, kind, source })));
  return { failed: 6, completed: 1, results, partIds: sources.map(part => part.part_id) };
}

try {
  const browser = await chromium.launch({ executablePath: required("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH"), headless: true });
  run.addCleanup("身份验收 Chromium", () => browser.close());
  const context = await browser.newContext({ viewport: { width: 1920, height: 1100 } });
  run.addCleanup("身份验收 context", () => context.close());
  page = await context.newPage();
  page.on("pageerror", error => actual.pageErrors.push(String(error)));
  await page.goto(baseUrl, { waitUntil: "domcontentloaded" });
  await page.reload({ waitUntil: "domcontentloaded" });
  await expect(page.locator("#workbench .monaco-workbench")).toBeVisible();
  let thread = await selectThread(page, owner, 0);
  actual.phase = "send";
  const message = "请核对资源身份，纠正后读取当前工作区的会话目录。";
  const messagePath = `/api/v1/sessions/${owner.sessionId}/threads/${owner.threadId}/messages`;
  const sent = page.waitForResponse(response => response.request().method() === "POST"
    && new URL(response.url()).pathname === messagePath);
  await thread.locator(webSelectors.composer).fill(message);
  await thread.locator(webSelectors.sendButton).click();
  const response = await sent;
  assert.equal(response.status(), 200, await response.text());
  assert.equal(response.request().headers()["x-boxteam-workspace-id"], owner.workspaceId);
  assert.equal(response.request().postDataJSON().message.content, message);
  const body = await response.json();
  assert.equal(body.request_id, response.headers()["x-request-id"]);
  assert.equal(body.data.thread_id, owner.threadId);
  actual.jobId = body.data.job_id;
  actual.turnId = body.data.turn_id;
  actual.phase = "live";
  actual.live = await inspectTurn(thread, actual.turnId);
  actual.phase = "refresh";
  const bootstrap = page.waitForResponse(response => {
    const url = new URL(response.url());
    return response.request().method() === "GET" && url.pathname === `/api/v1/sessions/${owner.sessionId}/bootstrap`
      && url.searchParams.get("thread_id") === owner.threadId
      && response.request().headers()["x-boxteam-workspace-id"] === owner.workspaceId;
  });
  await page.reload({ waitUntil: "domcontentloaded" });
  thread = await selectThread(page, owner, 0);
  const history = await bootstrap;
  assert.equal(history.status(), 200, await history.text());
  const restored = await history.json();
  assert.equal(restored.request_id, history.headers()["x-request-id"]);
  assert.equal(restored.data.session.session_id, owner.sessionId);
  assert.equal(restored.data.session.workspace_id, owner.workspaceId);
  assert.equal(restored.data.thread_id, owner.threadId);
  actual.historyRequestId = restored.request_id;
  actual.restored = await inspectTurn(thread, actual.turnId);
  assert.deepEqual(actual.restored, actual.live);
  await expect(thread.locator(webSelectors.composer)).toBeEnabled();
  assert.deepEqual(actual.pageErrors, []);
  actual.phase = "complete";
  await writeFile(resultPath, JSON.stringify(actual, null, 2));
} catch (error) {
  executionError = error;
  const diagnostics = await Promise.allSettled([
    writeFile(resultPath, JSON.stringify({ ...actual, error: String(error) }, null, 2)),
    ...(page ? [page.screenshot({ path: required("BOXTEAM_BROWSER_SCREENSHOT_PATH"), fullPage: true })] : []),
  ]);
  const failures = diagnostics.filter(entry => entry.status === "rejected").map(entry => entry.reason);
  if (failures.length) executionError = new AggregateError([error, ...failures], "身份验收与诊断保存失败");
  throw executionError;
} finally {
  try {
    await run.close();
  } catch (error) {
    if (executionError) throw new AggregateError([executionError, error], "身份验收与清理失败");
    throw error;
  }
}
