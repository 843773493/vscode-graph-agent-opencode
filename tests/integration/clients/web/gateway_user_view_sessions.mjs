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
const unopenedSessionId = requiredEnvironment("BOXTEAM_BROWSER_UNOPENED_SESSION_ID");
const resultPath = requiredEnvironment("BOXTEAM_BROWSER_RESULT_PATH");
const screenshotPath = requiredEnvironment("BOXTEAM_BROWSER_SCREENSHOT_PATH");
const executablePath = process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH || undefined;

async function localToken(page) {
  return page.evaluate(async () => {
    const response = await fetch("/api/gateway/auth/local-credential");
    if (!response.ok) throw new Error(`获取本地凭据失败: HTTP ${response.status}`);
    const payload = await response.json();
    const token = payload?.data?.token;
    if (typeof token !== "string" || token.length === 0) {
      throw new Error("本地凭据响应缺少 token");
    }
    return token;
  });
}

async function rawApi(page, pathname, init = {}) {
  const token = await localToken(page);
  return page.evaluate(async ({ pathname: path, init: requestInit, token: localTokenValue }) => {
    const headers = new Headers(requestInit.headers ?? {});
    headers.set("X-Local-Token", localTokenValue);
    const response = await fetch(path, {
      ...requestInit,
      headers,
      credentials: "include",
    });
    return {
      status: response.status,
      body: await response.text(),
    };
  }, { pathname, init, token });
}

async function api(page, pathname, init = {}) {
  const response = await rawApi(page, pathname, init);
  if (response.status < 200 || response.status >= 300) {
    throw new Error(`Gateway API 失败 ${response.status}: ${response.body}`);
  }
  return response.body ? JSON.parse(response.body) : null;
}

async function ensureGuest(page) {
  await page.goto(baseUrl, { waitUntil: "domcontentloaded", timeout: 30_000 });
  await page.getByRole("button", { name: "用户视图" }).waitFor({ state: "visible", timeout: 30_000 });
  const current = await rawApi(page, "/api/gateway/users/current");
  if (current.status === 401) {
    await api(page, "/api/gateway/users/guest", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ tracking: { source: "playwright-browser-integration" } }),
    });
  } else if (current.status !== 200) {
    throw new Error(`检查游客访问失败: ${current.status} ${current.body}`);
  }
}

async function currentAccess(page) {
  const response = await rawApi(page, "/api/gateway/users/current");
  if (response.status !== 200) return null;
  return JSON.parse(response.body).data;
}

async function waitUntil(predicate, label, timeout = 20_000) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    if (await predicate()) return;
    await new Promise((resolve) => setTimeout(resolve, 250));
  }
  throw new Error(`等待${label}超时`);
}

async function waitForCurrentUser(page, userId) {
  await waitUntil(
    async () => (await currentAccess(page))?.user_id === userId,
    `当前用户 ${userId}`,
  );
}

async function waitForGuest(page) {
  await waitUntil(
    async () => (await currentAccess(page))?.kind === "guest",
    "游客视图",
  );
}

const browser = await chromium.launch({ executablePath, headless: true });
const contextA = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
const contextB = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
const pageA = await contextA.newPage();
const pageB = await contextB.newPage();
let contextC = null;
let result;

