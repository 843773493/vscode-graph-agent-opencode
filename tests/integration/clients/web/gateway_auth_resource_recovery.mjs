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
const owner = JSON.parse(requiredEnvironment("BOXTEAM_BROWSER_OWNER"));
const resultPath = requiredEnvironment("BOXTEAM_BROWSER_RESULT_PATH");
const screenshotPath = requiredEnvironment("BOXTEAM_BROWSER_SCREENSHOT_PATH");
const scope = TestRunContext.fromTestFile("tests/integration/clients/web/test_gateway_auth_resource_recovery.py");
const pageErrors = [];
const resourceResponses = [];
let expireNextResourceRequest = false;
let expiredResourceRequests = 0;
let page;
let failure;
const result = { classification: "Integration", resourceResponses, pageErrors };

async function verifyNavigationDiagnostic(thread) {
  const endpoint = "/api/gateway/workspace-navigation";
  const matches = url => url.pathname === endpoint;
  const detail = `导航读取失败\n来源: ${endpoint}\n<script>throw new Error("不得执行诊断正文")</script>\n完整错误末尾`;
  const requestId = "navigation-diagnostic-explicit-failure";
  const draft = "诊断关闭后仍应保留的草稿";
  const requests = [];
  const diagnosticReads = [];
  const observe = request => {
    if (new URL(request.url()).pathname === "/api/gateway/diagnostics") diagnosticReads.push(request.url());
  };
  const intercept = async route => {
    if (route.request().method() !== "GET") return route.continue();
    requests.push({ url: route.request().url(), method: route.request().method() });
    await route.fulfill({ status: 503, contentType: "application/json",
      headers: { "X-Request-ID": requestId }, body: JSON.stringify({ detail, request_id: requestId }) });
  };
  result.navigationDiagnostic = { boundary: "仅导航 GET 返回显式 503，其余请求经真实 Gateway", requests, diagnosticReads };
  await thread.getByTestId("composer-input").fill(draft);
  page.on("request", observe);
  await page.route(matches, intercept);
  try {
    await page.getByRole("button", { name: "刷新资源树", exact: true }).click();
    const alert = page.getByRole("alert").filter({ hasText: "无法加载工作区列表" });
    await expect(alert).toBeVisible();
    await alert.getByRole("button", { name: "查看日志", exact: true }).click();
    const dialog = page.getByRole("dialog", { name: "无法加载工作区列表", exact: true });
    await expect(dialog).toBeVisible();
    await expect(dialog.locator("pre")).toContainText(detail);
    await expect(dialog.locator("script")).toHaveCount(0);
    expect(diagnosticReads).toEqual([]);
    expect(requests.length).toBeGreaterThan(0);
    result.navigationDiagnostic.detail = await dialog.locator("pre").textContent();
    await dialog.getByRole("button", { name: "关闭诊断", exact: true }).click();
    await expect(dialog).toHaveCount(0);
    await expect(thread.getByTestId("composer-input")).toHaveValue(draft);
  } finally {
    await page.unroute(matches, intercept);
    page.off("request", observe);
  }
  const recovered = page.waitForResponse(response => matches(new URL(response.url())) && response.request().method() === "GET");
  await page.getByRole("button", { name: "重新加载工作区列表", exact: true }).click();
  const response = await recovered;
  expect(response.status()).toBe(200);
  const payload = await response.json();
  expect(payload.request_id).toBeTruthy();
  expect(response.headers()["x-request-id"]).toBe(payload.request_id);
  await expect(page.getByRole("alert").filter({ hasText: "无法加载工作区列表" })).toHaveCount(0);
  await expect(thread.getByTestId("composer-input")).toHaveValue(draft);
  result.navigationDiagnostic.recoveredRequestId = payload.request_id;
  result.navigationDiagnostic.draftRetained = true;
}

// 先由穿透 ShadowRoot 的 locator 取得真实节点，再测量其所在局部布局。
async function panelLayout(body, panelSelector) {
  await expect(body).toBeVisible();
  await expect(body.locator(panelSelector)).toBeVisible();
  return body.evaluate((element, panelSelector) => {
    const panel = element.querySelector(panelSelector);
    const auxiliary = element.closest(".auxiliary-panel");
    const resources = element.closest(".native-thread-resources");
    if (!panel || !auxiliary || !resources) throw new Error("窄屏布局缺少真实资源容器");
    const bodyRect = element.getBoundingClientRect();
    const panelRect = panel.getBoundingClientRect();
    const auxiliaryRect = auxiliary.getBoundingClientRect();
    return { resourcesWidth: resources.getBoundingClientRect().width,
      bodyWidth: bodyRect.width, panelWidth: panelRect.width, auxiliaryPanelWidth: auxiliaryRect.width,
      panelRightGap: bodyRect.right - panelRect.right, auxiliaryPanelRightGap: bodyRect.right - auxiliaryRect.right,
      hasSharedPreview: resources.querySelector(".has-shared-preview") !== null,
      runtimePreviewPresent: resources.querySelector(".workspace-runtime-preview") !== null };
  }, panelSelector);
}
function fillsBody(layout) {
  return Math.abs(layout.resourcesWidth - 236) <= 1
    && layout.bodyWidth > 0 && layout.panelWidth > 0
    && Math.abs(layout.auxiliaryPanelRightGap) <= 1 && Math.abs(layout.panelRightGap) <= 1
    && !layout.hasSharedPreview && !layout.runtimePreviewPresent;
}

