// Integration：实际产品页面通过 Gateway 初始化；没有请求拦截或元数据替身。
import { writeFile } from "node:fs/promises";
import { chromium } from "playwright";
import { expect } from "playwright/test";
import { TestRunContext } from "../../../harness/js/run-context.mjs";

function required(name) {
  const value = process.env[name];
  if (!value) throw new Error(`缺少环境变量 ${name}`);
  return value;
}
function check(value, message) {
  if (!value) throw new Error(message);
}
const fixture = JSON.parse(required("BOXTEAM_BROWSER_FIXTURE"));
const result = { classification: "Integration", provider_boundary: "model_stream 精确回放，无模型调用",
  route_stubs: [], page_errors: [], console_errors: [], network: [], metadata: null,
  workspace_navigation_visible: false, settings_reload_rounds: [] };
const runContext = TestRunContext.fromTestFile("tests/integration/clients/web/test_workspace_metadata_bootstrap.py");
let page;
let executionError;
const pendingRequests = new Map();

async function openWorkspaceSettings() {
  await expect(page.locator("#workbench .monaco-workbench")).toBeVisible();
  await page.evaluate(async () => {
    await globalThis.workbench.commands.executeCommand("workbench.action.openWorkspaceSettingsFile");
  });
  const dialog = page.getByRole("dialog", { name: /settings\.json$/ });
  await expect(dialog).toBeVisible();
  const editor = dialog.locator(".monaco-editor");
  await expect(editor.locator(".view-lines")).toBeVisible();
  return editor;
}

async function verifyWorkspaceSettingsReload() {
  // 使用原版设置编辑器及保存链路；直接刷新，不预先 flush 或改写 IndexedDB。
  for (const fontSize of [16, 19, 17]) {
    const round = { fontSize, phase: "editing" };
    result.settings_reload_rounds.push(round);
    const editor = await openWorkspaceSettings();
    await editor.locator(".view-lines").click();
    await page.keyboard.press("ControlOrMeta+A");
    await page.keyboard.insertText(JSON.stringify({ "editor.fontSize": fontSize }, null, 2) + "\n");
    await expect(editor.locator(".view-lines")).toContainText(`"editor.fontSize": ${fontSize}`);
    await page.evaluate(async () => { await globalThis.workbench.commands.executeCommand("workbench.action.files.save"); });
    round.phase = "reloading";
    await page.reload({ waitUntil: "domcontentloaded" });
    const reopened = await openWorkspaceSettings();
    await expect(reopened.locator(".view-lines")).toContainText(`"editor.fontSize": ${fontSize}`);
    const line = reopened.locator(".view-line").first();
    await expect.poll(async () => {
      round.restoredFontSize = await line.evaluate(element => element.ownerDocument.defaultView
        .getComputedStyle(element).getPropertyValue("font-size"));
      return round.restoredFontSize;
    }).toBe(`${fontSize}px`);
    round.phase = "restored";
  }
}

async function verifyThemeBackgroundWarning() {
  const missingAsset = "0".repeat(64);
  const missingUrl = new URL(`/api/gateway/ui-assets/${missingAsset}`, required("BOXTEAM_BROWSER_BASE_URL")).href;
  await page.getByRole("button", { name: "添加工作区", exact: true }).click();
  await page.getByRole("dialog", { name: "选择工作区所在设备", exact: true })
    .getByRole("button", { name: "连接新的远程主机…", exact: true }).click();
  const control = page.getByRole("dialog", { name: "Gateway 控制中心", exact: true });
  await control.getByRole("button", { name: "主题设置", exact: true }).click();
  const backgroundInput = control.getByPlaceholder("https://example.com/background.webp", { exact: true });
  await backgroundInput.fill(missingUrl);
  const missingReady = page.waitForResponse(response => response.url() === missingUrl && response.request().method() === "GET");
  await control.getByRole("button", { name: "使用网络背景", exact: true }).click();
  const response = await missingReady;
  const payload = await response.json();
  check(response.status() === 404, `缺失背景图片没有返回真实 404: ${JSON.stringify(payload)}`);
  check(payload.request_id && response.headers()["x-request-id"] === payload.request_id, "背景图片 404 缺少原请求身份");
  check(payload.detail === `'Gateway UI 资源不存在: ${missingAsset}'`, "背景图片错误不属于指定资源");
  const warning = `主题背景图加载失败，已回退为无背景图：背景图片加载失败: ${missingUrl}`;
  const toast = page.locator(".notifications-toasts").getByText(warning, { exact: true });
  await expect(toast).toBeVisible();
  await expect(control.getByRole("status").filter({ hasText: "网络背景已保存。" })).toBeVisible();
  const hosts = page.locator(".boxteam-react-surface");
  check(await hosts.count() >= 2, "主题验收必须同时存在导航和会话宿主");
  await expect.poll(() => hosts.evaluateAll(nodes => nodes.every(node => getComputedStyle(node).backgroundImage === "none"))).toBe(true);
  result.theme_background_warning = { request_id: payload.request_id, asset_id: missingAsset, url: missingUrl,
    http_status: response.status(), notification: await toast.innerText(), hosts: await hosts.count() };
  await control.getByRole("button", { name: "使用主题默认背景", exact: true }).click();
  await expect(control.getByRole("status").filter({ hasText: "已恢复当前主题的默认背景。" })).toBeVisible();
  await expect(backgroundInput).toHaveValue("");
  await control.getByRole("button", { name: "关闭 Gateway 控制中心", exact: true }).click();
  const thread = page.locator(`section.boxteam-thread[data-workspace-id="${fixture.workspaceId}"][data-session-id="${fixture.nativeDirectory.sessionId}"][data-thread-id="${fixture.nativeDirectory.threadId}"]`);
  await expect(thread.getByTestId("composer-input")).toBeVisible();
}