try {
  await ensureGuest(pageA);
  await ensureGuest(pageB);
  const pageAGuest = await currentAccess(pageA);
  const pageBGuest = await currentAccess(pageB);
  if (pageAGuest?.kind !== "guest" || pageBGuest?.kind !== "guest") {
    throw new Error("Playwright 默认入口没有进入游客视图");
  }

  const userAId = "user-browser-view-a";
  await api(pageA, "/api/gateway/users", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ display_name: "浏览器用户 A", user_id: userAId }),
  });
  await api(pageA, "/api/gateway/users", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ display_name: "浏览器用户 B", user_id: "user-browser-view-b" }),
  });

  contextC = await browser.newContext({ viewport: { width: 1024, height: 768 } });
  const pageC = await contextC.newPage();
  await pageC.goto(baseUrl, { waitUntil: "domcontentloaded", timeout: 30_000 });
  await api(pageC, "/api/gateway/users/user-browser-view-b/access", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ client_label: "过期测试浏览器" }),
  });
  await contextC.close();
  contextC = null;
  await new Promise((resolve) => setTimeout(resolve, 46_000));

  await pageA.getByRole("button", { name: "用户视图" }).click();
  const expiredUserRow = pageA.locator(".gateway-user-row").filter({ hasText: "浏览器用户 B" });
  await expiredUserRow.waitFor({ state: "visible", timeout: 20_000 });
  const expiredUserLeaseReacquired = !(await expiredUserRow.innerText()).includes("占用中");
  if (!expiredUserLeaseReacquired) throw new Error("浏览器用户 B 的过期租约仍显示为占用");
  await expiredUserRow.getByRole("button", { name: "选择", exact: true }).click();
  await waitForCurrentUser(pageA, "user-browser-view-b");

  await api(pageA, `/api/gateway/users/${encodeURIComponent(userAId)}/access`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ client_label: "Web 浏览器" }),
  });
  await waitForCurrentUser(pageA, userAId);

  await api(
    pageA,
    `/api/gateway/users/current/view-state?workspace_id=${encodeURIComponent(workspaceId)}&session_id=${encodeURIComponent(sessionId)}`,
    {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        turn_anchor: "job-0008",
        scroll_offset: 32,
        follow_latest: false,
        projection_version: 1,
        tool_details_expanded: false,
      }),
    },
  );
  await pageA.reload({ waitUntil: "domcontentloaded" });
  await pageA.locator('[data-turn-id="job-0008"]').waitFor({ state: "visible", timeout: 30_000 });

  await pageB.getByRole("button", { name: "用户视图" }).click();
  const userARowB = pageB.locator(".gateway-user-row").filter({ hasText: "浏览器用户 A" });
  await userARowB.waitFor({ state: "visible", timeout: 20_000 });
  await waitUntil(
    async () => (await userARowB.innerText()).includes("占用中"),
    "用户占用状态",
  );
  const occupiedStateVisible = true;
  await userARowB.getByRole("button", { name: "接管", exact: true }).click();
  await waitForCurrentUser(pageB, userAId);

  const oldPageAccess = await rawApi(pageA, "/api/gateway/users/current");
  const oldPageExitedAfterTakeover = oldPageAccess.status === 401;
  await pageB.locator('[data-turn-id="job-0008"]').waitFor({ state: "visible", timeout: 30_000 });

  const jobResponse = await api(pageB, `/api/v1/sessions/${encodeURIComponent(unopenedSessionId)}/messages`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      message: { role: "user", content: "后台完成通知测试" },
      run: { mode: "single_agent", agent_id: "default" },
    }),
  });
  const jobId = jobResponse.data?.job_id;
  if (typeof jobId !== "string" || jobId.length === 0) throw new Error("后台通知测试缺少 job_id");
  let finalJobStatus = null;
  let finalJobError = null;
  await waitUntil(
    async () => {
      const job = (await api(pageB, `/api/v1/jobs/${encodeURIComponent(jobId)}`)).data;
      finalJobStatus = job?.status ?? null;
      finalJobError = job?.error_message ?? null;
      return ["completed", "succeeded", "failed", "cancelled", "timed_out"].includes(finalJobStatus);
    },
    "后台任务完成",
    60_000,
  );
  if (finalJobStatus !== "completed" && finalJobStatus !== "succeeded") {
    throw new Error(`后台任务没有完成: ${finalJobStatus}: ${finalJobError ?? "未知错误"}`);
  }

  const unreadIndicator = pageB
    .locator(`button[data-session-id="${unopenedSessionId}"]`)
    .getByRole("status", { name: "会话有未读结果" });
  await unreadIndicator.waitFor({ state: "visible", timeout: 30_000 });
  const activity = await api(pageB, "/api/v1/session-catalog/events?after=0&limit=100");
  const activityItems = activity.data?.items ?? [];
  const unopenedEvent = activityItems.find((item) => item.session_id === unopenedSessionId);
  const activityText = JSON.stringify(unopenedEvent ?? activityItems);
  result = {
    guestAccess: true,
    ordinaryUserSelection: true,
    expiredUserLeaseReacquired,
    viewRestoredAfterReload: true,
    occupiedStateVisible,
    takeoverCompleted: (await currentAccess(pageB))?.user_id === userAId,
    oldPageExitedAfterTakeover,
    viewRestoredInSecondBrowser: true,
    unopenedSessionUnread: true,
    noPrivateActivityContent: !activityText.includes("后台完成通知测试")
      && !activityText.includes("tool_call")
      && !activityText.includes("tool_result"),
  };
  await writeFile(resultPath, JSON.stringify(result, null, 2));
} catch (error) {
  await pageA.screenshot({ path: screenshotPath, fullPage: true }).catch(() => undefined);
  throw error;
} finally {
  await contextC?.close();
  await contextA.close();
  await contextB.close();
  await browser.close();
}
