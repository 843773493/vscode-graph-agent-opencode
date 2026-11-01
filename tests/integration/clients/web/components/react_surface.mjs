import assert from "node:assert/strict";
import { mkdir, writeFile } from "node:fs/promises";
import { dirname, join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { chromium } from "playwright";
import { expect } from "playwright/test";
import { TestRunContext } from "../../../../harness/js/run-context.mjs";

const file = fileURLToPath(import.meta.url);
const scope = await TestRunContext.fromTestFile(file).prepare();
const runtime = join(scope.outputRoot, "runtime");
await mkdir(runtime, { recursive: true });
for (const name of ["TMPDIR", "TMP", "TEMP"]) process.env[name] = runtime;
const observations = { classification: "Integration: 真实 ReactSurface 组件宿主，文件根、授权及主题图片为显式 HTTP 替身，通知使用装具 collector，无 Gateway 或业务后端", errors: [], requests: [], apiRequests: [], themeRequests: [] };
let page;
let failure;
const { createServer } = await import(pathToFileURL(join(scope.projectRoot, "src/clients/web/node_modules/vite/dist/node/index.js")).href);

try {
  const vite = await createServer({
    configFile: false, root: dirname(file), cacheDir: join(runtime, "vite-cache"), logLevel: "error",
    plugins: [{ name: "react-surface-text-styles", enforce: "pre", async resolveId(id, importer) {
      if (id.endsWith(".css?text")) return this.resolve(id.replace(/\?text$/, "?inline"), importer, { skipSelf: true });
    } }],
    resolve: { dedupe: ["react", "react-dom"], alias: {
      react: join(scope.projectRoot, "src/clients/web/node_modules/react"),
      "react-dom": join(scope.projectRoot, "src/clients/web/node_modules/react-dom"),
    } },
    esbuild: { jsx: "automatic" },
    // 首次挂载前固定共享 React 依赖，避免迟到预构建使同页持有两份模块实例。
    optimizeDeps: { entries: [join(dirname(file), "react_surface_harness.html")],
      include: ["react", "react-dom", "react-dom/client", "react/jsx-runtime", "react/jsx-dev-runtime"] },
    server: { host: "127.0.0.1", port: 0, fs: { allow: [scope.projectRoot] } },
  });
  scope.addCleanup("ReactSurface Vite", () => vite.close());
  await vite.listen();
  const address = vite.httpServer?.address();
  assert.ok(address && typeof address !== "string");
  const browser = await chromium.launch({ executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH || undefined, headless: true });
  scope.addCleanup("ReactSurface Chromium", () => browser.close());
  const context = await browser.newContext({ viewport: { width: 1200, height: 800 } });
  scope.addCleanup("ReactSurface BrowserContext", () => context.close());
  page = await context.newPage();
  page.on("pageerror", error => observations.errors.push({ kind: "page", message: error.message }));
  page.on("console", message => {
    if (message.type() === "error") observations.errors.push({ kind: "console", message: message.text(), url: message.location().url });
  });
  page.on("requestfailed", request => observations.requests.push({ url: request.url(), error: request.failure()?.errorText }));
  const pendingImages = new Map();
  const completedImages = [];
  const svg = '<svg xmlns="http://www.w3.org/2000/svg" width="2" height="2"><path fill="red" d="M0 0h2v2H0z"/></svg>';
  await page.route("**/fixture-theme/*", async route => {
    const pathname = new URL(route.request().url()).pathname;
    assert.equal(route.request().method(), "GET");
    const filename = pathname.slice("/fixture-theme/".length);
    assert.ok(["missing.svg", "current.svg", "old-success.svg", "old-failure.svg", "disposed.svg"].includes(filename), `未声明的主题图片 ${pathname}`);
    observations.themeRequests.push(pathname);
    if (filename.startsWith("old-") || filename === "disposed.svg") {
      await new Promise(resolve => pendingImages.set(filename, resolve));
    }
    await route.fulfill({ status: filename === "missing.svg" ? 404 : 200,
      contentType: filename === "missing.svg" ? "application/json" : "image/svg+xml",
      body: filename === "missing.svg" ? JSON.stringify({ detail: "fixture-theme-image-not-found" })
        : filename === "old-failure.svg" ? "invalid-image" : svg });
    completedImages.push(filename);
  });
  await page.route("**/api/**", async route => {
    const request = route.request();
    const pathname = new URL(request.url()).pathname;
    if (!pathname.startsWith("/api/")) { await route.continue(); return; }
    const declared = request.method() === "GET";
    const data = declared && pathname === "/api/gateway/auth/local-credential" ? { token: "fixture-surface-token" }
      : declared && pathname === "/api/gateway/users/current" ? { kind: "guest", user_id: null }
      : declared && pathname === "/api/v1/workspace/files/roots" ? {
        workspace_uri: "boxteam://workspace/backend_surface/resources/files/root",
        filesystem_uri: "boxteam://gateway/gateway_surface/resources/files/root", filesystem_label: "装具文件系统",
      } : null;
    observations.apiRequests.push({ method: request.method(), pathname, declared: data !== null });
    await route.fulfill({ status: data ? 200 : 501, contentType: "application/json",
      body: JSON.stringify(data ? { code: 0, message: "ok", request_id: "req-surface", data } : { detail: "组件场景未声明此 API" }) });
  });
  await page.goto(`http://127.0.0.1:${address.port}/react_surface_harness.html`, { waitUntil: "networkidle", timeout: 30_000 });
  await page.waitForFunction(() => typeof window.reactSurfaceHarness?.mount === "function", null, { timeout: 30_000 });
  const outsideColors = () => page.locator("#outside").evaluate(element => Array.from(element.querySelectorAll("[data-bt-surface],.auxiliary-view-body"), node => getComputedStyle(node).backgroundColor));
  const before = await outsideColors();
  assert.deepEqual(before, ["rgb(21, 31, 41)", "rgb(21, 31, 41)", "rgb(21, 31, 41)"]);
  await page.evaluate(() => window.reactSurfaceHarness.mount());
  const first = page.locator("#first");
  const second = page.locator("#second");
  await expect(first.getByRole("button", { name: "first 计数 0", exact: true })).toBeVisible();
  await expect(second.getByRole("button", { name: "second 计数 0", exact: true })).toBeVisible();
  assert.deepEqual(await outsideColors(), before, "产品样式泄漏到了外层工作台");
  observations.styles = await first.locator(".boxteam-react-surface").evaluate(host => {
    const root = host.shadowRoot;
    const color = selector => {
      const node = root?.querySelector(selector);
      if (!node) throw new Error(`样式检查缺少正向节点 ${selector}`);
      return getComputedStyle(node).backgroundColor;
    };
    return { layout: color('[data-bt-surface="layout"]'), panel: color('[data-bt-surface="panel"]'),
      expectedPanel: color('[data-testid="expected-panel"]'), auxiliary: color(".auxiliary-view-body") };
  });
  assert.equal(observations.styles.layout, "rgba(0, 0, 0, 0)");
  assert.equal(observations.styles.auxiliary, "rgba(0, 0, 0, 0)");
  assert.notEqual(observations.styles.panel, "rgba(0, 0, 0, 0)", "正向面板样式未加载");
  assert.equal(observations.styles.panel, observations.styles.expectedPanel);
  observations.resourceTabs = [];
  for (const tab of ["files", "changes", "goal", "resources", "debug"]) {
    await first.getByRole("combobox", { name: "first 资源标签", exact: true }).selectOption(tab);
    const bodies = first.locator(".auxiliary-panel > .auxiliary-view-body");
    await expect(bodies).toHaveCount(5);
    await expect(first.locator(".auxiliary-panel > .auxiliary-view-body:not([hidden])")).toHaveCount(1);
    const colors = await bodies.evaluateAll(elements => elements.map(element => ({
      surface: element.getAttribute("data-bt-surface"), background: getComputedStyle(element).backgroundColor,
    })));
    assert.deepEqual(colors, Array.from({ length: 5 }, () => ({ surface: "layout", background: "rgba(0, 0, 0, 0)" })));
    observations.resourceTabs.push({ tab, colors });
  }
  await first.getByRole("button", { name: "first 浮层", exact: true }).click();
  await expect(first.locator(".react-session-overlays").getByRole("note")).toHaveText("first 的浮层");
  await expect(second.getByRole("note")).toHaveCount(0);
  await page.evaluate(() => window.reactSurfaceHarness.focus());
  await expect(first.getByRole("textbox", { name: "first 输入", exact: true })).toBeFocused();
  await second.getByRole("button", { name: "second 计数 0", exact: true }).click();
  assert.equal(observations.errors.length, 0, JSON.stringify(observations.errors));

  let classifiedErrorCount = 0;
  for (const [method, expected, component, count] of [
    ["failContext", "useAppState must be used within AppProvider", "ProviderLessConsumer", 1],
    ["failProvider", "fixture-provider-initialization-failed", "FailingProvider", 2],
  ]) {
    const start = observations.errors.length;
    await page.evaluate(method => window.reactSurfaceHarness[method](), method);
    await expect(first.getByRole("alert")).toContainText(expected);
    await expect(first.getByRole("button", { name: "刷新页面", exact: true })).toBeVisible();
    await expect(first.getByRole("note")).toHaveCount(0);
    await second.getByRole("button", { name: `second 计数 ${count}`, exact: true }).click();
    await expect(second.getByRole("button", { name: `second 计数 ${count + 1}`, exact: true })).toBeVisible();
    const errors = observations.errors.slice(start);
    assert.ok(errors.some(error => error.kind === "console" && error.message.includes("BoxTeam 前端渲染失败") && error.message.includes(expected)));
    for (const error of errors) {
      assert.ok(error.message.includes(expected)
        || (error.kind === "console" && error.message.startsWith(`The above error occurred in the <${component}> component:`)), JSON.stringify(error));
    }
    classifiedErrorCount += errors.length;
    await page.evaluate(() => window.reactSurfaceHarness.recover());
    await expect(first.getByRole("button", { name: "first 计数 0", exact: true })).toBeVisible();
    await expect(first.getByRole("alert")).toHaveCount(0);
    await expect(second.getByRole("button", { name: `second 计数 ${count + 1}`, exact: true })).toBeVisible();
  }
  const theme = (id, background) => ({ id, color_scheme: "light", tokens: { "--bt-page-background": "#123456" },
    background_image_url: background ? `/fixture-theme/${background}` : null });
  const applyTheme = (name, value) => page.evaluate(({ name, value }) => window.reactSurfaceHarness.theme(name, value), { name, value });
  const themeSettled = id => page.evaluate(id => window.reactSurfaceHarness.themeSettled(id), id);
  const firstHost = first.locator(".boxteam-react-surface");
  await applyTheme("first", theme("missing", "missing.svg"));
  await expect(page.getByRole("complementary", { name: "装具主题通知", exact: true }).getByRole("status"))
    .toContainText("主题背景图加载失败，已回退为无背景图");
  await expect(firstHost).toHaveAttribute("data-boxteam-theme", "missing");
  assert.equal(await firstHost.evaluate(host => getComputedStyle(host).backgroundImage), "none");
  assert.equal(await firstHost.evaluate(host => host.style.getPropertyValue("--bt-page-background")), "#123456");
  for (const filename of ["old-success.svg", "old-failure.svg"]) {
    await applyTheme("first", theme(filename, filename));
    await expect.poll(() => pendingImages.has(filename)).toBe(true);
    await applyTheme("first", theme("current", "current.svg"));
    await expect(firstHost).toHaveAttribute("data-boxteam-theme", "current");
    pendingImages.get(filename)();
    await expect.poll(() => completedImages.includes(filename)).toBe(true);
    await themeSettled(filename);
    await expect(firstHost).toHaveAttribute("data-boxteam-theme", "current");
    assert.ok((await firstHost.evaluate(host => getComputedStyle(host).backgroundImage)).endsWith('/fixture-theme/current.svg")'));
  }
  await applyTheme("second", theme("second", null));
  await expect(second.locator(".boxteam-react-surface")).toHaveAttribute("data-boxteam-theme", "second");
  await expect(firstHost).toHaveAttribute("data-boxteam-theme", "current");
  const detachedHost = await firstHost.elementHandle();
  assert.ok(detachedHost);
  await applyTheme("first", theme("disposed", "disposed.svg"));
  await expect.poll(() => pendingImages.has("disposed.svg")).toBe(true);
  const beforeDispose = await page.evaluate(() => window.reactSurfaceHarness.events);
  await page.evaluate(() => window.reactSurfaceHarness.dispose());
  pendingImages.get("disposed.svg")();
  await expect.poll(() => completedImages.includes("disposed.svg")).toBe(true);
  await themeSettled("disposed");
  assert.equal(await detachedHost.evaluate(host => host.dataset.boxteamTheme), "current");
  observations.themeNotifications = await page.evaluate(() => window.reactSurfaceHarness.themeNotifications);
  assert.equal(observations.themeNotifications.length, 1, "过期或已释放宿主不得发布主题通知");
  assert.equal(observations.themeNotifications[0].kind, "warn");
  assert.ok(observations.themeNotifications[0].message.endsWith("/fixture-theme/missing.svg"));
  const themeErrors = observations.errors.slice(classifiedErrorCount);
  assert.equal(themeErrors.length, 1);
  assert.equal(themeErrors[0].kind, "console");
  assert.equal(themeErrors[0].message, "Failed to load resource: the server responded with a status of 404 (Not Found)");
  assert.equal(themeErrors[0].url, `http://127.0.0.1:${address.port}/fixture-theme/missing.svg`);
  classifiedErrorCount += themeErrors.length;
  await expect(first.locator(".boxteam-react-surface")).toHaveCount(0);
  const afterDispose = await page.evaluate(() => window.reactSurfaceHarness.events);
  assert.deepEqual(afterDispose, [...beforeDispose, "first:unmounted"]);
  assert.equal(afterDispose.filter(event => event === "second:mounted").length, 1);
  assert.equal(afterDispose.filter(event => event === "second:unmounted").length, 0);
  assert.deepEqual(await outsideColors(), before);
  assert.equal(observations.errors.length, classifiedErrorCount, "存在未归因的浏览器错误");
  assert.deepEqual(observations.requests, []);
  assert.ok(observations.apiRequests.every(request => request.declared), JSON.stringify(observations.apiRequests));
  observations.lifecycle = afterDispose;
} catch (error) {
  failure = error;
  if (page) {
    try { observations.themeState = await page.evaluate(() => ({
      notifications: window.reactSurfaceHarness?.themeNotifications,
      hosts: Array.from(document.querySelectorAll(".boxteam-react-surface"), host => ({
        theme: host.dataset.boxteamTheme, background: getComputedStyle(host).backgroundImage,
      })),
    })); }
    catch (diagnosticError) { failure = new AggregateError([failure, diagnosticError], "场景和主题诊断均失败"); }
    try { await page.screenshot({ path: join(scope.artifactsDir, "failure.png"), fullPage: true }); }
    catch (screenshotError) { failure = new AggregateError([failure, screenshotError], "场景和截图均失败"); }
  }
} finally {
  try { await scope.close(); }
  catch (cleanupError) { failure = failure ? new AggregateError([failure, cleanupError], "场景和清理均失败") : cleanupError; }
}
await writeFile(join(scope.artifactsDir, "observations.json"), JSON.stringify(observations, null, 2) + "\n");
await scope.writeResult(failure ? "failed" : "passed", { ...observations, error: failure ? String(failure) : null });
if (failure) throw failure;