async function verifyShutdownLogPersistence() {
  // 显式合成生命周期事件，让同一文档存活到延迟日志提交；上面的真实刷新仍独立验收。
  const readLogs = () => page.evaluate(async () => {
    const database = await new Promise((resolve, reject) => {
      const request = indexedDB.open("vscode-web-db");
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => reject(request.error);
      request.onupgradeneeded = () => request.transaction.abort();
    });
    try {
      return await new Promise((resolve, reject) => {
        const entries = [];
        const transaction = database.transaction("vscode-logs-store", "readonly");
        const cursor = transaction.objectStore("vscode-logs-store").openCursor();
        cursor.onsuccess = () => {
          if (!cursor.result) return;
          if (String(cursor.result.key).endsWith("/window.log")) entries.push({
            key: String(cursor.result.key), text: new TextDecoder().decode(cursor.result.value),
          });
          cursor.result.continue();
        };
        transaction.oncomplete = () => resolve(entries);
        transaction.onerror = () => reject(transaction.error);
        transaction.onabort = () => reject(transaction.error);
      });
    } finally {
      database.close();
    }
  });
  const marker = "[lifecycle] onBeforeUnload triggered and handled with veto support";
  const count = entries => entries.reduce((sum, entry) => sum + entry.text.split(marker).length - 1, 0);
  const before = count(await readLogs());
  result.controlled_shutdown = { boundary: "synthetic beforeunload/pagehide, real IndexedDB", before };
  await page.evaluate(() => {
    window.dispatchEvent(new Event("beforeunload"));
    window.dispatchEvent(new PageTransitionEvent("pagehide"));
  });
  await expect.poll(async () => {
    const logs = await readLogs();
    result.controlled_shutdown.after = count(logs);
    result.controlled_shutdown.keys = logs.map(entry => entry.key);
    return result.controlled_shutdown.after;
  }, { timeout: 5000 }).toBeGreaterThan(before);
  const storageErrors = result.console_errors.filter(({ text }) => /IndexedDB|database.*closed/i.test(text));
  check(storageErrors.length === 0, `关闭期间存储异常: ${JSON.stringify(storageErrors)}`);
}

