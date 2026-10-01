import { writeFile } from "node:fs/promises";
import process from "node:process";
import { chromium } from "playwright";

const CASES = [
  {
    name: "text_tool",
    sessionId: "ses_b1a2c3d4e5f6478899aabbccddeeff01",
    turnId: "boundary-turn-0001",
    apiStatus: "completed",
    finalText: "README 已读取；普通文本、tool_call 和工具结果按顺序展示。",
    expandedTexts: ["read_file"],
  },
  {
    name: "partial_tool_call",
    sessionId: "ses_b1a2c3d4e5f6478899aabbccddeeff02",
    turnId: "boundary-turn-0002",
    apiStatus: "cancelled",
    finalText: "我准备读取配置文件。",
    statusText: "已由用户中断",
    forbiddenTexts: ["工具执行结果未知", "后端未返回结果，无法确认是否成功"],
    expandedTexts: ["read_file 调用未完成"],
  },
  {
    name: "unknown_tool_outcome",
    sessionId: "ses_b1a2c3d4e5f6478899aabbccddeeff03",
    turnId: "boundary-turn-0003",
    apiStatus: "failed",
    finalText: "我已经启动大输出工具，正在等待结果。",
    statusText: "工具执行结果未知",
    expandedTexts: ["large_test_output 结果未知", "未确认返回结果"],
  },
  {
    name: "partial_text",
    sessionId: "ses_b1a2c3d4e5f6478899aabbccddeeff04",
    turnId: "boundary-turn-0004",
    apiStatus: "cancelled",
    finalText: "我已经开始分析这个问题，但回答在这里被用户中断……",
    statusText: "已由用户中断",
    forbiddenTexts: ["生成已中断", "重试失败轮次"],
  },
  {
    name: "tool_markup_text",
    sessionId: "ses_b1a2c3d4e5f6478899aabbccddeeff05",
    turnId: "boundary-turn-0005",
    apiStatus: "completed",
    finalText: "这是普通消息文本，不是实际工具调用。",
    forbiddenSelectors: [".chat-tool-row", '[data-status-kind="tool-outcome-unknown"]'],
  },
  {
    name: "parallel_tool_calls",
    sessionId: "ses_b1a2c3d4e5f6478899aabbccddeeff06",
    turnId: "boundary-turn-0006",
    apiStatus: "completed",
    finalText: "两个工具都已返回，结果已合并到最终答复。",
    expandedTexts: ["read_file", "search_files"],
  },
];

function requiredEnvironment(name) {
  const value = process.env[name];
  if (!value) throw new Error(`缺少环境变量 ${name}`);
  return value;
}

const baseUrl = requiredEnvironment("BOXTEAM_BROWSER_BASE_URL");
const workspaceId = requiredEnvironment("BOXTEAM_BROWSER_WORKSPACE_ID");
const resultPath = requiredEnvironment("BOXTEAM_BROWSER_RESULT_PATH");
const screenshotPath = requiredEnvironment("BOXTEAM_BROWSER_SCREENSHOT_PATH");
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
      body: JSON.stringify({ tracking: { source: "boundary-cases-browser" } }),
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

async function loadBoundaryCase(page, item) {
  const history = await api(
    page,
    `/api/v1/sessions/${item.sessionId}/history`,
    {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-BoxTeam-Workspace-Id": workspaceId,
      },
      body: JSON.stringify({
        turn_ids: [item.turnId],
        include: ["user", "text", "reasoning_detail", "tool_summary", "final_response"],
      }),
    },
  );
  const historyItem = history.data.items.find((candidate) => candidate.turn_id === item.turnId);
  if (historyItem?.status !== item.apiStatus) {
    throw new Error(`${item.name} 历史状态错误: ${JSON.stringify(historyItem)}`);
  }

  const sessionList = await api(page, "/api/v1/sessions?limit=100", {
    headers: { "X-BoxTeam-Workspace-Id": workspaceId },
  });
  if (!sessionList.data.items.some((candidate) => candidate.session_id === item.sessionId)) {
    throw new Error(`${item.name} 会话未进入列表: ${item.sessionId}`);
  }

  // 会话目录同步完成后再刷新，避免把初始目录竞态误判为展示问题。
  await page.reload({ waitUntil: "domcontentloaded" });
  await page.locator(`button[data-session-id="${item.sessionId}"]`).click();
  const turn = page.locator(`[data-turn-id="${item.turnId}"]`);
  await turn.waitFor({ state: "visible", timeout: 30_000 });
  await waitUntil(
    async () => (await turn.innerText()).includes(item.finalText),
    `${item.name} 正文`,
  );
  const collapsedText = await turn.innerText();
  if (item.statusText && !collapsedText.includes(item.statusText)) {
    throw new Error(`${item.name} 缺少状态 ${item.statusText}: ${collapsedText}`);
  }
  for (const forbiddenText of item.forbiddenTexts ?? []) {
    if (collapsedText.includes(forbiddenText)) {
      throw new Error(`${item.name} 出现不应显示的文案 ${forbiddenText}: ${collapsedText}`);
    }
  }
  for (const selector of item.forbiddenSelectors ?? []) {
    if (await turn.locator(selector).count() > 0) {
      throw new Error(`${item.name} 出现不应显示的元素 ${selector}`);
    }
  }

  let expandedText = collapsedText;
  if (item.expandedTexts?.length) {
    await turn.getByRole("button", { name: /^展开 Turn 中间消息/ }).click();
    await waitUntil(
      async () => {
        const text = await turn.innerText();
        return item.expandedTexts.every((expected) => text.includes(expected));
      },
      `${item.name} 中间消息`,
    );
    expandedText = await turn.innerText();
  }
  return {
    name: item.name,
    apiStatus: historyItem.status,
    finalTextVisible: collapsedText.includes(item.finalText),
    statusVisible: item.statusText ? collapsedText.includes(item.statusText) : true,
    forbiddenTextsAbsent: (item.forbiddenTexts ?? []).every((text) => !collapsedText.includes(text)),
    expandedTextsVisible: (item.expandedTexts ?? []).every((text) => expandedText.includes(text)),
    collapsedText,
    expandedText,
  };
}

const browser = await chromium.launch({ executablePath, headless: true });
const context = await browser.newContext({ viewport: { width: 1920, height: 1100 } });
const page = await context.newPage();
const pageErrors = [];
page.on("pageerror", (error) => pageErrors.push(String(error)));
let result;

try {
  await ensureGuest(page);
  const cases = [];
  for (const item of CASES) {
    cases.push(await loadBoundaryCase(page, item));
  }
  result = {
    cases,
    noPageErrors: pageErrors.length === 0,
    pageErrors,
  };
  await writeFile(resultPath, JSON.stringify(result, null, 2));
  await page.screenshot({ path: screenshotPath, fullPage: true });
  if (!result.noPageErrors || cases.some((item) =>
    !item.finalTextVisible
    || !item.statusVisible
    || !item.forbiddenTextsAbsent
    || !item.expandedTextsVisible
  )) {
    throw new Error(`边界展示错误: ${JSON.stringify(result)}`);
  }
} catch (error) {
  await page.screenshot({ path: screenshotPath, fullPage: true }).catch(() => undefined);
  throw error;
} finally {
  await context.close();
  await browser.close();
}