try {
  const browser = await chromium.launch({ executablePath: requiredEnvironment("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH"), headless: true });
  scope.addCleanup("认证恢复 Chromium", () => browser.close());
  const context = await browser.newContext({ viewport: { width: 1074, height: 912 } });
  scope.addCleanup("认证恢复 context", () => context.close());
  expect(await context.cookies()).toHaveLength(0);
  page = await context.newPage();
  page.on("pageerror", error => pageErrors.push(String(error)));
  page.on("response", response => {
    if (new URL(response.url()).pathname === `/api/v1/sessions/${owner.sessionId}/resources`) {
      resourceResponses.push(response.status());
    }
  });
  // Chromium 不允许 route.continue 覆盖 Cookie；由真实 Gateway 收到一次无 Cookie 请求，回注原响应。
  // 其它导航轮询可以并发恢复 Cookie，这里精确验证资源请求自身的 401 恢复链。
  await context.route(`**/api/v1/sessions/${owner.sessionId}/resources`, async route => {
    if (expireNextResourceRequest) {
      expireNextResourceRequest = false;
      expiredResourceRequests += 1;
      const response = await route.fetch({ headers: { ...route.request().headers(), cookie: "" } });
      expect(response.status()).toBe(401);
      expect((await response.json()).detail).toBe("user_session_required");
      await route.fulfill({ response });
    } else await route.continue();
  });
  // 不预先获取访客 Cookie，直接验证工作台初始化本身的认证恢复。
  await page.goto(new URL("/", baseUrl).href, { waitUntil: "domcontentloaded", timeout: 30_000 });
  await expect(page.locator("#workbench .monaco-workbench")).toBeVisible();
  const sessionButton = page.locator(`button[data-session-id="${owner.sessionId}"]`);
  await expect(sessionButton).toBeVisible({ timeout: 30_000 });
  await sessionButton.dblclick();
  const thread = page.locator(`section.boxteam-thread[data-workspace-id="${owner.workspaceId}"][data-session-id="${owner.sessionId}"][data-thread-id="${owner.threadId}"]`);
  await expect(thread).toBeVisible();
  await expect(thread).not.toContainText(/前端初始化失败|user_session_required/);
  result.bootstrapWithoutCookie = (await context.cookies()).length > 0;
  const toggle = thread.getByRole("button", { name: "切换会话资源", exact: true });
  if (await toggle.getAttribute("aria-expanded") !== "true") await toggle.click();
  const resources = thread.locator(".native-thread-resources");
  await resources.getByRole("tab", { name: "运行与连接", exact: true }).click();
  await expect.poll(() => resourceResponses.includes(200)).toBe(true);
  // 下一次真实资源轮询自己承受 401，并通过既有认证链自动恢复。
  resourceResponses.length = 0;
  expireNextResourceRequest = true;
  await context.clearCookies();
  await expect.poll(() => resourceResponses.includes(401) && resourceResponses.includes(200), { timeout: 15_000 }).toBe(true);
  result.resourceUnauthorizedThenRecovered = true;
  expect(expiredResourceRequests).toBe(1);
  result.expiredResourceRequests = expiredResourceRequests;
  await expect(resources.locator(".resource-panel")).not.toContainText(/后台连接加载失败|user_session_required/);
  result.resourcePanelErrorAbsent = true;
  await resources.evaluate(element => { element.style.flex = "0 0 236px"; });
  await expect.poll(() => resources.evaluate(element => Math.round(element.getBoundingClientRect().width))).toBe(236);
  result.resourcePanelVisible = await resources.isVisible();
  result.resourcePanelLayout = await panelLayout(resources.locator(".auxiliary-resources-body"), ".resource-panel");
  result.resourcePanelFillsBody = fillsBody(result.resourcePanelLayout);
  await resources.getByRole("tab", { name: "调试", exact: true }).click();
  result.debugPanelLayout = await panelLayout(resources.locator(".auxiliary-debug-body"), ".debug-panel");
  result.debugPanelFillsBody = fillsBody(result.debugPanelLayout);
  const beforeHidden = resourceResponses.length;
  // 跨越原有 5 秒周期，隐藏的资源页不得继续轮询。
  await page.waitForTimeout(5500);
  expect(resourceResponses).toHaveLength(beforeHidden);
  result.hiddenPollingStopped = true;
  await resources.getByRole("tab", { name: "运行与连接", exact: true }).click();
  await expect.poll(() => resourceResponses.length > beforeHidden && resourceResponses.at(-1) === 200).toBe(true);
  result.visiblePollingRestored = true;
  await verifyNavigationDiagnostic(thread);
  expect(result.bootstrapWithoutCookie).toBe(true);
  expect(result.resourcePanelVisible).toBe(true);
  expect(result.resourcePanelFillsBody).toBe(true);
  expect(result.debugPanelFillsBody).toBe(true);
  expect(pageErrors).toEqual([]);
  result.noPageErrors = true;
} catch (error) {
  failure = error;
  result.failure = String(error.stack ?? error);
} finally {
  const errors = failure ? [failure] : [];
  try {
    if (page) await page.screenshot({ path: screenshotPath, fullPage: true });
    await writeFile(resultPath, JSON.stringify(result, null, 2) + "\n");
  } catch (error) { errors.push(error); }
  try { await scope.close(); } catch (error) { errors.push(error); }
  if (errors.length) throw new AggregateError(errors, "Gateway 认证恢复 Web 验收失败");
}
