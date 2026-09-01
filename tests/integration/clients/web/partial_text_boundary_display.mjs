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
      body: JSON.stringify({ tracking: { source: "partial-text-boundary-browser" } }),
    });
    await page.reload({ waitUntil: "domcontentloaded" });
  } else if (current.status !== 200) {
    throw new Error(`检查游客访问失败: ${current.status} ${current.body}`);
  }
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
        include: ["user", "assistant_text", "final_response"],
      }),
    },
  );
  const historyItem = history.data.items.find((item) => item.turn_id === turnId);
  const apiCancelled = historyItem?.status === "cancelled";
  const partialResponsePart = historyItem?.response_parts?.find(
    (part) => part.text === "我已经开始分析这个问题，但回答在这里被用户中断……",
  );
  const apiPartial = partialResponsePart?.partial === true;
  const apiCompletionReason =
    partialResponsePart?.completion_reason === "user_interrupt";
  if (!apiCancelled) {
    throw new Error(`历史 API 未返回 cancelled: ${JSON.stringify(historyItem)}`);
  }

  await page.locator(`button[data-session-id="${sessionId}"]`).click();
  const boundaryTurn = page.locator(`[data-turn-id="${turnId}"]`);
  await boundaryTurn.waitFor({ state: "visible", timeout: 30_000 });
  await waitUntil(
    async () => (await boundaryTurn.innerText()).includes(
      "我已经开始分析这个问题，但回答在这里被用户中断……",
    ),
    "partial text 边界正文",
  );
  const boundaryText = await boundaryTurn.innerText();
  const partialTextVisible = boundaryText.includes(
    "我已经开始分析这个问题，但回答在这里被用户中断……",
  );
  const interruptedStatusVisible = await boundaryTurn
    .locator(".chat-inline-cancelled")
    .count()
    .then((count) => count > 0);
  const independentRetryVisible = (await boundaryTurn
    .getByRole("button", { name: "重新生成", exact: true })
    .count()) > 0;
  const failedRetryLabelVisible = boundaryText.includes("重试失败轮次");
  const legacyInterruptedStatusVisible = boundaryText.includes("生成已中断");
  const renderErrorVisible =
    (await boundaryTurn.locator(".chat-turn-error").count()) > 0;
  if (
    !partialTextVisible ||
    !apiPartial ||
    !apiCompletionReason ||
    !interruptedStatusVisible ||
    independentRetryVisible ||
    failedRetryLabelVisible ||
    legacyInterruptedStatusVisible ||
    renderErrorVisible
  ) {
    throw new Error(`partial text 边界展示错误: ${JSON.stringify({
      boundaryText,
      partialTextVisible,
      apiPartial,
      apiCompletionReason,
      interruptedStatusVisible,
      independentRetryVisible,
      failedRetryLabelVisible,
      legacyInterruptedStatusVisible,
      renderErrorVisible,
    })}`);
  }

  result = {
    apiCancelled,
    apiPartial,
    apiCompletionReason,
    partialTextVisible,
    interruptedStatusVisible,
    independentRetryVisible,
    failedRetryLabelVisible,
    renderErrorVisible,
    noPageErrors: pageErrors.length === 0,
    pageErrors,
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
