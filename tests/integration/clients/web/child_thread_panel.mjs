import assert from "node:assert/strict";
import { writeFile } from "node:fs/promises";
import process from "node:process";
import { chromium } from "playwright";
import { expect } from "playwright/test";
import { TestRunContext } from "../../../harness/js/run-context.mjs";

function requiredEnvironment(name) {
  const value = process.env[name];
  if (!value) throw new Error(`缺少环境变量 ${name}`);
  return value;
}

const baseUrl = requiredEnvironment("BOXTEAM_BROWSER_BASE_URL");
const expectedWorkspaceId = requiredEnvironment("BOXTEAM_BROWSER_WORKSPACE_ID");
const expectedGatewayId = requiredEnvironment("BOXTEAM_BROWSER_GATEWAY_ID");
const scope = TestRunContext.fromTestFile("tests/integration/clients/web/test_child_thread_panel.py");
let parentThread;
let threadOwner;
let browser;
let context;
let page;
const resultPath = requiredEnvironment("BOXTEAM_BROWSER_RESULT_PATH");
const screenshotPath = requiredEnvironment("BOXTEAM_BROWSER_SCREENSHOT_PATH");
// 与 child-thread-delegate cassette 的脚本化序列保持一致。
const confirmPrompt = "请先回复一条确认消息，证明会话可用。";
const sharedFinalText = "收到，会话运行正常，我可以继续执行任务。";
const delegatePrompt = "请委派一个子代理去完成示例任务。";
const delegateDescription = "完成示例任务并输出结果";
const parentFinalText = "子代理任务已完成。";
const childFinalText = "子线程已完成示例任务。";
const reportFinalText = "已收到子线程执行结果。";
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
    async ({ pathname: path, init: requestInit, token: localTokenValue, workspaceId, gatewayId }) => {
      const headers = new Headers(requestInit.headers ?? {});
      headers.set("X-Local-Token", localTokenValue);
      if (path.startsWith("/api/v1/")) {
        headers.set("X-BoxTeam-Workspace-Id", workspaceId);
        headers.set("X-BoxTeam-Target-Gateway-Id", gatewayId);
      }
      const response = await fetch(path, {
        ...requestInit,
        headers,
        credentials: "include",
      });
      const body = await response.text();
      if (!response.ok) throw new Error(`API ${response.status}: ${body}`);
      return body ? JSON.parse(body) : null;
    },
    { pathname, init, token, workspaceId: expectedWorkspaceId, gatewayId: expectedGatewayId },
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
  await page.goto(new URL("/", baseUrl).href, { waitUntil: "domcontentloaded", timeout: 30_000 });
  // 工作台通过自身初始化链取得访客身份，不以整页重载刷新导航树。
  await expect(page.getByRole("button", { name: "刷新资源树", exact: true })).toBeVisible({ timeout: 30_000 });
  await api(page, "/api/gateway/users/current");
}

function isSessionMessageRequest(request) {
  return request.method() === "POST"
    && new URL(request.url()).pathname === `/api/v1/sessions/${threadOwner.session_id}/threads/${threadOwner.thread_id}/messages`;
}

function streamKind(request) {
  const url = request.url();
  if (url.includes("/traces/stream")) return "trace";
  if (url.includes("/message-stream?")) return "message";
  if (new URL(url).pathname === "/api/v1/workspace/observations/stream") {
    const body = request.postDataJSON();
    if (body?.subscriptions?.some((subscription) => subscription.kind === "session_trace")) {
      return "trace";
    }
  }
  return null;
}

function isExpectedBrowserAbort(request) {
  // 页面刷新或重建 SSE 连接时，浏览器会取消尚未完成的 fetch；这不是服务端请求失败。
  return request.failure()?.errorText === "net::ERR_ABORTED";
}

