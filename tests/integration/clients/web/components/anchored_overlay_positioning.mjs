import { mkdir, writeFile } from "node:fs/promises";
import { dirname, join, resolve } from "node:path";
import process from "node:process";
import { fileURLToPath, pathToFileURL } from "node:url";
import { chromium } from "playwright";

import { TestRunContext } from "../../../../harness/js/run-context.mjs";

const testFile = fileURLToPath(import.meta.url);
const runContext = await TestRunContext.fromTestFile(
  testFile,
  process.env.BOXTEAM_PROJECT_ROOT ?? process.cwd(),
).prepare();
const projectRoot = runContext.projectRoot;
const runtimeDirectory = join(runContext.outputRoot, "runtime");
await mkdir(runtimeDirectory, { recursive: true });
for (const name of ["TMPDIR", "TMP", "TEMP"]) process.env[name] = runtimeDirectory;
const resultPath = join(
  runContext.artifactsDir,
  "anchored-overlay-positioning-result.json",
);
const screenshotPath = join(
  runContext.artifactsDir,
  "anchored-overlay-positioning-failure.png",
);
const executablePath = process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH || undefined;
const harnessPath = dirname(testFile);
const workspaceRoot = "boxteam://workspace/backend_files/resources/files/root";
const secondFilePath = `${workspaceRoot}/second.ts`;

async function measureFileTreeLayout(page) {
  return await page.evaluate((rowTitle) => {
    const host = document.querySelector(
      'section[aria-label="工作区文件树错误提示布局"]',
    );
    const workspace = document.querySelector(".workspace-file-tree");
    const notice = document.querySelector(".files-tree-notifications");
    const tree = document.querySelector(".files-tree-root");
    const row = Array.from(tree?.querySelectorAll("[title]") ?? []).find(
      (element) => element.getAttribute("title") === rowTitle,
    );
    if (
      !(host instanceof HTMLElement)
      || !(workspace instanceof HTMLElement)
      || !(notice instanceof HTMLElement)
      || !(tree instanceof HTMLElement)
      || !(row instanceof HTMLElement)
    ) {
      throw new Error("文件树布局测量缺少宿主、提示位、树容器或目标行");
    }
    const documentBox = (element) => {
      const rect = element.getBoundingClientRect();
      return {
        x: rect.x + window.scrollX,
        y: rect.y + window.scrollY,
        width: rect.width,
        height: rect.height,
      };
    };
    return {
      pageScrollX: window.scrollX,
      pageScrollY: window.scrollY,
      host: documentBox(host),
      workspace: documentBox(workspace),
      notification: documentBox(notice),
      notificationPosition: getComputedStyle(notice).position,
      tree: documentBox(tree),
      row: documentBox(row),
    };
  }, secondFilePath);
}

const { createServer } = await import(
  pathToFileURL(
    `${projectRoot}/src/clients/web/node_modules/vite/dist/node/index.js`,
  ).href,
);

