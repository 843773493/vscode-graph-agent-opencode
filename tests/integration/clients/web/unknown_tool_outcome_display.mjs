import { writeFile } from "node:fs/promises";
import process from "node:process";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";
import { expect } from "playwright/test";

import { TestRunContext } from "../../../harness/js/run-context.mjs";

function requiredEnvironment(name) {
  const value = process.env[name];
  if (!value) throw new Error(`缺少环境变量 ${name}`);
  return value;
}

const baseUrl = requiredEnvironment("BOXTEAM_BROWSER_BASE_URL");
const workspaceId = requiredEnvironment("BOXTEAM_BROWSER_WORKSPACE_ID");
const gatewayId = requiredEnvironment("BOXTEAM_BROWSER_GATEWAY_ID");
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
    async ({ pathname: path, init: requestInit, token: localTokenValue, gatewayId, workspaceId }) => {
      const headers = new Headers(requestInit.headers ?? {});
      headers.set("X-Local-Token", localTokenValue);
      headers.set("X-BoxTeam-Target-Gateway-Id", gatewayId);
      headers.set("X-BoxTeam-Workspace-Id", workspaceId);
      const response = await fetch(path, {
        ...requestInit,
        headers,
        credentials: "include",
      });
      const body = await response.text();
      if (!response.ok) throw new Error(`API ${response.status}: ${body}`);
      return body ? JSON.parse(body) : null;
    },
    { pathname, init, token, gatewayId, workspaceId },
  );
}

async function openWorkbenchPage(page) {
  await page.goto(new URL("/", baseUrl).href, { waitUntil: "domcontentloaded", timeout: 30_000 });
  await expect(page.locator("#workbench .monaco-workbench")).toBeVisible();
}

async function waitUntil(predicate, label, timeout = 30_000) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    if (await predicate()) return;
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  throw new Error(`等待${label}超时`);
}

const runContext = TestRunContext.fromTestFile(fileURLToPath(import.meta.url));
let page;
let executionError;
const pageErrors = [];
let result;

try {
  const browser = await chromium.launch({ executablePath, headless: true });
  runContext.addCleanup("边界展示 Chromium", () => browser.close());
  const context = await browser.newContext({
    viewport: { width: 1920, height: 1100 },
  });
  runContext.addCleanup("边界展示 context", () => context.close());
  page = await context.newPage();
  page.on("pageerror", (error) => pageErrors.push(String(error)));
  await openWorkbenchPage(page);
  const history = await api(
    page,
    `/api/v1/sessions/${sessionId}/history`,
    {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        turn_ids: [turnId],
        include: ["user", "text", "reasoning_detail", "tool_summary", "final_response"],
      }),
    },
  );
  const historyItem = history.data.items.find((item) => item.turn_id === turnId);
  const unknownToolPart = historyItem?.response_items?.find(
    (part) => part.kind === "tool_call" && part.outcome_unknown === true,
  );
  if (historyItem?.status !== "failed" || !unknownToolPart) {
    throw new Error(`历史 API 未返回未知工具失败事实: ${JSON.stringify(historyItem)}`);
  }

  const sessionList = await api(page, "/api/v1/sessions?limit=100");
  const sessionListed = sessionList.data.items.some(
    (item) => item.session_id === sessionId,
  );
  if (!sessionListed) {
    throw new Error(`会话列表未返回目标会话: ${sessionId}`);
  }

  // 认证和 Gateway 会话目录同步可能晚于首次页面挂载；确认 API 已就绪后重新加载，
  // 让前端从同一份权威会话列表启动，而不是把启动竞态误判为 UI 缺陷。
  await page.reload({ waitUntil: "domcontentloaded" });
  await page.getByTestId(`workspace-node-${JSON.stringify([gatewayId, workspaceId])}`)
    .locator("..").locator(`button[data-session-id="${sessionId}"]`).dblclick();
  const thread = page.locator(`section.boxteam-thread[data-workspace-id="${workspaceId}"][data-session-id="${sessionId}"]`);
  await expect(thread).toBeVisible();
  const boundaryTurn = thread.locator(`.chat-turn[data-turn-id="${turnId}"]`);
  await boundaryTurn.waitFor({ state: "visible", timeout: 30_000 });
  const processText = "我已经启动大输出工具，正在等待结果。";
  await expect(boundaryTurn.locator('.chat-turn-activity[data-status-kind="turn-failed"]')).toBeVisible();
  const collapsedText = await boundaryTurn.innerText();
  const turnFailureVisible = collapsedText.includes("本轮执行失败");
  const turnFailureDetailVisible = collapsedText.includes(historyItem.failure?.message ?? "该记录未保存失败详情");
  const processHiddenWhenCollapsed = !collapsedText.includes(processText);
  await boundaryTurn.locator(".chat-turn-activity > .chat-thinking-toggle").click();
  await expect(boundaryTurn.locator(".chat-activity-group").first()).toBeVisible();
  for (const group of await boundaryTurn.locator('.chat-activity-group > button').all()) {
    if (await group.getAttribute('aria-expanded') === 'false') await group.click();
  }
  const tool = boundaryTurn.locator('.chat-tool-row.is-unknown');
  await expect(tool).toContainText("large_test_output 结果未知");
  await tool.locator('.chat-tool-summary').click();
  await expect(tool).toContainText("未确认返回结果");
  const expandedText = await boundaryTurn.innerText();
  const expandedToolStatusVisible = expandedText.includes("large_test_output 结果未知");
  const expandedUnknownDetailVisible = expandedText.includes("未确认返回结果");
  const processVisibleWhenExpanded = expandedText.includes(processText);

  result = {
    apiFailed: historyItem.status === "failed",
    apiUnknownTool: unknownToolPart.tool_name === "large_test_output",
    turnFailureVisible,
    turnFailureDetailVisible,
    processHiddenWhenCollapsed,
    processVisibleWhenExpanded,
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
    || !result.turnFailureVisible
    || !result.turnFailureDetailVisible
    || !result.processHiddenWhenCollapsed
    || !result.processVisibleWhenExpanded
    || !result.expandedToolStatusVisible
    || !result.expandedUnknownDetailVisible
    || !result.noPageErrors
  ) {
    throw new Error(`未知工具结果展示错误: ${JSON.stringify(result)}`);
  }
} catch (error) {
  executionError = error;
  if (page) await page.screenshot({ path: screenshotPath, fullPage: true }).catch(() => undefined);
  throw error;
} finally {
  try {
    await runContext.close();
  } catch (cleanupError) {
    if (executionError) {
      throw new AggregateError([executionError, cleanupError], "边界展示执行与清理均失败");
    }
    throw cleanupError;
  }
}
