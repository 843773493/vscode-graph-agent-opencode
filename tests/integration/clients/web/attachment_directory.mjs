import assert from "node:assert/strict";
import { writeFile } from "node:fs/promises";
import { chromium } from "playwright";
import { expect } from "playwright/test";
import { TestRunContext } from "../../../harness/js/run-context.mjs";

function required(name) {
  const value = process.env[name];
  if (!value) throw new Error(`缺少环境变量 ${name}`);
  return value;
}
const fixture = JSON.parse(required("BOXTEAM_BROWSER_FIXTURE"));
const scope = TestRunContext.fromTestFile("tests/integration/clients/web/test_attachment_directory.py");
const result = { classification: "Integration", modelBoundary: "model_stream", pageErrors: [], settingsFailures: [], viewRestored: false, saved: false, reopened: false };
let page;
let failure;
try {
  const browser = await chromium.launch({ executablePath: required("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH"), headless: true });
  scope.addCleanup("附件目录 Chromium", () => browser.close());
  const context = await browser.newContext({ viewport: { width: 1800, height: 1100 } });
  scope.addCleanup("附件目录 context", () => context.close());
  page = await context.newPage();
  page.on("pageerror", (error) => result.pageErrors.push(error.message));
  page.on("response", (response) => {
    if (/\/file-tree-(?:settings|shortcuts)$/.test(new URL(response.url()).pathname) && response.status() >= 400) {
      result.settingsFailures.push(response.status());
    }
  });
  // 使用本次真实 Gateway 的访客入口，不拦截页面请求或伪造工作区状态。
  const target = new URL("/", required("BOXTEAM_BROWSER_BASE_URL"));
  await page.goto(target.href, { waitUntil: "domcontentloaded" });
  const guest = await page.evaluate(async () => {
    const credential = await fetch("/api/gateway/auth/local-credential");
    if (!credential.ok) throw new Error("读取本地凭证失败");
    const token = (await credential.json()).data.token;
    const response = await fetch("/api/gateway/users/guest", {
      method: "POST", headers: { "Content-Type": "application/json", "X-Local-Token": token },
      body: JSON.stringify({ tracking: { source: "attachment-directory-web" } }),
    });
    return { status: response.status, body: await response.text() };
  });
  assert.equal(guest.status, 200, guest.body);
  const thread = page.locator(`section.boxteam-thread[data-session-id="${fixture.sessionId}"][data-thread-id="${fixture.threadId}"]`);
  const metadata = page.waitForResponse((response) => new URL(response.url()).pathname === "/api/v1/workspace");
  await page.goto(target.href, { waitUntil: "domcontentloaded" });
  await expect(page.locator("#workbench .monaco-workbench")).toBeVisible();
  await page.locator(`button[data-session-id="${fixture.sessionId}"]`).dblclick();
  await expect(thread).toBeVisible();
  const response = await metadata;
  assert.equal(response.status(), 200, await response.text());
  assert.equal(response.headers()["x-request-id"], (await response.json()).request_id);
  await thread.locator(`.chat-stream-virtual-shell[data-owner-session-id="${fixture.sessionId}"]`).waitFor({ state: "visible" });
  const card = thread.locator(".message-attachment").filter({ hasText: fixture.attachment.name });
  await card.getByRole("button", { name: `在右侧打开附件：${fixture.attachment.name}`, exact: true }).click();
  const preview = thread.getByRole("region", { name: `附件预览：${fixture.attachment.name}` });
  // section 有显式名称，按可访问角色取得真实附件预览。
  await preview.getByRole("button", { name: "添加附件目录快捷路径", exact: true }).click();
  await preview.getByText("附件目录已添加到当前会话文件树", { exact: true }).waitFor({ state: "visible" });
  const openResult = async () => {
    await thread.getByRole("tab", { name: "文件", exact: true }).click();
    const tree = thread.getByRole("tree", { name: "工作区文件树", exact: true });
    const shortcut = tree.locator("button.files-tree-quick-main").filter({ hasText: `附件：${fixture.attachment.name}` });
    await shortcut.waitFor({ state: "visible" });
    assert.equal(await shortcut.getAttribute("title"), fixture.directoryUri);
    if (await shortcut.getAttribute("aria-expanded") !== "true") await shortcut.click();
    const derived = tree.getByText("derived", { exact: true }).locator("..");
    if (await derived.locator(".codicon-chevron-down").count() === 0) await derived.click();
    await tree.getByText("result.txt", { exact: true }).click();
    const editor = thread.locator("textarea.workspace-preview-text-editor:visible");
    await editor.waitFor({ state: "visible" });
    return editor;
  };
  const editor = await openResult();
  assert.equal(await editor.inputValue(), fixture.content);
  await thread.getByRole("button", { name: "编辑文件", exact: true }).click();
  await editor.click();
  await editor.fill(fixture.edited);
  const saving = page.waitForResponse((response) => response.request().method() === "PUT"
    && new URL(response.url()).pathname === "/api/v1/workspace/files/content");
  await thread.getByRole("button", { name: "保存", exact: true }).click();
  const saved = await saving;
  assert.equal(saved.status(), 200, await saved.text());
  assert.equal((await saved.json()).data.path, fixture.directoryUri + "/derived/result.txt");
  result.saved = true;
  assert.equal((await saved.request().allHeaders())["x-boxteam-workspace-id"], fixture.workspaceId);
  await page.evaluate(() => globalThis.boxteamWorkbenchLifecycle.prepareToClose());
  const restoredRead = page.waitForResponse((response) => {
    const url = new URL(response.url());
    return response.request().method() === "GET" && url.pathname === "/api/v1/workspace/files/content"
      && url.searchParams.get("path") === fixture.directoryUri + "/derived/result.txt";
  });
  await page.reload({ waitUntil: "domcontentloaded" });
  await expect(thread).toBeVisible();
  await expect(thread.getByRole("tab", { name: "文件", exact: true })).toHaveAttribute("aria-selected", "true");
  await expect(editor).toHaveValue(fixture.edited);
  const restored = await restoredRead;
  assert.equal(restored.status(), 200, await restored.text());
  assert.equal((await restored.json()).data.content, fixture.edited);
  result.viewRestored = true;
  result.reopened = true;
  assert.deepEqual(result.pageErrors, []);
  assert.deepEqual(result.settingsFailures, []);
} catch (error) {
  failure = error;
} finally {
  const errors = failure ? [failure] : [];
  try {
    if (page) await page.screenshot({ path: required("BOXTEAM_BROWSER_SCREENSHOT_PATH"), fullPage: true });
    await writeFile(required("BOXTEAM_BROWSER_RESULT_PATH"), JSON.stringify(result, null, 2) + "\n");
  } catch (error) { errors.push(error); }
  try { await scope.close(); } catch (error) { errors.push(error); }
  if (errors.length) throw new AggregateError(errors, "附件目录 Web 验收失败");
}
