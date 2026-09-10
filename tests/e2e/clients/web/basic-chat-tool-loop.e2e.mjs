import assert from "node:assert/strict";
import { writeFile } from "node:fs/promises";
import process from "node:process";
import { chromium } from "playwright";

function requiredEnvironment(name) {
  const value = process.env[name];
  if (!value) throw new Error(`缺少环境变量 ${name}`);
  return value;
}

const baseUrl = requiredEnvironment("BOXTEAM_BROWSER_BASE_URL");
const expectedWorkspaceId = requiredEnvironment("BOXTEAM_BROWSER_WORKSPACE_ID");
const resultPath = requiredEnvironment("BOXTEAM_BROWSER_RESULT_PATH");
const screenshotPath = requiredEnvironment("BOXTEAM_BROWSER_SCREENSHOT_PATH");
const firstPrompt = "请读取 README.md，并确认首轮工具调用已完成。";
const firstFinalText = "首轮工具调用已完成。";
const secondPrompt = "请再次读取 README.md，确认历史上下文仍然可用。";
const secondFinalText = "第二轮工具调用也已完成。";
const executablePath = process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH || undefined;

async function localToken(page) {
  return page.evaluate(async () => {
    const response = await fetch("/api/gateway/auth/local-credential");
    if (!response.ok) throw new Error(`获取本地凭据失败: HTTP ${response.status}`);
    const payload = await response.json();
    if (typeof payload?.data?.token !== "string") throw new Error("本地凭据缺少 token");
    return payload.data.token;
  });
}

async function api(page, pathname, init = {}) {
  const token = await localToken(page);
  return page.evaluate(
    async ({ pathname: path, init: requestInit, token: localTokenValue }) => {
      const headers = new Headers(requestInit.headers ?? {});
      headers.set("X-Local-Token", localTokenValue);
      const response = await fetch(path, {
        ...requestInit,
        headers,
        credentials: "include",
      });
      const body = await response.text();
      if (!response.ok) throw new Error(`API ${response.status}: ${body}`);
      return body ? JSON.parse(body) : null;
    },
    { pathname, init, token },
  );
}

async function waitUntil(predicate, label, timeout = 30_000) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    if (await predicate()) return;
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  throw new Error(`等待${label}超时`);
}

async function ensureGuest(page) {
  await page.goto(baseUrl, { waitUntil: "domcontentloaded", timeout: 30_000 });
  const current = await page.evaluate(async () => {
    const response = await fetch("/api/gateway/users/current");
    return { status: response.status, body: await response.text() };
  });
  if (current.status === 401) {
    await api(page, "/api/gateway/users/guest", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ tracking: { source: "basic-chat-tool-loop-e2e" } }),
    });
    await page.reload({ waitUntil: "domcontentloaded" });
  } else if (current.status !== 200) {
    throw new Error(`检查游客访问失败: ${current.status} ${current.body}`);
  }
}

function isSessionMessageRequest(request) {
  return request.method() === "POST"
    && /\/api\/v1\/sessions\/[^/]+\/messages$/.test(new URL(request.url()).pathname);
}

function streamKind(url) {
  if (url.includes("/traces/stream")) return "trace";
  if (url.includes("/message-stream?")) return "message";
  return null;
}

function isExpectedBrowserAbort(request) {
  // 页面刷新或重建 SSE 连接时，浏览器会取消尚未完成的 fetch；这不是服务端请求失败。
  return request.failure()?.errorText === "net::ERR_ABORTED";
}

async function waitForStreamCount(streamResponses, kind, expectedCount) {
  await waitUntil(
    async () => streamResponses.filter((url) => streamKind(url) === kind).length >= expectedCount,
    `${kind} SSE 第 ${expectedCount} 次连接`,
    60_000,
  );
}

