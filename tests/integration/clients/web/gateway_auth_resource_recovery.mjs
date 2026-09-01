import { writeFile } from "node:fs/promises";
import process from "node:process";
import { chromium } from "playwright";

function requiredEnvironment(name) {
  const value = process.env[name];
  if (!value) throw new Error(`缺少环境变量 ${name}`);
  return value;
}

async function waitUntil(predicate, label, timeout = 30_000) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    if (await predicate()) return;
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  throw new Error(`等待${label}超时`);
}

const baseUrl = requiredEnvironment("BOXTEAM_BROWSER_BASE_URL");
const sessionTitle = requiredEnvironment("BOXTEAM_BROWSER_SESSION_TITLE");
const resultPath = requiredEnvironment("BOXTEAM_BROWSER_RESULT_PATH");
const screenshotPath = requiredEnvironment("BOXTEAM_BROWSER_SCREENSHOT_PATH");
const executablePath = process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH || undefined;

const browser = await chromium.launch({ executablePath, headless: true });
const context = await browser.newContext({ viewport: { width: 1074, height: 912 } });
const page = await context.newPage();
const pageErrors = [];
const resourceResponses = [];
page.on("pageerror", (error) => pageErrors.push(String(error)));
page.on("response", (response) => {
  if (
    response.url().includes("/api/v1/sessions/")
    && response.url().includes("/resources")
  ) {
    resourceResponses.push(response.status());
  }
});

let result;
try {
  await page.goto(baseUrl, { waitUntil: "domcontentloaded", timeout: 30_000 });
  const sessionButton = page.locator("[data-session-id]").filter({
    hasText: sessionTitle,
  }).first();
  await sessionButton.waitFor({ state: "visible", timeout: 30_000 });
  await waitUntil(
    async () => (await page.getByText("前端初始化失败").count()) === 0,
    "无 Cookie 冷启动完成",
  );
  const bootstrapWithoutCookie =
    (await page.getByText("前端初始化失败").count()) === 0
    && (await page.getByText("user_session_required").count()) === 0;

  await context.clearCookies();
  resourceResponses.length = 0;
  await page.getByRole("tab", { name: "文件", exact: true }).click();
  await page.getByRole("tab", { name: "运行与连接", exact: true }).click();
  await waitUntil(
    async () => resourceResponses.includes(401) && resourceResponses.includes(200),
    "资源认证失效后自动恢复",
    15_000,
  );
  await page.waitForTimeout(300);
  await page.locator(".workspace-editor-shell").evaluate((editor) => {
    const contentLayout = editor.parentElement;
    const chatPanel = contentLayout?.querySelector(".chat-panel");
    if (!(chatPanel instanceof HTMLElement)) {
      throw new Error("窄屏资源面板布局测试缺少会话区");
    }
    editor.style.flex = "0 0 236px";
    chatPanel.style.flex = "1 1 auto";
  });
  await page.waitForTimeout(100);
  const resourcePanelCollapsed =
    (await page.locator(".content-layout").getAttribute("class"))
      ?.includes("auxiliary-collapsed") ?? false;
  const resourcePanelLayout = await page.locator(".auxiliary-resources-body").evaluate((body) => {
    const resourcePanel = body.querySelector(".resource-panel");
    const auxiliaryPanel = body.closest(".auxiliary-panel");
    if (!resourcePanel) {
      return null;
    }
    const bodyRect = body.getBoundingClientRect();
    const auxiliaryPanelRect = auxiliaryPanel?.getBoundingClientRect() ?? null;
    const resourcePanelRect = resourcePanel.getBoundingClientRect();
    return {
      workspaceEditorWidth: body.closest(".workspace-editor-shell")?.getBoundingClientRect().width ?? null,
      bodyWidth: bodyRect.width,
      auxiliaryPanelWidth: auxiliaryPanelRect?.width ?? null,
      auxiliaryPanelRightGap: auxiliaryPanelRect ? bodyRect.right - auxiliaryPanelRect.right : null,
      hasSharedPreview: body.classList.contains("has-shared-preview"),
      resourcePanelWidth: resourcePanelRect.width,
      resourcePanelRightGap: bodyRect.right - resourcePanelRect.right,
      runtimePreviewPresent: body.querySelector(".workspace-runtime-preview") !== null,
    };
  });
  await page.getByRole("tab", { name: "调试", exact: true }).click();
  await page.waitForTimeout(100);
  const debugPanelLayout = await page.locator(".workspace-editor-body-debug").evaluate((body) => {
    const auxiliaryPanel = body.querySelector(":scope > .auxiliary-panel");
    const debugPanel = body.querySelector(".debug-panel");
    if (!auxiliaryPanel || !debugPanel) {
      return null;
    }
    const bodyRect = body.getBoundingClientRect();
    const auxiliaryPanelRect = auxiliaryPanel.getBoundingClientRect();
    const debugPanelRect = debugPanel.getBoundingClientRect();
    return {
      bodyWidth: bodyRect.width,
      auxiliaryPanelWidth: auxiliaryPanelRect.width,
      auxiliaryPanelRightGap: bodyRect.right - auxiliaryPanelRect.right,
      debugPanelWidth: debugPanelRect.width,
      debugPanelRightGap: bodyRect.right - debugPanelRect.right,
      hasSharedPreview: body.classList.contains("has-shared-preview"),
    };
  });
  await page.getByRole("tab", { name: "运行与连接", exact: true }).click();
  await page.waitForTimeout(100);

  result = {
    schema_version: 1,
    bootstrapWithoutCookie,
    resourceResponses,
    resourceUnauthorizedThenRecovered:
      resourceResponses.includes(401) && resourceResponses.includes(200),
    resourcePanelErrorAbsent:
      (await page.locator(".resource-panel").getByText(
        /后台连接加载失败|user_session_required/,
      ).count()) === 0,
    resourcePanelCollapsed,
    resourcePanelVisible: !resourcePanelCollapsed,
    resourcePanelFillsBody: resourcePanelLayout !== null
      && resourcePanelLayout.runtimePreviewPresent === false
      && resourcePanelLayout.hasSharedPreview === false
      && resourcePanelLayout.auxiliaryPanelRightGap <= 1
      && resourcePanelLayout.resourcePanelRightGap <= 1,
    resourcePanelLayout,
    debugPanelFillsBody: debugPanelLayout !== null
      && debugPanelLayout.hasSharedPreview === false
      && debugPanelLayout.auxiliaryPanelRightGap <= 1
      && debugPanelLayout.debugPanelRightGap <= 1,
    debugPanelLayout,
    noPageErrors: pageErrors.length === 0,
    pageErrors,
  };
  await writeFile(resultPath, JSON.stringify(result, null, 2));
  await page.screenshot({ path: screenshotPath, fullPage: true });
  if (
    !result.bootstrapWithoutCookie
    || !result.resourceUnauthorizedThenRecovered
    || !result.resourcePanelErrorAbsent
    || !result.resourcePanelVisible
    || !result.resourcePanelFillsBody
    || !result.noPageErrors
  ) {
    throw new Error(`Gateway 认证恢复展示错误: ${JSON.stringify(result)}`);
  }
} catch (error) {
  await page.screenshot({ path: screenshotPath, fullPage: true }).catch(() => undefined);
  throw error;
} finally {
  await context.close();
  await browser.close();
}
