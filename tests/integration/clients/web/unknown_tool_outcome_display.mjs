import { writeFile } from "node:fs/promises";
import process from "node:process";
import { chromium } from "playwright";

function requiredEnvironment(name) {
  const value = process.env[name];
  if (!value) throw new Error(`缺少环境变量 ${name}`);
  return value;
}

const baseUrl = requiredEnvironment("BOXTEAM_BROWSER_BASE_URL");
const workspaceId = requiredEnvironment("BOXTEAM_BROWSER_WORKSPACE_ID");
const sessionId = requiredEnvironment("BOXTEAM_BROWSER_SESSION_ID");
const turnId = requiredEnvironment("BOXTEAM_BROWSER_TURN_ID");
const resultPath = requiredEnvironment("BOXTEAM_BROWSER_RESULT_PATH");
const screenshotPath = requiredEnvironment("BOXTEAM_BROWSER_SCREENSHOT_PATH");
const executablePath =
  process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH || undefined;

async function localToken(page) {
  return page.evaluate(async () => {
    const response = await fetch("/api/gateway/auth/local-credential");
    if (!response.ok)
      throw new Error(`获取本地凭据失败: HTTP ${response.status}`);
    const payload = await response.json();
    if (typeof payload?.data?.token !== "string")
      throw new Error("本地凭据缺少 token");
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
      body: JSON.stringify({ tracking: { source: "unknown-tool-outcome-browser" } }),
    });
    await page.reload({ waitUntil: "domcontentloaded" });
  } else if (current.status !== 200) {
    throw new Error(`检查游客访问失败: ${current.status} ${current.body}`);
  }
}

async function waitUntil(predicate, label, timeout = 30_000) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    if (await predicate()) return;
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  throw new Error(`等待${label}超时`);
}

const browser = await chromium.launch({ executablePath, headless: true });
const context = await browser.newContext({
  viewport: { width: 1920, height: 1100 },
});
const page = await context.newPage();
const pageErrors = [];
page.on("pageerror", (error) => pageErrors.push(String(error)));
let result;

try {
  await ensureGuest(page);
  const history = await api(
    page,
    `/api/v1/sessions/${sessionId}/history`,
    {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-BoxTeam-Workspace-Id": workspaceId,
      },
      body: JSON.stringify({
        turn_ids: [turnId],
        include: ["user", "text", "reasoning_detail", "tool_summary", "final_response"],
      }),
    },
  );
  const historyItem = history.data.items.find((item) => item.turn_id === turnId);
  const unknownToolPart = historyItem?.response_parts?.find(
    (part) => part.kind === "tool_call" && part.outcome_unknown === true,
  );
  if (historyItem?.status !== "failed" || !unknownToolPart) {
    throw new Error(`历史 API 未返回未知工具失败事实: ${JSON.stringify(historyItem)}`);
  }

  const sessionList = await api(page, "/api/v1/sessions?limit=100", {
    headers: { "X-BoxTeam-Workspace-Id": workspaceId },
  });
  const sessionListed = sessionList.data.items.some(
    (item) => item.session_id === sessionId,
  );
  if (!sessionListed) {
    throw new Error(`会话列表未返回目标会话: ${sessionId}`);
  }

  // 认证和 Gateway 会话目录同步可能晚于首次页面挂载；确认 API 已就绪后重新加载，
  // 让前端从同一份权威会话列表启动，而不是把启动竞态误判为 UI 缺陷。
  await page.reload({ waitUntil: "domcontentloaded" });
  await page.locator(`button[data-session-id="${sessionId}"]`).click();
  const boundaryTurn = page.locator(`[data-turn-id="${turnId}"]`);
  await boundaryTurn.waitFor({ state: "visible", timeout: 30_000 });
  await waitUntil(
    async () => (await boundaryTurn.innerText()).includes("工具执行结果未知"),
    "折叠态工具异常提示",
  );
  const collapsedText = await boundaryTurn.innerText();
  const unknownStatus = boundaryTurn.locator(
    '[data-status-kind="tool-outcome-unknown"]',
  );
  const unknownStatusVisible = await unknownStatus.isVisible();
  const backendCompletionWarningVisible = collapsedText.includes(
    "large_test_output：后端未返回结果，无法确认是否成功",
  );
  const finalResponseVisible = collapsedText.includes(
    "我已经启动大输出工具，正在等待结果。",
  );

  await boundaryTurn
    .getByRole("button", { name: "展开 Turn 中间消息", exact: true })
    .click();
  await waitUntil(
    async () => (await boundaryTurn.innerText()).includes("large_test_output 结果未知"),
    "展开态工具异常提示",
  );
  const expandedText = await boundaryTurn.innerText();
  const expandedToolStatusVisible = expandedText.includes("large_test_output 结果未知");
  const expandedUnknownDetailVisible = expandedText.includes("未确认返回结果");

  result = {
    apiFailed: historyItem.status === "failed",
    apiUnknownTool: unknownToolPart.tool_name === "large_test_output",
    unknownStatusVisible,
    backendCompletionWarningVisible,
    finalResponseVisible,
    expandedToolStatusVisible,
    expandedUnknownDetailVisible,
    noPageErrors: pageErrors.length === 0,
    pageErrors,
    collapsedText,
    expandedText,
  };
  await writeFile(resultPath, JSON.stringify(result, null, 2));
  await page.screenshot({ path: screenshotPath, fullPage: true });
  if (
    !result.apiFailed
    || !result.apiUnknownTool
    || !result.unknownStatusVisible
    || !result.backendCompletionWarningVisible
    || !result.finalResponseVisible
    || !result.expandedToolStatusVisible
    || !result.expandedUnknownDetailVisible
    || !result.noPageErrors
  ) {
    throw new Error(`未知工具结果展示错误: ${JSON.stringify(result)}`);
  }
} catch (error) {
  await page
    .screenshot({ path: screenshotPath, fullPage: true })
    .catch(() => undefined);
  throw error;
} finally {
  await context.close();
  await browser.close();
}