async function waitForJobSuccess(page, jobId) {
  let latestJob = null;
  await waitUntil(
    async () => {
      const payload = await api(page, `/api/v1/jobs/${jobId}`);
      latestJob = payload?.data ?? null;
      if (["failed", "cancelled", "timed_out"].includes(latestJob?.status)) {
        throw new Error(`Job 执行失败: ${JSON.stringify(latestJob)}`);
      }
      return ["completed", "succeeded"].includes(latestJob?.status);
    },
    `Job ${jobId} 成功结束`,
    120_000,
  );
  return latestJob;
}

async function createSessionThroughGateway(page, workspaceId) {
  const payload = await api(page, "/api/v1/sessions", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-BoxTeam-Workspace-Id": workspaceId,
    },
    body: JSON.stringify({ title: "Web 工具循环完整流程", agent_id: "default" }),
  });
  const sessionId = payload?.data?.session_id;
  assert.equal(typeof sessionId, "string", "Gateway 新建会话响应缺少 session_id");
  return sessionId;
}

async function selectSession(page, sessionId, expectedTraceCount) {
  const sessionButton = page.locator(`button[data-session-id="${sessionId}"]`);
  await sessionButton.waitFor({ state: "visible", timeout: 30_000 });
  await sessionButton.click();
  await page.locator("#input").waitFor({ state: "visible", timeout: 30_000 });
  await waitForStreamCount(page.streamResponses, "trace", expectedTraceCount);
}

async function sendPrompt(page, sessionId, prompt) {
  const sendRequestPromise = page.waitForRequest(isSessionMessageRequest, { timeout: 30_000 });
  const sendResponsePromise = page.waitForResponse(
    (response) => isSessionMessageRequest(response.request()) && response.status() === 200,
    { timeout: 30_000 },
  );
  await page.locator("#input").fill(prompt);
  await page.locator("#sendButton").click();
  const [request, response] = await Promise.all([sendRequestPromise, sendResponsePromise]);
  assert.match(
    new URL(request.url()).pathname,
    new RegExp(`/api/v1/sessions/${sessionId}/messages$`),
    "Composer 消息请求未发往当前 Session",
  );
  const payload = request.postDataJSON();
  assert.equal(payload?.message?.content, prompt, "Composer 发送内容不一致");
  assert.equal(response.status(), 200, "Composer 发送失败");
  const responseBody = await response.json();
  const jobId = responseBody?.data?.job_id;
  assert.equal(typeof jobId, "string", "Composer 响应缺少 job_id");
  return { payload, jobId };
}

function expectedActivityPreview(durationMs, itemCount) {
  assert.ok(Number.isInteger(durationMs) && durationMs > 0, "Turn duration_ms 必须是正整数");
  assert.ok(Number.isInteger(itemCount) && itemCount > 0, "Turn item_count 必须是正整数");
  const duration = durationMs < 1000
    ? `${durationMs}ms`
    : `${(durationMs / 1000).toFixed(durationMs >= 10_000 ? 0 : 1)}s`;
  return `耗时 ${duration} · Item ${itemCount} 项`;
}

