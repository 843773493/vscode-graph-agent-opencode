import assert from "node:assert/strict";
import { writeFile } from "node:fs/promises";
import { chromium } from "playwright";
import { TestRunContext } from "../../../harness/js/run-context.mjs";

function required(name) {
  const value = process.env[name];
  if (!value) throw new Error(`缺少环境变量 ${name}`);
  return value;
}
const fixture = JSON.parse(required("BOXTEAM_BROWSER_FIXTURE"));
const scope = TestRunContext.fromTestFile("tests/integration/clients/web/test_attachment_directory.py");
const result = { classification: "Integration", modelBoundary: "model_stream", pageErrors: [], settingsFailures: [], settingsSaved: false, saved: false, reopened: false };
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
    if (new URL(response.url()).pathname === "/api/gateway/ui-settings" && response.status() >= 400) {
      result.settingsFailures.push(response.status());
    }
  });
  // 使用本次真实 Gateway 的访客入口，不拦截页面请求或伪造工作区状态。
  await page.goto(required("BOXTEAM_BROWSER_BASE_URL"), { waitUntil: "domcontentloaded" });
  const guest = await page.evaluate(async () => {
    const response = await fetch("/api/gateway/users/guest", {
      method: "POST", headers: { "Content-Type": "application/json", "X-Local-Token": "local-dev-token" },
      body: JSON.stringify({ tracking: { source: "attachment-directory-web" } }),
    });
    return { status: response.status, body: await response.text() };
  });
  assert.equal(guest.status, 200, guest.body);
  const openSession = async () => {
    const metadata = page.waitForResponse((response) => new URL(response.url()).pathname === "/api/v1/workspace");
    await page.goto(required("BOXTEAM_BROWSER_BASE_URL"), { waitUntil: "domcontentloaded" });
    const response = await metadata;
    assert.equal(response.status(), 200, await response.text());
    assert.equal(response.headers()["x-request-id"], (await response.json()).request_id);
    await page.getByRole("button", { name: "工作台视图", exact: true }).click();
    await page.getByRole("menuitemradio", { name: /会话工作台/ }).click();
    const workspace = page.getByTestId(`workspace-node-${fixture.workspaceId}`);
    const expand = workspace.getByRole("button", { name: /^展开工作区 / });
    if (await expand.count()) await expand.click();
    await page.locator(`button[data-session-id="${fixture.sessionId}"]`).click();
    await page.locator(`.chat-stream-virtual-shell[data-owner-session-id="${fixture.sessionId}"]`).waitFor({ state: "visible" });
  };
  await openSession();
  const card = page.locator(".message-attachment").filter({ hasText: fixture.attachment.name });
  await card.getByRole("button", { name: `在右侧打开附件：${fixture.attachment.name}`, exact: true }).click();
  const preview = page.getByRole("region", { name: `附件预览：${fixture.attachment.name}` });
  // section 有显式名称，按可访问角色取得真实附件预览。
  await preview.getByRole("button", { name: "添加附件目录快捷路径", exact: true }).click();
  await preview.getByText("附件目录已添加到当前会话文件树", { exact: true }).waitFor({ state: "visible" });
  const openResult = async () => {
    await page.getByRole("tab", { name: "文件", exact: true }).click();
    const tree = page.getByRole("tree", { name: "工作区文件树", exact: true });
    const shortcut = tree.locator("button.files-tree-quick-main").filter({ hasText: `附件：${fixture.attachment.name}` });
    await shortcut.waitFor({ state: "visible" });
    assert.equal(await shortcut.getAttribute("title"), fixture.directoryUri);
    if (await shortcut.getAttribute("aria-expanded") !== "true") await shortcut.click();
    const derived = tree.getByText("derived", { exact: true }).locator("..");
    if (await derived.locator(".codicon-chevron-down").count() === 0) await derived.click();
    await tree.getByText("result.txt", { exact: true }).click();
    const editor = page.locator("textarea.workspace-preview-text-editor");
    await editor.waitFor({ state: "visible" });
    return editor;
  };
  const savingSettings = page.waitForResponse((response) => response.request().method() === "PUT"
    && new URL(response.url()).pathname === "/api/gateway/ui-settings"
    && response.request().postDataJSON()?.layout?.workspace_preview_active_file_path === fixture.directoryUri + "/derived/result.txt");
  let editor = await openResult();
  const settings = await savingSettings;
  assert.equal(settings.status(), 200, await settings.text());
  result.settingsSaved = true;
  assert.equal(await editor.inputValue(), fixture.content);
  await editor.click();
  await editor.fill(fixture.edited);
  const saving = page.waitForResponse((response) => response.request().method() === "PUT"
    && new URL(response.url()).pathname === "/api/v1/workspace/files/content");
  await page.getByRole("button", { name: "保存", exact: true }).click();
  const saved = await saving;
  assert.equal(saved.status(), 200, await saved.text());
  assert.equal((await saved.json()).data.path, fixture.directoryUri + "/derived/result.txt");
  result.saved = true;
  await openSession();
  editor = await openResult();
  assert.equal(await editor.inputValue(), fixture.edited);
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