try {
  const browser = await chromium.launch({
    executablePath: required("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH"), headless: true,
  });
  runContext.addCleanup("元数据 Chromium", () => browser.close());
  const context = await browser.newContext({ viewport: { width: 1600, height: 1000 } });
  runContext.addCleanup("元数据 context", () => context.close());
  page = await context.newPage();
  page.on("pageerror", (error) => result.page_errors.push(error.message));
  page.on("console", (message) => {
    if (message.type() === "error") result.console_errors.push({ text: message.text(), location: message.location() });
  });
  page.on("request", request => pendingRequests.set(request, { url: request.url(), startedAt: Date.now() }));
  page.on("requestfinished", request => pendingRequests.delete(request));
  page.on("requestfailed", (request) => {
    pendingRequests.delete(request);
    result.network.push({ url: request.url(), method: request.method(), failure: request.failure(), timing: request.timing() });
  });
  page.on("response", (response) => {
    if (new URL(response.url()).pathname.startsWith("/api/") || response.status() >= 400) result.network.push({
      url: response.url(), status: response.status(), timing: response.request().timing(),
    });
  });
  const [response] = await Promise.all([
    page.waitForResponse((response) => response.request().method() === "GET"
      && new URL(response.url()).pathname === "/api/v1/workspace", { timeout: 30_000 }),
    (async () => {
      await page.goto(new URL("/", required("BOXTEAM_BROWSER_BASE_URL")).href, { waitUntil: "domcontentloaded", timeout: 30_000 });
      await expect(page.locator("#workbench .monaco-workbench")).toBeVisible();
      // 工作台导航只加载目录；打开真实 Thread 后才加载它归属工作区的元数据。
      await page.locator(`button[data-session-id="${fixture.nativeDirectory.sessionId}"]`).dblclick();
    })(),
  ]);
  const payload = await response.json();
  check(response.status() === 200, `产品初始化元数据失败 ${response.status()}: ${JSON.stringify(payload)}`);
  check(response.headers()["x-request-id"] === payload.request_id, "产品初始化元数据 request_id 不一致");
  check(payload.data.workspace_id === fixture.workspaceId, "页面初始化取得了其他工作区的元数据");
  const metadataHeaders = await response.request().allHeaders();
  check(metadataHeaders["x-boxteam-workspace-id"] === fixture.workspaceId
    && metadataHeaders["x-boxteam-target-gateway-id"] === fixture.gatewayId, "页面初始化请求归属错误");
  check(!Object.hasOwn(payload.data, "root_path") && !Object.hasOwn(payload.data.git, "root"), "产品元数据仍包含重复路径");
  result.metadata = payload;
  const workspace = page.getByTestId(`workspace-node-${JSON.stringify([fixture.gatewayId, fixture.workspaceId])}`);
  await workspace.waitFor({ state: "visible", timeout: 30_000 });
  result.workspace_navigation_visible = true;
  const native = fixture.nativeDirectory;
  check(native, "缺少未注册目录验收输入");
  const openNativeFile = async () => {
    await page.locator(`button[data-session-id="${native.sessionId}"]`).dblclick();
    const thread = page.locator(`section.boxteam-thread[data-workspace-id="${fixture.workspaceId}"][data-session-id="${native.sessionId}"][data-thread-id="${native.threadId}"]`);
    await expect(thread).toBeVisible();
    const toggle = thread.getByRole("button", { name: "切换会话资源", exact: true });
    if (await toggle.getAttribute("aria-expanded") !== "true") await toggle.click();
    await thread.getByRole("tab", { name: "文件", exact: true }).click();
    const tree = thread.getByRole("tree", { name: "工作区文件树", exact: true });
    const shortcut = tree.locator("button.files-tree-quick-main").filter({ hasText: "未注册目录" });
    await shortcut.waitFor({ state: "visible", timeout: 30_000 });
    check(await shortcut.getAttribute("title") === native.rootUri, "快捷目录未保存URI");
    if (await shortcut.getAttribute("aria-expanded") !== "true") await shortcut.click();
    await tree.getByText(native.fileName, { exact: true }).click();
    const editor = thread.locator("textarea.workspace-preview-text-editor");
    await editor.waitFor({ state: "visible", timeout: 30_000 });
    return editor;
  };
  let editor = await openNativeFile();
  check(await editor.inputValue() === native.content, "未注册目录正文读取错误");
  await editor.click();
  await editor.fill(native.edited);
  const saved = page.waitForResponse((response) => response.request().method() === "PUT"
    && new URL(response.url()).pathname === "/api/v1/workspace/files/content");
  await page.getByRole("button", { name: "保存", exact: true }).click();
  const savedResponse = await saved;
  check(savedResponse.status() === 200, `原生目录编辑失败: ${await savedResponse.text()}`);
  const savedBody = await savedResponse.json();
  check(savedBody.data.path.startsWith(native.rootUri + "/"), "编辑响应丢失原生目录URI");
  await expect(editor).toHaveValue(native.edited);
  await page.evaluate(() => globalThis.boxteamWorkbenchLifecycle.prepareToClose());
  await page.reload({ waitUntil: "domcontentloaded" });
  editor = await openNativeFile();
  check(await editor.inputValue() === native.edited, "刷新后未从目标目录重开已编辑文件");
  result.native_directory_reopened = true;
  await verifyWorkspaceSettingsReload();
  await page.getByRole("button", { name: "Close Modal Editor (Escape)", exact: true }).click();
  editor = await openNativeFile();
  check(await editor.inputValue() === native.edited, "原版设置多次刷新后目标目录文件发生变化");
  await verifyThemeBackgroundWarning();
  await verifyShutdownLogPersistence();
  check(result.page_errors.length === 0, `页面异常: ${result.page_errors.join("; ")}`);
  const storageErrors = result.console_errors.filter(({ text }) => /IndexedDB|database.*closed/i.test(text));
  check(storageErrors.length === 0, `刷新期间存储异常: ${JSON.stringify(storageErrors)}`);
} catch (error) {
  executionError = error;
  result.error = String(error);
  result.pending_requests = [...pendingRequests.values()];
  throw error;
} finally {
  const failures = [];
  result.pending_requests ??= [...pendingRequests.values()];
  if (page) {
    try { result.page_text = await page.locator("body").innerText({ timeout: 5000 }); }
    catch (error) { failures.push(error); }
    try { await page.screenshot({ path: required("BOXTEAM_BROWSER_SCREENSHOT_PATH"), fullPage: true, timeout: 5000 }); }
    catch (error) { failures.push(error); }
  }
  try { await runContext.close(); }
  catch (error) { failures.push(error); }
  result.diagnostic_errors = failures.map(String);
  try { await writeFile(required("BOXTEAM_BROWSER_RESULT_PATH"), JSON.stringify(result, null, 2) + "\n"); }
  catch (error) { failures.push(error); }
  if (failures.length) throw new AggregateError(executionError ? [executionError, ...failures] : failures,
    "元数据页面执行、诊断或清理失败");
}