async function waitForCompletedTurn(page, prompt, finalText, expectedReasoning) {
  const userMessage = page.locator(".chat-user-text").filter({ hasText: prompt }).last();
  await userMessage.waitFor({ state: "visible", timeout: 60_000 });
  const turn = userMessage.locator("xpath=ancestor::article[contains(@class, 'chat-turn')]");
  const finalMessage = turn.locator(".chat-markdown").filter({ hasText: finalText }).last();
  await finalMessage.waitFor({ state: "visible", timeout: 120_000 });
  await page.waitForFunction(
    () => document.querySelectorAll("#interruptButton, .chat-thinking.is-active").length === 0,
    undefined,
    { timeout: 30_000 },
  );
  const thinking = turn.locator(".chat-thinking").last();
  await thinking.waitFor({ state: "visible", timeout: 30_000 });
  const thinkingToggle = thinking.locator(".chat-thinking-toggle");
  await waitUntil(
    async () => thinking.getAttribute("data-item-count").then((value) => value !== null),
    "terminal 历史投影提供 Item 统计",
    30_000,
  );
  const durationMs = Number(await thinking.getAttribute("data-duration-ms"));
  const itemCount = Number(await thinking.getAttribute("data-item-count"));
  const activityPreview = expectedActivityPreview(durationMs, itemCount);
  const activityPreviewElement = thinking.locator(".chat-thinking-preview");
  await activityPreviewElement.waitFor({ state: "visible", timeout: 30_000 });
  const originalViewport = page.viewportSize();
  await page.setViewportSize({ width: 640, height: 900 });
  assert.equal(
    await activityPreviewElement.isVisible(),
    true,
    "窄窗口也必须显示耗时和 Item 数量",
  );
  if (originalViewport) await page.setViewportSize(originalViewport);
  assert.equal(
    await activityPreviewElement.innerText(),
    activityPreview,
    "可见标题必须精确匹配 Turn duration_ms 和中间 item_count",
  );
  assert.equal(itemCount, 4, "固定工具循环每轮必须投影 2 个 reasoning、1 个 tool call 和 1 个 tool result");
  assert.equal(
    await thinkingToggle.getAttribute("aria-label"),
    `展开 Turn 中间消息：${activityPreview}`,
    "折叠按钮的可访问名称必须包含精确耗时和 Item 数量",
  );
  assert.equal(await thinkingToggle.getAttribute("aria-expanded"), "false");
  await thinkingToggle.click();
  await waitUntil(
    async () => await thinkingToggle.getAttribute("aria-expanded") === "true",
    "Turn 中间 Item 展开",
    30_000,
  );
  await activityPreviewElement.waitFor({ state: "visible", timeout: 30_000 });
  assert.equal(await activityPreviewElement.innerText(), activityPreview);
  const completedTool = thinking.locator(".chat-tool-row.is-complete").filter({ hasText: "已运行 read_file" });
  await completedTool.waitFor({ state: "visible", timeout: 30_000 });
  const activityOrder = await thinking.locator(".chat-thinking-body").evaluate((body) =>
    [...body.children]
      .filter((element) => element.matches(".chat-markdown, .chat-tool-row"))
      .map((element) => element.textContent?.trim() ?? ""),
  );
  assert.equal(activityOrder.length, 3, "展开后应为 reasoning → 聚合工具 → reasoning 三个展示块");
  assert.match(activityOrder[0], new RegExp(expectedReasoning[0]));
  assert.match(activityOrder[1], /已运行 read_file/);
  assert.match(activityOrder[2], new RegExp(expectedReasoning[1]));
  assert.equal(
    activityOrder.filter((text) => text.includes(expectedReasoning[1])).length,
    1,
    "工具后的 reasoning summary 不得重复投影",
  );
  assert.equal(
    await turn.locator(".chat-tool-row.is-failed, .chat-tool-row.is-unknown, .chat-tool-row.is-incomplete").count(),
    0,
    "工具调用出现失败、未知或未完成状态",
  );
  return {
    activityPreview,
    durationMs,
    itemCount,
    activityOrder,
    expanded: await thinkingToggle.getAttribute("aria-expanded") === "true",
    finalText: await finalMessage.innerText(),
    toolText: await completedTool.innerText(),
    userCount: await page.locator(".chat-user-text").filter({ hasText: prompt }).count(),
  };
}