async function waitForStreamCount(streamResponses, kind, expectedCount) {
  await waitUntil(
    async () => streamResponses.filter((request) => streamKind(request) === kind).length >= expectedCount,
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
    body: JSON.stringify({ title: "Web 子会话线程委派流程", agent_id: "default" }),
  });
  const sessionId = payload?.data?.session_id;
  assert.equal(typeof sessionId, "string", "Gateway 新建会话响应缺少 session_id");
  threadOwner = payload.data;
  return sessionId;
}

async function selectSession(page, sessionId, expectedTraceCount) {
  const sessionButton = page.locator(`button[data-session-id="${sessionId}"]`);
  await sessionButton.waitFor({ state: "visible", timeout: 30_000 });
  await sessionButton.dblclick();
  parentThread = page.locator(`section.boxteam-thread[data-workspace-id="${threadOwner.workspace_id}"][data-session-id="${sessionId}"][data-thread-id="${threadOwner.thread_id}"]`);
  await expect(parentThread).toBeVisible();
  await parentThread.locator("#input").waitFor({ state: "visible", timeout: 30_000 });
  await waitForStreamCount(streamResponses, "trace", expectedTraceCount);
}

async function sendPrompt(page, sessionId, prompt) {
  const sendRequestPromise = page.waitForRequest(isSessionMessageRequest, { timeout: 30_000 });
  const sendResponsePromise = page.waitForResponse(
    (response) => isSessionMessageRequest(response.request()) && response.status() === 200,
    { timeout: 30_000 },
  );
  await parentThread.locator("#input").fill(prompt);
  await parentThread.locator("#sendButton").click();
  const [request, response] = await Promise.all([sendRequestPromise, sendResponsePromise]);
  assert.equal(new URL(request.url()).pathname,
    `/api/v1/sessions/${sessionId}/threads/${threadOwner.thread_id}/messages`,
    "Composer 消息请求未发往当前 Thread");
  const payload = request.postDataJSON();
  assert.equal(payload?.message?.content, prompt, "Composer 发送内容不一致");
  assert.equal((await request.allHeaders())["x-boxteam-workspace-id"], expectedWorkspaceId);
  assert.equal(response.status(), 200, "Composer 发送失败");
  const responseBody = await response.json();
  assert.equal(responseBody.data.thread_id, threadOwner.thread_id, "Composer 接纳 Thread 归属不一致");
  const jobId = responseBody?.data?.job_id;
  assert.equal(typeof jobId, "string", "Composer 响应缺少 job_id");
  return { payload, jobId };
}

/** 等待某个用户消息所在 Turn 里的最终文本可见，并返回该 Turn 的最终文本。 */
async function waitForTurnFinalText(page, prompt, finalText) {
  const userMessage = parentThread.locator(".chat-user-text").filter({ hasText: prompt }).last();
  await userMessage.waitFor({ state: "visible", timeout: 60_000 });
  const turn = userMessage.locator("xpath=ancestor::article[contains(@class, 'chat-turn')]");
  const finalMessage = turn.locator(".chat-markdown").filter({ hasText: finalText }).last();
  await finalMessage.waitFor({ state: "visible", timeout: 120_000 });
  return finalMessage.innerText();
}

/** 在精确 Thread 的资源区域进入运行与连接。 */
async function openResourcesTab() {
  const toggle = parentThread.getByRole("button", { name: "切换会话资源", exact: true });
  if (await toggle.getAttribute("aria-expanded") !== "true") await toggle.click();
  await parentThread.getByRole("tab", { name: "运行与连接", exact: true }).click();
  const panel = parentThread.locator(".child-thread-panel");
  await expect(panel).toBeVisible();
  return panel;
}

/** 等待 ChildThreadPanel 渲染出委派 child 项（面板 5s 静默轮询，给足窗口）。 */
async function waitForChildThreadItem(panel) {
  const childItems = panel.locator(".child-thread-item:not(.child-thread-owner-item)");
  const item = childItems.first();
  await waitUntil(async () => (await childItems.count()) >= 1, "子会话线程项出现", 45_000);
  await item.waitFor({ state: "visible", timeout: 30_000 });
  return item;
}

