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
const userMessage = "请读取 README.md，然后告诉我工具调用是否完成。";
const expectedFinalText = "浏览器 SSE 工具调用已完成。";
const executablePath =
  process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH || undefined;

async function localToken(page) {
  return page.evaluate(async () => {
    const response = await fetch("/api/gateway/auth/local-credential");
    if (!response.ok) {
      throw new Error(`获取本地凭据失败: HTTP ${response.status}`);
    }
    const payload = await response.json();
    if (typeof payload?.data?.token !== "string") {
      throw new Error("本地凭据缺少 token");
    }
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
      body: JSON.stringify({ tracking: { source: "basic-chat-tool-loop" } }),
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

const browser = await chromium.launch({ executablePath, headless: true });
const context = await browser.newContext({
  viewport: { width: 1920, height: 1100 },
});
const page = await context.newPage();
const pageErrors = [];
const streamResponses = [];
page.on("pageerror", (error) => pageErrors.push(String(error)));
page.on("response", (response) => {
  if (
    response.status() === 200
    && (response.url().includes("/traces/stream")
      || response.url().includes("/message-stream?"))
  ) {
    streamResponses.push(response.url());
  }
});
let result;

try {
  await ensureGuest(page);
  const workspaces = await api(page, "/api/gateway/workspaces");
  const workspaceId = workspaces?.data?.active_workspace_id;
  if (workspaceId !== expectedWorkspaceId) {
    throw new Error(
      `Gateway active workspace 不一致: expected=${expectedWorkspaceId}, actual=${workspaceId}`,
    );
  }

  const workspaceHeaders = {
    "Content-Type": "application/json",
    "X-BoxTeam-Workspace-Id": workspaceId,
  };
  const created = await api(page, "/api/v1/sessions", {
    method: "POST",
    headers: workspaceHeaders,
    body: JSON.stringify({ title: "浏览器工具循环", agent_id: "default" }),
  });
  const sessionId = created?.data?.session_id;
  if (typeof sessionId !== "string" || !sessionId) {
    throw new Error("创建浏览器工具循环会话缺少 session_id");
  }
  const updated = await api(page, `/api/v1/sessions/${sessionId}`, {
    method: "PATCH",
    headers: workspaceHeaders,
    body: JSON.stringify({ provider_id: "primary" }),
  });
  if (updated?.data?.current_provider_id !== "primary") {
    throw new Error("浏览器工具循环会话没有切换到 primary provider");
  }

  await page.reload({ waitUntil: "domcontentloaded" });
  const sessionButton = page.locator(`button[data-session-id="${sessionId}"]`);
  await sessionButton.waitFor({ state: "visible", timeout: 30_000 });
  await sessionButton.click();
  await page.locator("#input").waitFor({ state: "visible", timeout: 30_000 });
  await waitUntil(
    async () => streamResponses.some((url) => url.includes("/traces/stream")),
    "会话 trace SSE",
  );

  const sendRequestPromise = page.waitForRequest(isSessionMessageRequest, {
    timeout: 30_000,
  });
  const sendResponsePromise = page.waitForResponse(
    (response) => isSessionMessageRequest(response.request()) && response.status() === 200,
    { timeout: 30_000 },
  );
  await page.locator("#input").fill(userMessage);
  await page.locator("#sendButton").click();
  const [sendRequest, sendResponse] = await Promise.all([
    sendRequestPromise,
    sendResponsePromise,
  ]);
  const sentPayload = sendRequest.postDataJSON();
  if (sentPayload?.message?.content !== userMessage) {
    throw new Error(`Composer 发送内容不一致: ${JSON.stringify(sentPayload)}`);
  }
  if (sendResponse.status() !== 200) {
    throw new Error(`Composer 发送失败: HTTP ${sendResponse.status()}`);
  }

  await waitUntil(
    async () => streamResponses.some((url) => url.includes("/message-stream?")),
    "Turn message SSE",
  );
  await waitUntil(
    async () => page.evaluate(
      (text) => document.body.innerText.includes(text),
      expectedFinalText,
    ),
    "浏览器最终回复",
  );
  const thinkingToggle = page.locator(".chat-thinking-toggle").filter({
    hasText: "已运行 read_file",
  });
  await thinkingToggle.waitFor({ state: "visible", timeout: 30_000 });
  await thinkingToggle.click();
  const completedTool = page.locator(".chat-tool-row").filter({
    hasText: "已运行 read_file",
  });
  await completedTool.waitFor({ state: "visible", timeout: 30_000 });
  const completedToolText = await completedTool.innerText();
  if (
    completedToolText.includes("未完成")
    || completedToolText.includes("失败")
    || completedToolText.includes("未知")
  ) {
    throw new Error(`read_file 工具执行状态异常: ${completedToolText}`);
  }

  result = {
    workspaceId,
    sessionId,
    composerSendRequest: true,
    sentMessage: sentPayload.message.content,
    traceSseConnected: streamResponses.some((url) => url.includes("/traces/stream")),
    messageSseConnected: streamResponses.some((url) => url.includes("/message-stream?")),
    completedToolVisible: await completedTool.isVisible(),
    completedToolText,
    finalTextVisible: true,
    pageErrors,
    noPageErrors: pageErrors.length === 0,
  };
  await writeFile(resultPath, JSON.stringify(result, null, 2));
} catch (error) {
  await page
    .screenshot({ path: screenshotPath, fullPage: true })
    .catch(() => undefined);
  throw error;
} finally {
  await context.close();
  await browser.close();
}
