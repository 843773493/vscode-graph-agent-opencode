import { writeFile } from "node:fs/promises";
import process from "node:process";
import { chromium } from "playwright";

function requiredEnvironment(name) {
  const value = process.env[name];
  if (!value) throw new Error(`缺少环境变量 ${name}`);
  return value;
}

function assertCondition(condition, message) {
  if (!condition) throw new Error(message);
}

const baseUrl = requiredEnvironment("BOXTEAM_E2E_BASE_URL");
const fixture = JSON.parse(requiredEnvironment("BOXTEAM_E2E_FIXTURE"));
const resultPath = requiredEnvironment("BOXTEAM_E2E_RESULT_PATH");
const screenshotPath = requiredEnvironment("BOXTEAM_E2E_SCREENSHOT_PATH");
const executablePath = process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH || undefined;

const browser = await chromium.launch({ executablePath, headless: true });
const context = await browser.newContext({ viewport: { width: 1600, height: 1000 } });
const page = await context.newPage();
// 控制台错误、页面异常与失败请求留档，失败时随结果输出，便于定位 attach 链路问题。
const consoleErrors = [];
const pageErrors = [];
const requestFailures = [];
context.on("console", (message) => {
  if (message.type() === "error") {
    consoleErrors.push(`${Date.now()} ${message.text()}`);
  }
});
context.on("pageerror", (error) => {
  pageErrors.push(`${Date.now()} ${error.message}`);
});
context.on("requestfailed", (request) => {
  requestFailures.push(
    `${Date.now()} ${request.method()} ${request.url()} ${request.failure()?.errorText ?? ""}`,
  );
});
let delayedInitialSnapshot = false;
let delayedClientModule = false;
let clientModuleCacheControl = null;
let abortClientModuleOnce = false;
const result = {
  schema_version: 1,
  browser_id: null,
  popup_url: null,
  attach_url: null,
  transition: null,
  final: null,
  random_uuid_unavailable: null,
  recovery: null,
  tab_and_navigation_failure: null,
};
context.on("response", (response) => {
  if (response.url().includes("/api/gateway/attach/browser/main.js")) {
    clientModuleCacheControl = response.headers()["cache-control"] || null;
  }
});

// 共享宿主机上其他项目的 Docker 网络变更会触发 Chromium 网络变更通知，
// 批量中断在飞的 loopback 请求（ERR_NETWORK_CHANGED，attach 模块图加载失败）。
// attach 资源改由测试进程代取并回注，绕开浏览器网络栈；资源仍来自真实 Gateway。
async function fetchThroughRoute(route) {
  const response = await route.fetch();
  await route.fulfill({ response });
}

await context.route("**/browser-manager/api/browsers/*", async (route) => {
  const request = route.request();
  if (!delayedInitialSnapshot && request.method() === "GET") {
    delayedInitialSnapshot = true;
    await new Promise((resolve) => setTimeout(resolve, 600));
  }
  await fetchThroughRoute(route);
});
await context.route("**/api/gateway/attach/**", async (route) => {
  const request = route.request();
  const url = new URL(request.url());
  if (/^\/api\/gateway\/attach\/(browser|terminal)\/?$/.test(url.pathname)) {
    // 文档必须直连加载：CDP fulfill 会让文档失去本地地址归属，
    // 触发 Chrome Local Network Access 检查阻断后续 WebSocket（ERR_BLOCKED_BY_LOCAL_NETWORK_ACCESS_CHECKS）。
    await route.continue();
    return;
  }
  if (url.pathname === "/api/gateway/attach/browser/main.js") {
    if (abortClientModuleOnce) {
      abortClientModuleOnce = false;
      await route.abort("failed");
      return;
    }
    if (!delayedClientModule) {
      delayedClientModule = true;
      await new Promise((resolve) => setTimeout(resolve, 600));
    }
  }
  await fetchThroughRoute(route);
});

