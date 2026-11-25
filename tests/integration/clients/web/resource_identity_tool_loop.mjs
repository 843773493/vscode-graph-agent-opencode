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
  const expand = turn.locator(".chat-turn-activity > .chat-thinking-toggle");
  await expect(expand).toBeVisible();
  // 正文可先于终态到达；等真实终态后再展开，避免误点仍自动展开的live过程。
  await expect(expand.locator(":scope > .codicon-check")).toBeVisible();
  if (await expand.getAttribute("aria-expanded") !== "true") await expand.click();
  await expect(expand).toHaveAttribute("aria-expanded", "true");
  await expect(turn.locator(".chat-activity-group").first()).toBeVisible();
  for (const group of await turn.locator('.chat-activity-group > button').all()) {
    if (await group.getAttribute('aria-expanded') === 'false') await group.click();
  }
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
  const parts = detail.response_items.filter(part => ["tool_call", "tool_result"].includes(part.kind));
  // request／runtime载体保留各自原ID；调用身份由ModelCall及Provider原ID共同确定。
  const callIdentity = part => {
    const model = part.source.model_call_id;
    const prefix = `${model}:tool-call:`;
    const id = part.tool_call_id;
    assert(typeof id === "string" && model);
    return [part.kind, model, id.startsWith(prefix) ? id.slice(prefix.length) : id];
  };
  assert.deepEqual(parts.map(callIdentity), [
    ...["tool_call", "tool_result"].flatMap(kind => [0, 1, 2].map(index => [kind, modelCalls[0], `rejected-${index}`])),
    ["tool_call", modelCalls[1], "corrected-0"], ["tool_result", modelCalls[1], "corrected-0"],
  ]);
  const rows = turn.locator(".chat-tool-row");
  // 每个结果按精确ModelCall／调用身份合入原声明行，保留独立Item来源。
  await expect(rows).toHaveCount(4);
  await expect(turn.locator(".chat-tool-row.is-failed")).toHaveCount(3);
  await expect(turn.locator(".chat-tool-row.is-complete")).toHaveCount(1);
  const results = [];
  const sources = [];
  const callParts = parts.filter(part => part.kind === "tool_call");
  for (let index = 0; index < 4; index++) {
    const row = rows.nth(index);
    const references = JSON.parse(await row.getAttribute("data-response-sources"));
    assert.deepEqual(references.map(callIdentity), [
      callIdentity(callParts[index]), ["tool_result", ...callIdentity(callParts[index]).slice(1)],
    ]);
    sources.push(...references);
    const summary = row.locator(".chat-tool-summary");
    if (await summary.getAttribute("aria-expanded") !== "true") await summary.click();
    const input = row.locator(".chat-tool-details pre code").first();
    await expect(input).toBeVisible();
    // 原调用参数逐字保留其目标；错误输出与原生路径输入分别验收。
    assert.deepEqual(JSON.parse(await input.innerText()), JSON.parse(callParts[index].arguments));
    const output = row.locator(".chat-tool-details pre code").last();
    await expect(output).toBeVisible();
    if (index < 3) {
      await expect(output).toHaveText(safeError);
      results.push((await output.innerText()).trim());
    } else {
      assert.equal(JSON.parse(await output.innerText()).scope, "workspace");
    }
  }
  assert(sources.every(part => typeof part.id === "string" && part.id === part.source.item_id));
  const byId = (left, right) => left.id.localeCompare(right.id);
  assert.deepEqual(sources.map(({ id, kind, source }) => ({ id, kind, source })).sort(byId),
    parts.map(({ id, kind, source }) => ({ id, kind, source })).sort(byId));
  return { failed: 3, completed: 1, results, itemIds: sources.map(part => part.id) };
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