async function loadPersistedEvidence(page, sessionId, workspaceId) {
  const headers = { "X-BoxTeam-Workspace-Id": workspaceId };
  const messages = await api(page, `/api/v1/sessions/${sessionId}/messages`, { headers });
  const logs = await api(page, `/api/v1/sessions/${sessionId}/llm-request-logs`, { headers });
  const agentState = await api(page, `/api/v1/sessions/${sessionId}/agent-state/messages`, { headers });
  const history = await api(page, `/api/v1/sessions/${sessionId}/history`, {
    method: "POST",
    headers: { ...headers, "Content-Type": "application/json" },
    body: JSON.stringify({
      direction: "tail",
      turns: 2,
      include: ["user", "reasoning_detail", "tool_call", "tool_result", "final_response"],
    }),
  });
  const items = messages?.data?.items ?? [];
  const logItems = Array.isArray(logs?.data) ? logs.data : logs?.data?.items ?? [];
  const historyTurns = history?.data?.items ?? [];
  const canonicalActivity = historyTurns.map((turn) => {
    const parts = (turn.response_parts ?? []).filter((part) => part.kind !== "final_text");
    assert.equal(parts.length, turn.activity_stats?.item_count, "history Item 数量与 response_parts 不一致");
    assert.ok(parts.every((part) => typeof part.source?.item_id === "string"));
    assert.ok(parts.every((part) => Number.isInteger(part.source?.item_sequence)));
    assert.ok(parts.every((part) => Number.isInteger(part.source?.part_ordinal)));
    assert.ok(parts.every((part) => typeof part.source?.created_at === "string"));
    assert.ok(parts.every((part) => Number.isInteger(part.source?.elapsed_ms)));
    const coordinates = parts.map((part) => [
      part.source.item_sequence,
      part.source.part_ordinal,
    ]);
    assert.deepEqual(
      coordinates,
      [...coordinates].sort((left, right) => left[0] - right[0] || left[1] - right[1]),
      "history response_parts 未使用后端 canonical 顺序",
    );
    return {
      turnId: turn.turn_id,
      itemCount: turn.activity_stats.item_count,
      firstItemSequence: turn.activity_stats.first_item_sequence,
      lastItemSequence: turn.activity_stats.last_item_sequence,
      coordinates,
    };
  });
  const upstreamAttempts = logItems
    .map((item) => item?.upstream?.attempts?.[0])
    .filter((attempt) => attempt);
  return {
    messageCount: items.length,
    userMessages: items.filter((item) => item.role === "user").map((item) => item.content),
    assistantMessages: items.filter((item) => item.role === "assistant").map((item) => item.content),
    llmRequestCount: logItems.length,
    upstreamMessageRoles: upstreamAttempts.map((attempt) =>
      (attempt.request?.messages ?? []).map((message) => message.role)),
    agentStateJsonl: agentState?.data?.jsonl ?? "",
    canonicalActivity,
  };
}

const browser = await chromium.launch({ executablePath, headless: true });
const context = await browser.newContext({ viewport: { width: 1920, height: 1100 } });
const page = await context.newPage();
page.streamResponses = [];
const pageErrors = [];
const consoleErrors = [];
const failedRequests = [];
let expectedUnauthorizedProbeCount = 0;
page.on("pageerror", (error) => pageErrors.push(String(error)));
page.on("console", (message) => {
  if (message.type() !== "error") return;
  const text = message.text();
  if (
    expectedUnauthorizedProbeCount > 0
    && text === "Failed to load resource: the server responded with a status of 401 (Unauthorized)"
  ) {
    expectedUnauthorizedProbeCount -= 1;
    return;
  }
  consoleErrors.push(text);
});
page.on("request", (request) => {
  if (
    request.method() === "GET"
    && new URL(request.url()).pathname === "/api/gateway/users/current"
  ) {
    expectedUnauthorizedProbeCount += 1;
  }
});
page.on("requestfailed", (request) => {
  if (isExpectedBrowserAbort(request)) return;
  failedRequests.push({ url: request.url(), error: request.failure()?.errorText || "unknown" });
});
page.on("response", (response) => {
  if (
    response.status() !== 401
    && new URL(response.url()).pathname === "/api/gateway/users/current"
    && expectedUnauthorizedProbeCount > 0
  ) {
    expectedUnauthorizedProbeCount -= 1;
  }
  if (response.status() === 200 && streamKind(response.url())) page.streamResponses.push(response.url());
});