try {
  await page.goto(baseUrl, { waitUntil: "domcontentloaded", timeout: 30_000 });
  // 会话按钮的可访问名包含标题之外的时间/活动文本，不能用 exact 角色名匹配；
  // 按仓库既有模式（gateway_auth 同款）用 data-session-id + hasText 定位。
  const sessionButton = page.locator("[data-session-id]").filter({
    hasText: fixture.sessionTitle,
  }).first();
  await sessionButton.waitFor({ state: "visible", timeout: 15_000 });
  await sessionButton.click();

  // 资源面板入口是内容区的“运行与连接”标签（gateway_auth 同款），
  // 不存在名为“后台连接”的按钮。
  await page.getByRole("tab", { name: "运行与连接", exact: true }).click();
  await page.getByRole("button", { name: /新建连接/ }).waitFor({ state: "visible" });
  await page.getByRole("button", { name: /新建连接/ }).click();

  const createResponsePromise = page.waitForResponse(
    (response) => response.request().method() === "POST"
      && response.url().includes("/browser-manager/api/browsers"),
    { timeout: 30_000 },
  );
  const popupPromise = page.waitForEvent("popup", { timeout: 15_000 });
  await page.getByRole("menuitem", { name: /新建浏览器/ }).click();
  const createResponse = await createResponsePromise;
  assertCondition(createResponse.ok(), `新建浏览器请求失败: ${createResponse.status()}`);
  const createPayload = await createResponse.json();
  const browserId = createPayload?.data?.browser_id;
  assertCondition(typeof browserId === "string" && browserId, "新建浏览器响应缺少 browser_id");
  result.browser_id = browserId;

  // 产品链路：新建浏览器后由 openExtensionWindow 打开扩展窗口，
  // 预览 iframe 渲染在扩展窗口中，主页面只保留资源面板与成功提示。
  const popup = await popupPromise;
  // popup 事件触发时 URL 尚未提交，先等扩展窗口路由生效再读取参数。
  await popup.waitForURL(
    (url) => url.pathname === "/extension",
    { timeout: 15_000 },
  );
  const popupUrl = new URL(popup.url());
  assertCondition(
    popupUrl.pathname === "/extension"
      && popupUrl.searchParams.get("resourceType") === "browser"
      && popupUrl.searchParams.get("resourceId") === browserId,
    `扩展窗口没有定位到新建浏览器资源: ${popup.url()}`,
  );
  result.popup_url = popup.url();

  // iframe title 由资源显示名拼接（含浏览器标题，导航后会变化），
  // 改用包含 browserId 的 src 属性稳定定位。
  const iframeSelector = `iframe[src*="browserId=${encodeURIComponent(browserId)}"]`;
  const iframe = popup.locator(iframeSelector);
  await iframe.waitFor({ state: "visible", timeout: 15_000 });
  result.attach_url = await iframe.getAttribute("src");
  assertCondition(
    result.attach_url?.includes(`/api/gateway/attach/browser/`)
      && result.attach_url.includes(`workspaceId=${encodeURIComponent(fixture.workspaceId)}`)
      && result.attach_url.includes(`browserId=${encodeURIComponent(browserId)}`),
    `浏览器预览未使用 Gateway attach 地址: ${result.attach_url}`,
  );

  const attachedPage = popup.frameLocator(iframeSelector);
  const badge = attachedPage.locator("#attach-state-badge");
  const statusLine = attachedPage.locator("#status-line");
  const overlay = attachedPage.locator("#screen-overlay");
  const address = attachedPage.getByRole("textbox", { name: "浏览器地址" });
  const targetUrl = "data:text/html;charset=utf-8,%3Ctitle%3EManual%20Ready%3C%2Ftitle%3E%3Ch1%3EBOXTEAM_MANUAL_BROWSER_READY%3C%2Fh1%3E";
  await badge.waitFor({ state: "visible", timeout: 15_000 });
  await address.fill(targetUrl);
  await address.press("Enter");
  result.transition = {
    // badge 与 status-line 的状态文本自 a1e7bfc 起只写入 aria-label，textContent 仅剩图标。
    badge: (await badge.getAttribute("aria-label"))?.trim() || "",
    status: (await statusLine.getAttribute("aria-label"))?.trim() || "",
    overlay: (await overlay.textContent())?.trim() || "",
    parentNotice: (await page.locator(".resource-notice").textContent())?.trim() || "",
    queuedAddress: await address.inputValue(),
  };
  const transitionText = Object.values(result.transition).join(" ");
  assertCondition(!transitionText.includes("等待连接"), `初始化阶段出现虚假等待状态: ${transitionText}`);
  assertCondition(!transitionText.includes("未连接"), `初始化阶段出现虚假未连接状态: ${transitionText}`);
  assertCondition(
    /初始化|连接中|正在.*连接/.test(transitionText),
    `初始化阶段没有明确连接反馈: ${transitionText}`,
  );
  assertCondition(result.transition.queuedAddress === targetUrl, "初始化阶段提交的 URL 没有保留");

  await badge.waitFor({ state: "visible", timeout: 15_000 });
  await popup.waitForFunction(
    ({ browserId }) => {
      const frame = [...document.querySelectorAll("iframe")].find((candidate) =>
        candidate.src.includes(`browserId=${browserId}`),
      );
      return frame?.contentDocument?.querySelector("#attach-state-badge")
        ?.getAttribute("aria-label")?.includes("已连接");
    },
    { browserId },
    { timeout: 20_000 },
  );

  const focusedElement = await attachedPage.locator("body").evaluate(
    () => document.activeElement?.id || "",
  );
  assertCondition(focusedElement === "address-input", `空白浏览器连接后焦点不在地址栏: ${focusedElement}`);
  await attachedPage.getByRole("button", { name: "Manual Ready", exact: true }).waitFor({
    state: "visible",
    timeout: 20_000,
  });
  result.final = {
    badge: (await badge.getAttribute("aria-label"))?.trim() || "",
    parentNotice: (await page.locator(".resource-notice").textContent())?.trim() || "",
    focusedElement,
    submittedDuringInitialization: true,
    firstNavigationTitle: "Manual Ready",
    delayedInitialSnapshot,
    delayedClientModule,
    clientModuleCacheControl,
  };
  assertCondition(result.final.badge.includes("已连接"), `首次导航后连接状态异常: ${result.final.badge}`);
  assertCondition(
    result.final.parentNotice.includes("新建浏览器成功")
      // “运行与连接中查看”含“连接中”子串，处理中判定只看“正在”。
      && !result.final.parentNotice.includes("正在"),
    `浏览器可用后父页面提示异常: ${result.final.parentNotice}`,
  );
  assertCondition(delayedInitialSnapshot, "测试未命中浏览器初始化快照请求，过渡态断言无效");
  assertCondition(delayedClientModule, "测试未延迟浏览器客户端模块，早期提交断言无效");
  assertCondition(clientModuleCacheControl === "no-store", `浏览器客户端缓存策略异常: ${clientModuleCacheControl}`);

  const canvas = attachedPage.locator("#screen-canvas");
  const firstTabUrl = "data:text/html;charset=utf-8,%3Ctitle%3ETAB_ONE_GREEN%3C%2Ftitle%3E%3Cstyle%3Ehtml%2Cbody%7Bmargin%3A0%3Bwidth%3A100%25%3Bheight%3A100%25%3Bbackground%3Argb(0%2C200%2C0)%7D%3C%2Fstyle%3E";
  await address.fill(firstTabUrl);
  await address.press("Enter");
  await attachedPage.getByRole("button", { name: "TAB_ONE_GREEN", exact: true }).waitFor({
    state: "visible",
    timeout: 20_000,
  });
  await popup.waitForFunction(
    ({ browserId }) => {
      const frame = [...document.querySelectorAll("iframe")].find((candidate) =>
        candidate.src.includes(`browserId=${browserId}`),
      );
      const targetCanvas = frame?.contentDocument?.querySelector("#screen-canvas");
      if (targetCanvas?.tagName !== "CANVAS") return false;
      const pixel = targetCanvas.getContext("2d")?.getImageData(20, 20, 1, 1).data;
      return pixel && pixel[1] > 150 && pixel[0] < 80;
    },
    { browserId },
    { timeout: 20_000 },
  );

  await attachedPage.locator("#new-tab-button").click();
  await attachedPage.locator(".browser-tab").nth(1).waitFor({ state: "visible", timeout: 10_000 });
  await popup.waitForFunction(
    ({ browserId }) => {
      const frame = [...document.querySelectorAll("iframe")].find((candidate) =>
        candidate.src.includes(`browserId=${browserId}`),
      );
      const targetCanvas = frame?.contentDocument?.querySelector("#screen-canvas");
      if (targetCanvas?.tagName !== "CANVAS") return false;
      const pixel = targetCanvas.getContext("2d")?.getImageData(20, 20, 1, 1).data;
      return pixel && !(pixel[1] > 150 && pixel[0] < 80);
    },
    { browserId },
    { timeout: 20_000 },
  );
  const blankPixel = await canvas.evaluate((element) => (
    [...element.getContext("2d").getImageData(20, 20, 1, 1).data]
  ));
  assertCondition(await address.inputValue() === "about:blank", "新标签页地址不是 about:blank");

  const failedUrl = "http://127.0.0.1:1/";
  await address.fill(failedUrl);
  await address.press("Enter");
  await popup.waitForFunction(
    ({ browserId }) => {
      const frame = [...document.querySelectorAll("iframe")].find((candidate) =>
        candidate.src.includes(`browserId=${browserId}`),
      );
      const status = frame?.contentDocument?.querySelector("#status-line")?.getAttribute("aria-label") || "";
      return status.includes("ERR_") && status.includes("127.0.0.1:1");
    },
    { browserId },
    { timeout: 20_000 },
  );
  const navigationFailedStatus = (await statusLine.getAttribute("aria-label"))?.trim() || "";
  const navigationFailedOverlay = (await overlay.textContent())?.trim() || "";
  assertCondition(await address.inputValue() === failedUrl, "导航失败后地址栏没有保留用户请求 URL");
  assertCondition(navigationFailedOverlay.includes("ERR_"), `画面没有展示导航失败原因: ${navigationFailedOverlay}`);
  assertCondition(!navigationFailedStatus.includes("null/"), `导航失败状态仍显示 null/: ${navigationFailedStatus}`);
  assertCondition(!navigationFailedStatus.includes("\u001b"), `导航失败状态包含 ANSI 控制码: ${navigationFailedStatus}`);
  assertCondition(!navigationFailedOverlay.includes("\u001b"), `导航失败画面包含 ANSI 控制码: ${navigationFailedOverlay}`);

  const recoveredUrl = "data:text/html;charset=utf-8,%3Ctitle%3ERECOVERED_BLUE%3C%2Ftitle%3E%3Cstyle%3Ehtml%2Cbody%7Bmargin%3A0%3Bwidth%3A100%25%3Bheight%3A100%25%3Bbackground%3Argb(0%2C80%2C220)%7D%3C%2Fstyle%3E";
  await address.fill(recoveredUrl);
  await address.press("Enter");
  await attachedPage.getByRole("button", { name: "RECOVERED_BLUE", exact: true }).waitFor({
    state: "visible",
    timeout: 20_000,
  });
  await popup.waitForFunction(
    ({ browserId }) => {
      const frame = [...document.querySelectorAll("iframe")].find((candidate) =>
        candidate.src.includes(`browserId=${browserId}`),
      );
      const targetCanvas = frame?.contentDocument?.querySelector("#screen-canvas");
      if (targetCanvas?.tagName !== "CANVAS") return false;
      const pixel = targetCanvas.getContext("2d")?.getImageData(20, 20, 1, 1).data;
      return pixel && pixel[2] > 150 && pixel[0] < 80;
    },
    { browserId },
    { timeout: 20_000 },
  );
  result.tab_and_navigation_failure = {
    blankPixel,
    failedStatus: navigationFailedStatus,
    failedOverlay: navigationFailedOverlay,
    recoveredTitle: "RECOVERED_BLUE",
    recoveredStatus: (await statusLine.getAttribute("aria-label"))?.trim() || "",
  };
  assertCondition(
    !result.tab_and_navigation_failure.recoveredStatus.includes("ERR_"),
    `恢复成功后仍残留导航错误: ${result.tab_and_navigation_failure.recoveredStatus}`,
  );

  const randomUuidUnavailablePage = await context.newPage();
  await randomUuidUnavailablePage.addInitScript(() => {
    window.sessionStorage.clear();
    Object.defineProperty(window.Crypto.prototype, "randomUUID", {
      configurable: true,
      value: undefined,
    });
  });
  await randomUuidUnavailablePage.goto(result.attach_url, {
    waitUntil: "domcontentloaded",
    timeout: 30_000,
  });
  await randomUuidUnavailablePage.waitForFunction(
    () => document.querySelector("#attach-state-badge")?.getAttribute("aria-label")?.includes("已连接"),
    undefined,
    { timeout: 20_000 },
  );
  result.random_uuid_unavailable = {
    randomUuidType: await randomUuidUnavailablePage.evaluate(() => typeof crypto.randomUUID),
    ready: await randomUuidUnavailablePage.evaluate(
      () => window.BOXTEAM_BROWSER_CLIENT_READY === true,
    ),
    badge: (await randomUuidUnavailablePage.locator("#attach-state-badge")
      .getAttribute("aria-label"))?.trim() || "",
  };
  assertCondition(
    result.random_uuid_unavailable.randomUuidType === "undefined",
    "测试没有成功模拟 crypto.randomUUID 不可用的 HTTP 来源",
  );
  assertCondition(result.random_uuid_unavailable.ready, "randomUUID 不可用时浏览器客户端未完成初始化");
  assertCondition(
    result.random_uuid_unavailable.badge.includes("已连接"),
    `randomUUID 不可用时浏览器没有连接: ${result.random_uuid_unavailable.badge}`,
  );
  await randomUuidUnavailablePage.close();

  const recoveryPage = await context.newPage();
  await recoveryPage.addInitScript(() => {
    const originalSetTimeout = window.setTimeout.bind(window);
    window.setTimeout = (callback, delay = 0, ...args) => originalSetTimeout(
      callback,
      delay === 8000 ? 100 : delay,
      ...args,
    );
  });
  abortClientModuleOnce = true;
  await recoveryPage.goto(result.attach_url, { waitUntil: "domcontentloaded", timeout: 30_000 });
  const recoveryBadge = recoveryPage.locator("#attach-state-badge");
  const recoveryStatus = recoveryPage.locator("#status-line");
  const recoveryOverlay = recoveryPage.locator("#screen-overlay");
  await recoveryBadge.waitFor({ state: "visible", timeout: 5_000 });
  await recoveryPage.waitForFunction(
    () => document.querySelector("#attach-state-badge")?.getAttribute("aria-label") === "初始化失败",
    undefined,
    { timeout: 5_000 },
  );
  const failedStatus = (await recoveryStatus.getAttribute("aria-label"))?.trim() || "";
  assertCondition(failedStatus.includes("点击画面区域重新加载"), `初始化失败提示不完整: ${failedStatus}`);
  await recoveryOverlay.click();
  await recoveryPage.waitForFunction(
    () => document.querySelector("#attach-state-badge")?.getAttribute("aria-label")?.includes("已连接"),
    undefined,
    { timeout: 20_000 },
  );
  result.recovery = {
    failedStatus,
    reloadedBadge: (await recoveryBadge.getAttribute("aria-label"))?.trim() || "",
  };
  assertCondition(result.recovery.reloadedBadge.includes("已连接"), "初始化失败后点击重载没有恢复连接");
  await recoveryPage.close();
  await writeFile(resultPath, `${JSON.stringify(result, null, 2)}\n`, "utf8");
} catch (error) {
  await page.screenshot({ path: screenshotPath, fullPage: true }).catch(() => undefined);
  result.console_errors = consoleErrors;
  result.page_errors = pageErrors;
  result.request_failures = requestFailures;
  const pageStates = [];
  for (const candidate of context.pages()) {
    const entry = { url: candidate.url() };
    try {
      entry.iframes = await candidate.evaluate(() => [
        ...document.querySelectorAll("iframe"),
      ].map((frame) => ({
        title: frame.title,
        src: frame.getAttribute("src"),
        badge: frame.contentDocument?.querySelector("#attach-state-badge")?.getAttribute("aria-label") ?? null,
        status: frame.contentDocument?.querySelector("#status-line")?.getAttribute("aria-label") ?? null,
        overlay: frame.contentDocument?.querySelector("#screen-overlay")?.textContent ?? null,
        clientReady: frame.contentWindow?.BOXTEAM_BROWSER_CLIENT_READY ?? null,
      })));
    } catch (stateError) {
      entry.error = String(stateError);
    }
    pageStates.push(entry);
  }
  result.page_states = pageStates;
  result.error = error instanceof Error ? error.stack || error.message : String(error);
  await writeFile(resultPath, `${JSON.stringify(result, null, 2)}\n`, "utf8");
  throw error;
} finally {
  await browser.close();
}
