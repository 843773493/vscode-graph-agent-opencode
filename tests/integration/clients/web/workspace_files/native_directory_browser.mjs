// Integration：完整生产页面、Gateway 与文件 owner；文件引用场景由本地 Provider 桩回显 Markdown。
import assert from "node:assert/strict";
import { dirname } from "node:path";
import { mkdir, open, writeFile } from "node:fs/promises";
import { chromium } from "playwright";

const fixture = JSON.parse(process.env.BOXTEAM_BROWSER_FIXTURE);
const output = process.env.BOXTEAM_BROWSER_RESULT_PATH;
assert.ok(output && process.env.BOXTEAM_BROWSER_BASE_URL);
const artifacts = dirname(output);
await mkdir(artifacts, { recursive: true });
const runtime = process.env.BOXTEAM_BROWSER_RUNTIME_PATH;
assert.ok(runtime);
await mkdir(runtime, { recursive: true });
// Linux Unix socket 长度有限；目录 fd 的短路径仍指向本测试的正式 runtime。
const runtimeHandle = process.platform === "linux" ? await open(runtime, "r") : null;
const temporary = runtimeHandle ? `/proc/${process.pid}/fd/${runtimeHandle.fd}` : runtime;
for (const key of ["TMPDIR", "TMP", "TEMP"]) process.env[key] = temporary;
const pageErrors = [], exchanges = [], pending = [];
let browser, context, page;
try {
  browser = await chromium.launch({ executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH, headless: true });
  context = await browser.newContext({ viewport: { width: 1920, height: 1100 } });
  await context.addCookies(fixture.cookies ?? []);
  page = await context.newPage();
  page.setDefaultTimeout(15_000);
  page.on("pageerror", (error) => pageErrors.push(String(error)));
  page.on("response", (response) => {
    if (/\/api\/v1\/(?:workspace\/files|sessions\/[^/]+\/file-tree)/.test(response.url())
        && response.headers()["content-type"]?.includes("application/json")) {
      pending.push((async () => {
        const body = await response.json();
        assert.equal(body.request_id, response.headers()["x-request-id"]);
        assert.ok(body.request_id);
        assert.ok(!JSON.stringify(body).includes(fixture.directory), "普通文件响应泄露原生绝对目录");
        exchanges.push({ url: response.url(), status: response.status(), body });
      })());
    }
  });
  const selectSession = async () => {
    const session = page.locator(`[data-session-id="${fixture.session_id}"]`);
    await session.first().waitFor({ state: "visible", timeout: 30_000 });
    await session.first().click();
    await page.getByRole("tab", { name: "文件", exact: true }).click();
    await page.locator(".files-tree-root > button.root").first().waitFor();
  };
  await page.goto(process.env.BOXTEAM_BROWSER_BASE_URL, { waitUntil: "domcontentloaded" });
  await selectSession();
  const editor = page.locator(".workspace-preview-text-editor:visible");
  const filePreferences = async (directoryUri) => {
    const credential = await context.request.get(`${process.env.BOXTEAM_BROWSER_BASE_URL}/api/gateway/auth/local-credential`);
    assert.equal(credential.status(), 200);
    const { data: { token } } = await credential.json();
    const deadline = Date.now() + 15_000;
    while (Date.now() < deadline) {
      const response = await context.request.get(`${process.env.BOXTEAM_BROWSER_BASE_URL}/api/gateway/ui-settings`, {
        headers: { "X-Local-Token": token },
      });
      assert.equal(response.status(), 200, await response.text());
      const body = await response.json();
      assert.ok(body.request_id);
      assert.equal(body.request_id, response.headers()["x-request-id"]);
      assert.ok(!JSON.stringify(body).includes(fixture.directory), "UI 设置响应泄露原生绝对目录");
      const { data } = body;
      const active = data.layout.workspace_preview_active_file_path;
      const expanded = Object.values(data.workspace_file_tree.expanded_paths_by_workspace).flat();
      if (active?.startsWith(directoryUri + "/") && data.layout.workspace_preview_file_paths.includes(active)
          && expanded.includes(directoryUri)) return data;
      await new Promise((resolve) => setTimeout(resolve, 50));
    }
    throw new Error("真实用户 profile 没有持久化文件预览及目录展开 URI");
  };
  if (fixture.references) {
    const opened = [];
    for (const reference of fixture.references) {
      const link = page.locator("button.chat-file-reference").filter({ hasText: reference.label });
      await link.waitFor();
      assert.ok((await link.getAttribute("title")).includes(reference.uri));
      assert.ok((await link.getAttribute("title")).includes("第 2 行"));
      await link.click();
      await editor.waitFor();
      assert.equal(await editor.inputValue(), reference.content);
      opened.push(reference.uri);
    }
    assert.equal(await page.locator("button.chat-file-reference").filter({ hasText: "缺失绝对" }).count(), 0);
    await page.locator(".chat-file-reference-fallback").filter({ hasText: "缺失绝对" }).waitFor();
    const inline = page.locator("button.chat-file-reference.inline-code");
    await inline.waitFor();
    await inline.click();
    assert.equal(await editor.inputValue(), fixture.references[0].content);
    await page.reload({ waitUntil: "domcontentloaded" });
    await selectSession();
    const reopened = page.locator("button.chat-file-reference").filter({ hasText: fixture.references[3].label });
    await reopened.waitFor();
    await reopened.click();
    await editor.waitFor();
    assert.equal(await editor.inputValue(), fixture.references[3].content);
    await Promise.all(pending);
    assert.ok(exchanges.some((item) => new URL(item.url).searchParams.get("path") === fixture.missing
      && item.status === 404));
    assert.ok(!exchanges.some((item) => item.status === 200
      && item.body.data?.path?.includes("不存在")));
    assert.deepEqual(pageErrors, []);
    await writeFile(output, JSON.stringify({ references: opened, page_errors: pageErrors, exchanges }, null, 2) + "\n");
  } else {
  let shortcut;
  if (fixture.shortcut_uri) {
    shortcut = { path: fixture.shortcut_uri };
  } else {
    const roots = page.locator(".files-tree-root > button.root");
    const workspaceRoot = roots.first(), filesystemRoot = roots.last();
    if (await workspaceRoot.getAttribute("aria-expanded") === "true") await workspaceRoot.click();
    await Promise.all(pending);
    const filesystemUri = exchanges.find((item) => new URL(item.url).pathname.endsWith("/files/roots")).body.data.filesystem_uri;
    const directoryResponse = (uri) => page.waitForResponse((response) => {
      const url = new URL(response.url());
      return response.request().method() === "GET" && url.pathname === "/api/v1/workspace/files"
        && url.searchParams.get("path") === uri;
    });
    assert.equal(await filesystemRoot.getAttribute("aria-expanded"), "false");
    let response = directoryResponse(filesystemUri);
    await filesystemRoot.click();
    let listing = (await (await response).json()).data;
    for (const segment of fixture.directory.split("/").filter(Boolean)) {
      const child = listing.items.find((item) => item.name === segment);
      assert.ok(child, `真实目录结果缺少 ${segment}`);
      response = directoryResponse(child.path);
      await page.getByTitle(child.path, { exact: true }).click();
      listing = (await (await response).json()).data;
    }
    const directoryUri = listing.path;
    const file = listing.items.find((item) => item.name === fixture.file_name);
    assert.ok(file);
    await page.getByTitle(file.path, { exact: true }).click();

    await editor.waitFor();
    assert.equal(await editor.inputValue(), fixture.original);
    await page.getByRole("button", { name: "编辑文件", exact: true }).click();
    await editor.fill(fixture.edited);
    const saveResponse = page.waitForResponse((response) => response.request().method() === "PUT"
      && new URL(response.url()).pathname === "/api/v1/workspace/files/content");
    await page.getByRole("button", { name: "保存", exact: true }).click();
    const saved = await saveResponse;
    assert.equal(saved.status(), 200, await saved.text());
    await page.getByTitle(directoryUri, { exact: true }).click({ button: "right" });
    const shortcutResponse = page.waitForResponse((response) => response.request().method() === "POST"
      && new URL(response.url()).pathname === `/api/v1/sessions/${fixture.session_id}/file-tree-shortcuts`);
    await page.getByRole("menuitem", { name: "添加到当前会话快捷路径", exact: true }).click();
    const added = await shortcutResponse;
    assert.equal(added.status(), 200, await added.text());
    const settings = (await added.json()).data;
    assert.equal(settings.session_shortcuts.length, 1);
    shortcut = settings.session_shortcuts[0];
    assert.ok(shortcut.path.startsWith("boxteam://gateway/"));
    if (fixture.persist_files) await filePreferences(shortcut.path);
    await page.reload({ waitUntil: "domcontentloaded" });
    await selectSession();
  }
  if (fixture.persist_files) {
    await editor.waitFor();
    assert.equal(await editor.inputValue(), fixture.edited, "页面应从用户 profile 自动恢复文件预览");
  }
  const shortcutRow = page.locator(".files-tree-shortcut .files-tree-quick-main");
  await shortcutRow.waitFor();
  assert.equal(await shortcutRow.getAttribute("title"), shortcut.path);
  if (await shortcutRow.getAttribute("aria-expanded") !== "true") await shortcutRow.click();
  await page.locator(".files-tree-shortcut").locator("button.files-tree-item").filter({
    has: page.locator(".file-label").getByText(fixture.file_name, { exact: true }),
  }).click();
  await editor.waitFor();
  assert.equal(await editor.inputValue(), fixture.edited);
  await Promise.all(pending);
  assert.deepEqual(pageErrors, []);
  const preferences = fixture.persist_files ? await filePreferences(shortcut.path) : null;
  await writeFile(output, JSON.stringify({ shortcut_uri: shortcut.path, reopened_content: await editor.inputValue(),
    file_preferences: preferences, page_errors: pageErrors, exchanges }, null, 2) + "\n");
  }
  await page.screenshot({ path: `${artifacts}/native-directory-browser.png` });
} catch (error) {
  await Promise.allSettled(pending);
  await writeFile(`${artifacts}/native-directory-failure-exchanges.json`, JSON.stringify({ page_errors: pageErrors, exchanges }, null, 2) + "\n");
  if (page) {
    await writeFile(`${artifacts}/native-directory-failure.html`, await page.content());
    await page.screenshot({ path: `${artifacts}/native-directory-failure.png` });
  }
  throw error;
} finally {
  try { if (context) await context.close(); }
  finally {
    try { if (browser) await browser.close(); }
    finally { if (runtimeHandle) await runtimeHandle.close(); }
  }
}
