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
// 与 child-thread-delegate cassette 的脚本化序列保持一致。
const confirmPrompt = "请先回复一条确认消息，证明会话可用。";
const sharedFinalText = "收到，会话运行正常，我可以继续执行任务。";
const delegatePrompt = "请委派一个子代理去完成示例任务。";
const delegateDescription = "完成示例任务并输出结果";
const parentFinalText = "子代理任务已完成。";
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
      body: JSON.stringify({ tracking: { source: "child-thread-panel-e2e" } }),
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
    body: JSON.stringify({ title: "Web 子会话线程委派流程", agent_id: "default" }),
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

/** 等待某个用户消息所在 Turn 里的最终文本可见，并返回该 Turn 的最终文本。 */
async function waitForTurnFinalText(page, prompt, finalText) {
  const userMessage = page.locator(".chat-user-text").filter({ hasText: prompt }).last();
  await userMessage.waitFor({ state: "visible", timeout: 60_000 });
  const turn = userMessage.locator("xpath=ancestor::article[contains(@class, 'chat-turn')]");
  const finalMessage = turn.locator(".chat-markdown").filter({ hasText: finalText }).last();
  await finalMessage.waitFor({ state: "visible", timeout: 120_000 });
  return finalMessage.innerText();
}

/** 打开主窗口右侧侧边栏的「运行与连接」标签并返回 ChildThreadPanel 定位器。 */
async function openResourcesTab(page) {
  const resourcesTab = page
    .locator(".workspace-component-tab")
    .filter({ hasText: "运行与连接" });
  if ((await resourcesTab.count()) === 0 || !(await resourcesTab.first().isVisible())) {
    // 右侧侧边栏被折叠时，先用标题栏按钮展开。
    const toggle = page.locator(".titlebar-auxiliary-button");
    await toggle.waitFor({ state: "visible", timeout: 30_000 });
    if ((await toggle.getAttribute("aria-pressed")) !== "true") {
      await toggle.click();
    }
  }
  await resourcesTab.first().waitFor({ state: "visible", timeout: 30_000 });
  await resourcesTab.first().click();
  const panel = page.locator(".child-thread-panel");
  await panel.waitFor({ state: "visible", timeout: 30_000 });
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

const browser = await chromium.launch({ executablePath, headless: true });
const context = await browser.newContext({ viewport: { width: 1920, height: 1100 } });
const page = await context.newPage();
page.streamResponses = [];
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
  confirmTurn: null,
  delegateTurn: null,
  childThread: null,
  navigation: null,
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

  // 第一轮：纯文本确认回复（cassette interaction 0，后续 child 也会复用）。
  const confirmSend = await sendPrompt(page, result.sessionId, confirmPrompt);
  await waitForStreamCount(page.streamResponses, "message", 1);
  const confirmFinalText = await waitForTurnFinalText(page, confirmPrompt, sharedFinalText);
  result.confirmTurn = {
    jobId: confirmSend.jobId,
    finalText: confirmFinalText,
    job: await waitForJobSuccess(page, confirmSend.jobId).then((job) => job?.status ?? null),
  };

  // 第二轮：Agent 调 task 工具真实委派子会话。
  const delegateSend = await sendPrompt(page, result.sessionId, delegatePrompt);
  await waitForStreamCount(page.streamResponses, "message", 2);
  const delegateFinalText = await waitForTurnFinalText(page, delegatePrompt, parentFinalText);
  const delegateJob = await waitForJobSuccess(page, delegateSend.jobId);
  // Turn 完成后 thinking 区默认折叠；先定位本轮 Turn，再展开后读取工具行（对齐基线脚本行为）。
  const delegateTurn = page
    .locator(".chat-user-text")
    .filter({ hasText: delegatePrompt })
    .last()
    .locator("xpath=ancestor::article[contains(@class, 'chat-turn')]");
  const thinkingToggle = delegateTurn.locator(".chat-thinking-toggle").last();
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
  const copyIdLabel = await item
    .locator(".child-thread-copy-id")
    .getAttribute("aria-label");
  assert.match(copyIdLabel ?? "", /^复制 child thread ID: /, "复制按钮缺少 child thread ID 标注");
  const childThreadId = (copyIdLabel ?? "").replace(/^复制 child thread ID: /, "").trim();
  assert.match(title, /^委派：/, "child 项标题必须以「委派：」开头");
  assert.ok(title.includes(delegateDescription), `child 项标题缺少委派描述: ${title}`);
  assert.equal(statusText, "等待启动", "pending admission 的 child 项必须显示「等待启动」");
  assert.ok(metaText.includes("general-purpose"), `child 项元信息缺少 subagent_type: ${metaText}`);
  result.childThread = {
    childThreadId,
    title,
    statusText,
    metaText,
  };

  // 点击 child 项只切换当前 Session 内的 Node Debug owner，不切换聊天会话。
  const debugStateRequest = page.waitForRequest((request) => {
    const url = new URL(request.url());
    return url.pathname === "/api/v1/debug/node"
      && url.searchParams.get("session_id") === result.sessionId
      && url.searchParams.get("thread_id") === childThreadId;
  }, { timeout: 30_000 });
  await item.locator(".child-thread-main").click();
  const request = await debugStateRequest;
  const debugOwner = page.locator(".debug-workbench-header").filter({ hasText: `调试 owner: ${childThreadId}` });
  await debugOwner.waitFor({ state: "visible", timeout: 30_000 });
  result.navigation = {
    selectedThreadId: new URL(request.url()).searchParams.get("thread_id"),
    debugOwnerVisible: true,
  };

  // 持久化证据：父子两端的 LLM 请求日志与后端 child-threads 列表。
  const parentLogs = await api(
    page,
    `/api/v1/sessions/${result.sessionId}/llm-request-logs`,
    { headers: workspaceHeaders },
  );
  const childThreads = await api(
    page,
    `/api/v1/sessions/${result.sessionId}/child-threads`,
    { headers: workspaceHeaders },
  );
  const parentLogItems = Array.isArray(parentLogs?.data) ? parentLogs.data : parentLogs?.data?.items ?? [];
  const rolesOf = (item) => {
    const attempt = item?.upstream?.attempts?.[0];
    return (attempt?.request?.messages ?? []).map((message) => message.role);
  };
  const threadItems = childThreads?.data?.items ?? [];
  assert.equal(threadItems.length, 1, "后端 child-threads 必须返回 1 条委派子会话");
  assert.equal(threadItems[0]?.thread_id, childThreadId, "child-threads 的 thread_id 与面板不一致");
  result.persisted = {
    parentLlmRequestCount: parentLogItems.length,
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
    trace: page.streamResponses.filter((url) => streamKind(url) === "trace").length,
    message: page.streamResponses.filter((url) => streamKind(url) === "message").length,
  };

  assert.equal(result.persisted.parentLlmRequestCount, 3, "parent 必须产生 3 次上游模型请求");
  // 前两次请求形态固定；第三次请求存在产品侧非确定性（见 R7 报告）：
  // 消息列表可能带或不带尾部 tool 结果消息，两种形态都由 cassette 覆盖。
  assert.deepEqual(result.persisted.parentUpstreamMessageRoles[0], ["system", "user"]);
  assert.deepEqual(result.persisted.parentUpstreamMessageRoles[1], [
    "system",
    "user",
    "assistant",
    "user",
  ]);
  assert.ok(
    [
      ["system", "user", "assistant", "user", "assistant", "tool"],
      ["system", "user", "assistant", "user", "assistant"],
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
  await writeFile(resultPath, JSON.stringify(result, null, 2));
} catch (error) {
  await dumpFailureDiagnostics();
  await page.screenshot({ path: screenshotPath, fullPage: true }).catch(() => undefined);
  throw error;
} finally {
  await context.close();
  await browser.close();
}