try {
  const vite = await createServer({
    root: harnessPath,
    cacheDir: join(runtimeDirectory, "vite-cache"),
    logLevel: "error",
    server: {
      host: "127.0.0.1",
      port: 0,
      strictPort: false,
      fs: { allow: [projectRoot] },
    },
    resolve: {
      alias: {
        react: resolve(projectRoot, "src/clients/web/node_modules/react"),
        "react-dom": resolve(projectRoot, "src/clients/web/node_modules/react-dom"),
        "@floating-ui/react": resolve(
          projectRoot,
          "src/clients/web/node_modules/@floating-ui/react",
        ),
        "@vscode/codicons": resolve(
          projectRoot,
          "src/clients/web/node_modules/@vscode/codicons",
        ),
      },
    },
  });
  runContext.addCleanup("AnchoredOverlay Vite", () => vite.close());
  await vite.listen();

  const address = vite.httpServer?.address();
  if (!address || typeof address === "string") {
    throw new Error("无法读取 AnchoredOverlay Integration 的 Vite 端口");
  }

  const browser = await chromium.launch({ executablePath, headless: true });
  runContext.addCleanup("AnchoredOverlay Chromium", () => browser.close());
  const context = await browser.newContext({ viewport: { width: 1280, height: 800 } });
  runContext.addCleanup("AnchoredOverlay BrowserContext", () => context.close());
  const page = await context.newPage();
  const unexpectedApiRequests = [];
  const apiRequests = [];
  const apiResponses = [];
  const browserErrors = [];
  page.on("pageerror", (error) => browserErrors.push(error.message));
  page.on("console", (message) => {
    if (message.type() === "error") browserErrors.push(message.text());
  });
  page.on("requestfailed", (request) => {
    browserErrors.push(`${request.method()} ${request.url()}: ${request.failure()?.errorText ?? "请求失败"}`);
  });
  page.on("request", (request) => {
    const url = new URL(request.url());
    if (url.pathname.startsWith("/api/")) {
      apiRequests.push(`${request.method()} ${url.pathname}${url.search}`);
    }
  });
  page.on("response", (response) => {
    const url = new URL(response.url());
    if (url.pathname.startsWith("/api/")) {
      apiResponses.push(`${response.status()} ${response.request().method()} ${url.pathname}${url.search}`);
    }
  });
  await page.route("**/api/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (!url.pathname.startsWith("/api/")) {
      await route.continue();
      return;
    }
    const ok = (data) => route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ code: 0, message: "ok", request_id: "req-layout", data }),
    });

    if (url.pathname === "/api/gateway/auth/local-credential") {
      await ok({ token: "layout-token" });
      return;
    }
    if (url.pathname === "/api/gateway/users/current") {
      await ok({ kind: "guest", user_id: null });
      return;
    }
    if (url.pathname === "/api/v1/workspace/files/roots") {
      await ok({
        workspace_uri: workspaceRoot,
        filesystem_uri: "boxteam://gateway/gateway_layout/resources/files/root",
        filesystem_label: "/",
      });
      return;
    }
    if (url.pathname === "/api/v1/workspace/files" && request.method() === "GET") {
      const path = url.searchParams.get("path") || workspaceRoot;
      await ok({
        directory_key: {
          workspace_id: "backend_files",
          scope: "workspace",
          root: workspaceRoot,
          path,
        },
        directory_revision: 1,
        root_path: path,
        path,
        items: path === workspaceRoot
          ? ["first.ts", "second.ts", ...Array.from(
            { length: 80 },
            (_, index) => `entry-${String(index).padStart(3, "0")}.ts`,
          )].map((name, index) => ({
            name,
            path: `${workspaceRoot}/${name}`,
            kind: "file",
            has_children: false,
            size: index + 1,
            modified_at: null,
          }))
          : [],
        truncated: false,
        next_cursor: null,
      });
      return;
    }
    if (url.pathname === "/api/v1/sessions/ses_layout/file-tree-settings") {
      await route.fulfill({
        status: 500,
        contentType: "application/json",
        body: JSON.stringify({ detail: "模拟快捷路径设置读取失败" }),
      });
      return;
    }
    if (
      url.pathname === "/api/v1/workspace/files/reveal"
      && request.method() === "POST"
    ) {
      await route.fulfill({
        status: 500,
        contentType: "application/json",
        body: JSON.stringify({ detail: "模拟服务器拒绝系统显示请求" }),
      });
      return;
    }

    unexpectedApiRequests.push(`${request.method()} ${url.pathname}`);
    await route.fulfill({
      status: 501,
      contentType: "application/json",
      body: JSON.stringify({ detail: "集成场景未声明此 API 请求" }),
    });
  });
  const result = {
    anchor: null,
    overlay: null,
    fileTreeNotice: null,
    searchStatusLayout: null,
    viewport: { width: 1280, height: 800 },
  };

  try {
    const settingsResponse = page.waitForResponse((response) => (
      response.url().includes("/api/v1/sessions/ses_layout/file-tree-settings")
      && response.status() === 500
    ), { timeout: 10_000 });
    await page.goto(
      `http://127.0.0.1:${address.port}/anchored_overlay_positioning_harness.html`,
      { waitUntil: "networkidle", timeout: 30_000 },
    );
    const workspaceRootRow = page.locator(".files-tree-item.root").filter({
      hasText: "layout-fixture",
    });
    await workspaceRootRow.waitFor({ state: "visible", timeout: 10_000 });
    if (await workspaceRootRow.getAttribute("aria-expanded") === "true") {
      await workspaceRootRow.click();
    }
    await workspaceRootRow.click();
    const secondFileRow = page.getByTitle(`${workspaceRoot}/second.ts`, { exact: true });
    await secondFileRow.waitFor({ state: "visible", timeout: 10_000 });
    await settingsResponse;
    const anchor = page.getByTestId("anchor");
    await anchor.click();
    const overlay = page.getByTestId("overlay");
    await overlay.waitFor({ state: "visible", timeout: 10_000 });
    await page.waitForFunction(() => {
      const element = document.querySelector('[data-testid="overlay"]');
      if (!element) return false;
      const rect = element.getBoundingClientRect();
      return rect.width > 0
        && rect.height > 0
        && rect.left > 0
        && rect.top > 0;
    }, undefined, { timeout: 10_000 });

    const measurements = await page.evaluate(() => {
      const anchorElement = document.querySelector('[data-testid="anchor"]');
      const overlayElement = document.querySelector('[data-testid="overlay"]');
      const positioner = overlayElement?.parentElement;
      if (!anchorElement || !overlayElement || !positioner) {
        throw new Error("定位回归测试缺少锚点或浮层元素");
      }
      const anchorRect = anchorElement.getBoundingClientRect();
      const overlayRect = overlayElement.getBoundingClientRect();
      return {
        anchor: {
          left: anchorRect.left,
          top: anchorRect.top,
          right: anchorRect.right,
          bottom: anchorRect.bottom,
        },
        overlay: {
          left: overlayRect.left,
          top: overlayRect.top,
          right: overlayRect.right,
          bottom: overlayRect.bottom,
          width: overlayRect.width,
          height: overlayRect.height,
        },
        visibility: getComputedStyle(positioner).visibility,
      };
    });
    result.anchor = measurements.anchor;
    result.overlay = measurements.overlay;

    if (measurements.visibility !== "visible") {
      throw new Error("浮层定位完成后仍不可见");
    }
    if (measurements.overlay.left <= 0 || measurements.overlay.top <= 0) {
      throw new Error(`浮层出现在左上角: ${JSON.stringify(measurements.overlay)}`);
    }
    if (
      measurements.overlay.right > result.viewport.width
      || measurements.overlay.bottom > result.viewport.height
    ) {
      throw new Error(`浮层超出视口: ${JSON.stringify(measurements.overlay)}`);
    }
    const alignsWithAnchor = Math.abs(
      measurements.overlay.left - measurements.anchor.left,
    ) <= 2 || Math.abs(
      measurements.overlay.right - measurements.anchor.right,
    ) <= 2;
    if (!alignsWithAnchor) {
      throw new Error(`浮层没有与锚点水平对齐或边界对齐: ${JSON.stringify(measurements)}`);
    }
    const verticalGap = Math.min(
      Math.abs(measurements.overlay.bottom - measurements.anchor.top),
      Math.abs(measurements.overlay.top - measurements.anchor.bottom),
    );
    if (verticalGap > 8) {
      throw new Error(`浮层没有贴近锚点: ${JSON.stringify(measurements)}`);
    }

    const treeLayoutBeforeNotice = await measureFileTreeLayout(page);
    result.fileTreeNotice = { layoutBefore: treeLayoutBeforeNotice };
    await secondFileRow.click({ button: "right" });
    await page.getByRole("menuitem", { name: "在系统中显示" }).click();
    const actionError = page.locator(".files-tree-action-error");
    await actionError.waitFor({ state: "visible", timeout: 10_000 });
    const notificationNext = page.locator(".files-tree-notification-next");
    await notificationNext.waitFor({ state: "visible", timeout: 10_000 });
    if (!(await notificationNext.innerText()).includes("1/2")) {
      throw new Error("并存提示没有显示当前条数和总数");
    }
    await notificationNext.click();
    const settingsError = page.locator(".files-tree-settings-error");
    await settingsError.waitFor({ state: "visible", timeout: 10_000 });
    const secondaryAlertText = await settingsError.innerText();
    if (!(await notificationNext.innerText()).includes("2/2")) {
      throw new Error("切换到第二条提示后计数没有更新");
    }
    await notificationNext.click();
    await actionError.waitFor({ state: "visible", timeout: 10_000 });
    const treeLayoutAfterNotice = await measureFileTreeLayout(page);
    const noticeLayout = await page.evaluate(() => {
      const alert = document.querySelector(".files-tree-action-error");
      if (!(alert instanceof HTMLElement)) {
        throw new Error("文件树操作失败后没有渲染通知容器和错误提示");
      }
      return alert.textContent?.trim() ?? "";
    });
    result.fileTreeNotice = {
      layoutBefore: treeLayoutBeforeNotice,
      layoutAfter: treeLayoutAfterNotice,
      alertText: noticeLayout,
      secondaryAlertText,
      notificationCounterText: await notificationNext.innerText(),
    };
    if (treeLayoutAfterNotice.notificationPosition !== "static") {
      throw new Error(`文件树提示没有处于固定预留布局位: ${JSON.stringify(result.fileTreeNotice)}`);
    }
    if (
      Math.abs(treeLayoutAfterNotice.pageScrollX - treeLayoutBeforeNotice.pageScrollX) > 0.25
      || Math.abs(treeLayoutAfterNotice.pageScrollY - treeLayoutBeforeNotice.pageScrollY) > 0.25
    ) {
      throw new Error(`文件树错误提示改变了页面滚动位置: ${JSON.stringify(result.fileTreeNotice)}`);
    }
    if (Math.abs(
      treeLayoutAfterNotice.notification.height
      - treeLayoutBeforeNotice.notification.height
    ) > 0.25) {
      throw new Error(`文件树提示位高度发生变化: ${JSON.stringify(result.fileTreeNotice)}`);
    }
    for (const boxName of ["host", "workspace", "notification", "tree", "row"]) {
      for (const coordinate of ["x", "y", "width", "height"]) {
        if (Math.abs(
          treeLayoutAfterNotice[boxName][coordinate]
          - treeLayoutBeforeNotice[boxName][coordinate]
        ) > 0.25) {
          throw new Error(
            `错误提示改变了文件树布局: ${boxName}.${coordinate} `
            + JSON.stringify(result.fileTreeNotice),
          );
        }
      }
    }
    if (!noticeLayout.includes("模拟服务器拒绝系统显示请求")) {
      throw new Error(`文件树没有展示后端错误: ${JSON.stringify(result.fileTreeNotice)}`);
    }

    const lastTreeRow = page.getByTitle(`${workspaceRoot}/entry-079.ts`, { exact: true });
    await lastTreeRow.scrollIntoViewIfNeeded();
    await lastTreeRow.waitFor({ state: "visible", timeout: 10_000 });
    const [notificationRowBounds, noticeBounds] = await Promise.all([
      lastTreeRow.boundingBox(),
      page.locator(".files-tree-notifications").boundingBox(),
    ]);
    if (!notificationRowBounds || !noticeBounds) {
      throw new Error("提示出现时无法测量末行和提示位");
    }
    const noticeOverlapsLastRow = notificationRowBounds.left < noticeBounds.x + noticeBounds.width
      && notificationRowBounds.x + notificationRowBounds.width > noticeBounds.x
      && notificationRowBounds.y < noticeBounds.y + noticeBounds.height
      && notificationRowBounds.y + notificationRowBounds.height > noticeBounds.y;
    if (noticeOverlapsLastRow) {
      throw new Error(`文件树提示遮挡末行: ${JSON.stringify({ notificationRowBounds, noticeBounds })}`);
    }
    await lastTreeRow.click();
    await page.waitForFunction(() => (
      document.querySelector('[data-testid="file-tree-status"]')?.textContent?.includes("entry-079.ts")
    ), undefined, { timeout: 10_000 });
    result.fileTreeNotice.lastRowClickable = true;

    await page.getByRole("button", { name: "关闭文件操作错误" }).click();
    await page.getByTestId("open-file-tree-search").click();
    const searchInput = page.getByRole("searchbox", { name: "筛选文件" });
    await searchInput.waitFor({ state: "visible", timeout: 10_000 });
    const fileTree = page.locator(".files-tree-root");
    const treeBeforeSearchStatus = await fileTree.boundingBox();
    if (!treeBeforeSearchStatus) {
      throw new Error("搜索状态出现前无法测量文件树范围");
    }
    await searchInput.fill("entry-");
    await page.waitForFunction(() => (
      document.querySelector(".files-tree-search-status")?.textContent?.includes("正在筛选：entry-")
    ), undefined, { timeout: 10_000 });
    const treeAfterSearchStatus = await fileTree.boundingBox();
    if (!treeAfterSearchStatus) {
      throw new Error("搜索状态出现后无法测量文件树范围");
    }
    const lastFilteredRow = page.getByTitle(`${workspaceRoot}/entry-079.ts`, { exact: true });
    await lastFilteredRow.scrollIntoViewIfNeeded();
    await lastFilteredRow.waitFor({ state: "visible", timeout: 10_000 });
    const [lastRowBounds, searchStatusBounds] = await Promise.all([
      lastFilteredRow.boundingBox(),
      page.locator(".files-tree-search-status").boundingBox(),
    ]);
    if (!lastRowBounds || !searchStatusBounds) {
      throw new Error("搜索结果末行或搜索状态缺少浏览器布局边界");
    }
    const searchStatusOverlapsLastRow = lastRowBounds.left < searchStatusBounds.right
      && lastRowBounds.right > searchStatusBounds.left
      && lastRowBounds.top < searchStatusBounds.bottom
      && lastRowBounds.bottom > searchStatusBounds.top;
    if (searchStatusOverlapsLastRow) {
      throw new Error("文件树搜索状态遮挡了末行: " + JSON.stringify({
        lastRowBounds,
        searchStatusBounds,
      }));
    }
    await lastFilteredRow.click();
    await page.waitForFunction(() => (
      document.querySelector('[data-testid="file-tree-status"]')?.textContent?.includes("entry-079.ts")
    ), undefined, { timeout: 10_000 });
    result.searchStatusLayout = {
      treeTopDeltaPx: treeAfterSearchStatus.y - treeBeforeSearchStatus.y,
      treeHeightDeltaPx: treeAfterSearchStatus.height - treeBeforeSearchStatus.height,
      searchStatusOverlapsLastRow,
      lastFilteredRowClicked: true,
    };
    if (
      Math.abs(result.searchStatusLayout.treeTopDeltaPx) > 0.25
      || Math.abs(result.searchStatusLayout.treeHeightDeltaPx) > 0.25
    ) {
      throw new Error(`输入搜索反馈推动了文件树: ${JSON.stringify(result.searchStatusLayout)}`);
    }
    if (unexpectedApiRequests.length > 0) {
      throw new Error(`集成场景出现未声明的 API 请求: ${unexpectedApiRequests.join(", ")}`);
    }

    await writeFile(resultPath, `${JSON.stringify(result, null, 2)}\n`, "utf8");
    await runContext.writeResult("passed", result);
  } catch (error) {
    await page.screenshot({ path: screenshotPath, fullPage: true });
    await runContext.writeResult("failed", {
      message: error instanceof Error ? error.message : String(error),
      browserErrors,
      apiRequests,
      apiResponses,
      unexpectedApiRequests,
      details: result,
      pageText: await page.locator("body").innerText().catch(() => "无法读取页面文本"),
      screenshotPath,
    });
    throw error;
  }
} finally {
  await runContext.close();
}