const result = {
  workspaceId: expectedWorkspaceId,
  sessionId: null,
  firstTurn: null,
  restoredHistory: null,
  secondTurn: null,
  persisted: null,
  streams: { trace: 0, message: 0 },
  diagnostics: { pageErrors, consoleErrors, failedRequests },
};

try {
  await ensureGuest(page);
  const workspaces = await api(page, "/api/gateway/workspaces");
  assert.equal(workspaces?.data?.active_workspace_id, expectedWorkspaceId, "active workspace 不一致");

  result.sessionId = await createSessionThroughGateway(page, expectedWorkspaceId);
  const workspaceHeaders = {
    "Content-Type": "application/json",
    "X-BoxTeam-Workspace-Id": expectedWorkspaceId,
  };
  const updated = await api(page, `/api/v1/sessions/${result.sessionId}`, {
    method: "PATCH",
    headers: workspaceHeaders,
    body: JSON.stringify({ provider_id: "primary" }),
  });
  assert.equal(updated?.data?.current_provider_id, "primary", "Session 没有切换到 primary provider");

  await page.reload({ waitUntil: "domcontentloaded" });
  await selectSession(page, result.sessionId, 1);
  const firstSend = await sendPrompt(page, result.sessionId, firstPrompt);
  await waitForStreamCount(page.streamResponses, "message", 1);
  result.firstTurn = await waitForCompletedTurn(
    page,
    firstPrompt,
    firstFinalText,
    ["先读取 README.md", "工具已经返回，我先核对 README.md"],
  );
  result.firstJob = await waitForJobSuccess(page, firstSend.jobId);

  await page.reload({ waitUntil: "domcontentloaded" });
  await selectSession(page, result.sessionId, 2);
  result.restoredHistory = await waitForCompletedTurn(
    page,
    firstPrompt,
    firstFinalText,
    ["先读取 README.md", "工具已经返回，我先核对 README.md"],
  );

  const secondSend = await sendPrompt(page, result.sessionId, secondPrompt);
  await waitForStreamCount(page.streamResponses, "message", 2);
  result.secondTurn = await waitForCompletedTurn(
    page,
    secondPrompt,
    secondFinalText,
    ["复用历史上下文", "工具已经返回，我先确认历史上下文"],
  );
  result.secondJob = await waitForJobSuccess(page, secondSend.jobId);
  result.persisted = await loadPersistedEvidence(page, result.sessionId, expectedWorkspaceId);
  result.streams = {
    trace: page.streamResponses.filter((url) => streamKind(url) === "trace").length,
    message: page.streamResponses.filter((url) => streamKind(url) === "message").length,
  };

  assert.equal(result.persisted.llmRequestCount, 4, "两轮工具循环没有产生四次上游模型请求");
  assert.deepEqual(
    result.persisted.canonicalActivity.map((turn) => turn.itemCount),
    [4, 4],
    "两轮 history 均应由后端投影四个逻辑 Item",
  );
  assert.deepEqual(result.persisted.userMessages, [firstPrompt, secondPrompt]);
  assert.equal(result.persisted.assistantMessages.at(-1), secondFinalText);
  assert.deepEqual(result.persisted.upstreamMessageRoles, [
    ["system", "user"],
    ["system", "user", "assistant", "tool"],
    ["system", "user", "assistant", "tool", "assistant", "user"],
    ["system", "user", "assistant", "tool", "assistant", "user", "assistant", "tool"],
  ]);
  assert.match(result.persisted.agentStateJsonl, /先读取 README\.md/);
  assert.deepEqual(pageErrors, [], "浏览器 pageerror 出现");
  assert.deepEqual(consoleErrors, [], "浏览器 console error 出现");
  assert.deepEqual(failedRequests, [], "浏览器请求失败");
  await writeFile(resultPath, JSON.stringify(result, null, 2));
} catch (error) {
  await page.screenshot({ path: screenshotPath, fullPage: true }).catch(() => undefined);
  throw error;
} finally {
  await context.close();
  await browser.close();
}