const streamResponses = [];

const pageErrors = [];
const consoleErrors = [];
const failedRequests = [];
let expectedUnauthorizedProbeCount = 0;
/** 失败时把页面关键状态转储到诊断 JSON，供离线排查（不影响正常断言路径）。 */
async function dumpFailureDiagnostics() {
  const diagnosticsPath = resultPath.replace(/-result\.json$/, "-diagnostics.json");
  const [bodyText, userTexts, markdownTexts, chatTurnCount, inputVisible, sessionListHtmlCount] =
    await Promise.all([
      page
        .locator("body")
        .innerText()
        .then((text) => text.slice(0, 3000))
        .catch(() => null),
      page.locator(".chat-user-text").allInnerTexts().catch(() => []),
      page.locator(".chat-markdown").allInnerTexts().catch(() => []),
      page.locator(".chat-turn").count().catch(() => -1),
      page.locator("#input").isVisible().catch(() => false),
      page.locator(".session-list").count().catch(() => -1),
    ]);
  await writeFile(
    diagnosticsPath,
    JSON.stringify(
      {
        url: page.url(),
        bodyText,
        userTexts,
        markdownTexts,
        chatTurnCount,
        inputVisible,
        sessionListCount: sessionListHtmlCount,
        pageErrors,
        consoleErrors,
        failedRequests,
      },
      null,
      2,
    ),
  ).catch(() => undefined);
}
const result = {
  classification: "Integration",
  workspaceId: expectedWorkspaceId,
  sessionId: null,
  confirmTurn: null,
  delegateTurn: null,
  childThread: null,
  navigation: null,
  persisted: null,
  streams: { trace: 0, message: 0 },
  diagnostics: { pageErrors, consoleErrors, failedRequests },
};

let failure;
try {
  browser = await chromium.launch({ executablePath, headless: true });
  scope.addCleanup("子 Thread Chromium", () => browser.close());
  context = await browser.newContext({ viewport: { width: 1920, height: 1100 } });
  scope.addCleanup("子 Thread context", () => context.close());
  page = await context.newPage();
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
    if (response.status() === 200 && streamKind(response.request())) {
      streamResponses.push(response.request());
    }
  });

  await ensureGuest(page);
  const workspaces = await api(page, "/api/gateway/workspaces");
  assert.deepEqual(workspaces?.data?.active_workspace,
    { gateway_id: expectedGatewayId, workspace_id: expectedWorkspaceId }, "active workspace 不一致");

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

  await page.getByRole("button", { name: "刷新资源树", exact: true }).click();
  await selectSession(page, result.sessionId, 1);

  // 第一轮：纯文本确认回复；child 使用独立的冻结 seed 请求及响应。
  const confirmSend = await sendPrompt(page, result.sessionId, confirmPrompt);
  await waitForStreamCount(streamResponses, "message", 1);
  const confirmFinalText = await waitForTurnFinalText(page, confirmPrompt, sharedFinalText);
  result.confirmTurn = {
    jobId: confirmSend.jobId,
    finalText: confirmFinalText,
    job: await waitForJobSuccess(page, confirmSend.jobId).then((job) => job?.status ?? null),
  };

  // 第二轮：Agent 调 task 工具真实委派子会话。
  const delegateSend = await sendPrompt(page, result.sessionId, delegatePrompt);
  await waitForStreamCount(streamResponses, "message", 2);
  const delegateFinalText = await waitForTurnFinalText(page, delegatePrompt, parentFinalText);
  const delegateJob = await waitForJobSuccess(page, delegateSend.jobId);
  // Turn 完成后 thinking 区默认折叠；先定位本轮 Turn，再展开后读取工具行（对齐基线脚本行为）。
  const delegateTurn = parentThread
    .locator(".chat-user-text")
    .filter({ hasText: delegatePrompt })
    .last()
    .locator("xpath=ancestor::article[contains(@class, 'chat-turn')]");
  const thinkingToggle = delegateTurn.locator(".chat-turn-activity > button.chat-thinking-toggle");
  await thinkingToggle.waitFor({ state: "visible", timeout: 30_000 });
  if ((await thinkingToggle.getAttribute("aria-expanded")) !== "true") {
    await thinkingToggle.click();
  }
  await waitUntil(
    async () => (await thinkingToggle.getAttribute("aria-expanded")) === "true",
    "委派 Turn 中间 Item 展开",
    30_000,
  );
  const completedTaskToolRow = delegateTurn
    .locator(".chat-tool-row.is-complete")
    .filter({ hasText: "已运行 task" })
    .first();
  await completedTaskToolRow.waitFor({ state: "visible", timeout: 30_000 });
  result.delegateTurn = {
    jobId: delegateSend.jobId,
    finalText: delegateFinalText,
    jobStatus: delegateJob?.status ?? null,
    toolText: await completedTaskToolRow.innerText(),
  };

  // 打开右侧侧边栏「运行与连接」标签，等待子会话线程面板渲染 child 项。
  const panel = await openResourcesTab(page);
  const item = await waitForChildThreadItem(panel);
  const title = (await item.locator(".child-thread-copy strong").innerText()).trim();
  const statusText = (await item.locator(".child-thread-status").innerText()).trim();
  const metaText = (await item.locator(".child-thread-copy small").innerText()).trim();
  const childThreadIdCopyLabel = await item
    .locator('button[aria-label^="复制 child thread ID:"]')
    .getAttribute("aria-label");
  assert.match(childThreadIdCopyLabel ?? "", /^复制 child thread ID: /, "复制按钮缺少 child thread ID 标注");
  const childThreadId = (childThreadIdCopyLabel ?? "").replace(/^复制 child thread ID: /, "").trim();
  assert.match(title, /^委派：/, "child 项标题必须以「委派：」开头");
  assert.ok(title.includes(delegateDescription), `child 项标题缺少委派描述: ${title}`);
  assert.equal(statusText, "运行中", "已启动的 child 项必须显示「运行中」");
  assert.ok(metaText.includes("general-purpose"), `child 项元信息缺少 subagent_type: ${metaText}`);
  result.childThread = {
    childThreadId,
    title,
    statusText,
    metaText,
  };

  // 调试入口打开精确 child 编辑器，并展开其调试资源。
  const debugStateRequest = page.waitForRequest((request) => {
    const url = new URL(request.url());
    return url.pathname === "/api/v1/debug/node"
      && url.searchParams.get("session_id") === result.sessionId
      && url.searchParams.get("thread_id") === childThreadId;
  }, { timeout: 30_000 });
  await item.locator('button[aria-label^="调试子线程："]').click();
  const request = await debugStateRequest;
  const childThread = page.locator(`section.boxteam-thread[data-workspace-id="${threadOwner.workspace_id}"][data-session-id="${result.sessionId}"][data-thread-id="${childThreadId}"]`);
  await expect(childThread).toBeVisible();
  await expect(childThread.getByRole("tab", { name: "调试", exact: true })).toHaveAttribute("aria-selected", "true");
  const debugOwner = childThread.locator(".debug-workbench-header").filter({ hasText: `调试 owner: ${childThreadId}` });
  await debugOwner.waitFor({ state: "visible", timeout: 30_000 });
  result.navigation = {
    selectedThreadId: new URL(request.url()).searchParams.get("thread_id"),
    debugOwnerVisible: true,
  };
  // 再次打开已有 child 标签时，仍应选中调试，不能丢弃此次导航意图。
  await childThread.getByRole("tab", { name: "文件", exact: true }).click();
  await selectSession(page, result.sessionId, 1);
  const reopenedPanel = await openResourcesTab();
  await reopenedPanel.getByRole("button", { name: `调试子线程：${childThreadId}`, exact: true }).click();
  await expect(childThread.getByRole("tab", { name: "调试", exact: true })).toHaveAttribute("aria-selected", "true");
  await expect(debugOwner).toBeVisible();
  await expect(childThread).toHaveCount(1);
  result.navigation.reusedTabDebugVisible = true;

  // 持久化证据：父子两端的 LLM 请求日志与后端 child-threads 列表。
  await selectSession(page, result.sessionId, 1);
  await expect(parentThread.getByRole("article", { name: "会话生成", exact: true }))
    .toHaveText("会话生成委派任务已结束，正在处理返回结果。");
  let sessionLogItems;
  await waitUntil(async () => {
    const logs = await api(page, `/api/v1/sessions/${result.sessionId}/llm-request-logs`, { headers: workspaceHeaders });
    sessionLogItems = Array.isArray(logs.data) ? logs.data : logs.data.items;
    return sessionLogItems.length >= 5;
  }, "冻结任务、原父调用及自动回报的模型日志", 60_000);
  const childThreads = await api(
    page,
    `/api/v1/sessions/${result.sessionId}/child-threads`,
    { headers: workspaceHeaders },
  );
  const parentJobIds = new Set([confirmSend.jobId, delegateSend.jobId]);
  const parentLogItems = sessionLogItems.filter((item) => parentJobIds.has(item?.job_id));
  const otherLogs = sessionLogItems.filter((item) => !parentJobIds.has(item?.job_id));
  const jobs = await Promise.all(otherLogs.map(item => waitForJobSuccess(page, item.job_id)));
  const childLogs = otherLogs.filter((_item, index) => jobs[index].thread_id === childThreadId);
  const reportLogs = otherLogs.filter((_item, index) => jobs[index].thread_id === threadOwner.thread_id);
  assert.equal(childLogs.length, 1, "冻结委派输入必须只执行一次");
  assert.equal(reportLogs.length, 1, "子线程结果必须只自动回报父 Thread 一次");
  for (const job of jobs) {
    assert.equal(job.session_id, result.sessionId);
    assert.equal(job.status, "completed");
  }
  const childMessages = childLogs[0].upstream.attempts[0].request.messages;
  assert.deepEqual(childMessages.map(message => message.role), ["system", "user"]);
  const childSources = JSON.parse(childMessages[1].content);
  assert.equal(childSources.protocol, "boxteam-context-sources:v2");
  const seeds = childSources.sources.filter(source => source.source_kind === "thread_task_seed");
  assert.equal(seeds.length, 1, "子线程模型请求必须包含唯一冻结任务来源");
  assert.equal(seeds[0].root_placement, "tail_only");
  assert.ok(seeds[0].body.includes("<delegated_task"));
  assert.ok(seeds[0].body.includes(delegateDescription));
  assert.ok(seeds[0].body.includes(threadOwner.thread_id));
  const reportMessages = reportLogs[0].upstream.attempts[0].request.messages;
  const reportInput = reportMessages.at(-1);
  assert.equal(reportInput.role, "user");
  assert.ok(reportInput.content.includes("<generated_session_result"));
  assert.ok(reportInput.content.includes(childFinalText));
  assert.equal(reportLogs[0].upstream.attempts[0].response.choices[0].message.content, reportFinalText);
  // 回报使用安全文案；子线程的私有结果正文不能当作父 Thread 的用户输入显示。
  await expect(parentThread.locator(".chat-user-text").filter({ hasText: childFinalText })).toHaveCount(0);
  await expect(parentThread.locator(".chat-internal-execution-display")).not.toContainText(childFinalText);
  const reportsPanel = await openResourcesTab();
  await reportsPanel.locator(`.child-thread-item[data-thread-id="${childThreadId}"] .child-thread-report-link`).click();
  const report = reportsPanel.locator(`.child-thread-conversation[data-thread-id="${childThreadId}"] .child-thread-report`);
  await expect(report.getByText(childFinalText, { exact: true })).toBeVisible();
  const rolesOf = (item) => {
    const attempt = item?.upstream?.attempts?.[0];
    return (attempt?.request?.messages ?? []).map((message) => message.role);
  };
  const threadItems = childThreads?.data?.items ?? [];
  assert.equal(threadItems.length, 1, "后端 child-threads 必须返回 1 条委派子会话");
  assert.equal(threadItems[0]?.thread_id, childThreadId, "child-threads 的 thread_id 与面板不一致");
  result.persisted = {
    sessionLlmRequestCount: sessionLogItems.length,
    parentLlmRequestCount: parentLogItems.length,
    childLlmRequestCount: childLogs.length,
    reportLlmRequestCount: reportLogs.length,
    childSeed: { sourceId: seeds[0].source_identity, task: delegateDescription, parentThreadId: threadOwner.thread_id },
    report: { jobId: reportLogs[0].job_id, childThreadId, finalText: reportFinalText },
    parentUpstreamMessageRoles: parentLogItems.map(rolesOf),
    childThreads: threadItems.map((thread) => ({
      thread_id: thread.thread_id,
      title: thread.title,
      admission_state: thread.admission_state,
      collaboration_state: thread.collaboration_state,
      subagent_type: thread.subagent_type,
    })),
  };
  result.streams = {
    trace: streamResponses.filter((request) => streamKind(request) === "trace").length,
    message: streamResponses.filter((request) => streamKind(request) === "message").length,
  };

  await writeFile(resultPath, JSON.stringify(result, null, 2));
  assert.equal(result.persisted.sessionLlmRequestCount, 5, "三次父调用、一次子调用与一次自动回报必须共 5 次");
  assert.equal(result.persisted.parentLlmRequestCount, 3, "两个父会话 Job 必须产生 3 次上游模型请求");
  assert.equal(result.persisted.childLlmRequestCount, 1, "委派子线程必须产生 1 次上游模型请求");
  // 此最小工作区仅登记一条 AGENTS 来源；工具集使用独立 tools 字段。
  const registeredContextRoles = ["user"];
  const requestRolePrefix = ["system", "user", ...registeredContextRoles];
  assert.deepEqual(result.persisted.parentUpstreamMessageRoles[0], requestRolePrefix);
  assert.deepEqual(result.persisted.parentUpstreamMessageRoles[1], [
    ...requestRolePrefix,
    "assistant",
    "user",
  ]);
  assert.ok(
    [
      [...requestRolePrefix, "assistant", "user", "assistant", "tool"],
      [...requestRolePrefix, "assistant", "user", "assistant"],
    ].some(
      (shape) =>
        JSON.stringify(shape) === JSON.stringify(result.persisted.parentUpstreamMessageRoles[2]),
    ),
    `parent 第三次请求角色形态不在预期集合内: ${JSON.stringify(result.persisted.parentUpstreamMessageRoles[2])}`,
  );
  assert.ok(result.streams.message >= 2, "两轮对话必须各自建立 message SSE 连接");
  assert.deepEqual(pageErrors, [], "浏览器 pageerror 出现");
  assert.deepEqual(consoleErrors, [], "浏览器 console error 出现");
  assert.deepEqual(failedRequests, [], "浏览器请求失败");
} catch (error) {
  failure = error;
} finally {
  const errors = failure ? [failure] : [];
  if (failure && page) {
    try {
      await dumpFailureDiagnostics();
      await page.screenshot({ path: screenshotPath, fullPage: true });
    } catch (error) { errors.push(error); }
  }
  try { await scope.close(); } catch (error) { errors.push(error); }
  if (errors.length) throw new AggregateError(errors, "子 Thread 工作台验收失败");
}
