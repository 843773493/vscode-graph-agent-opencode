// Integration：完整生产页面、Gateway 与文件 owner；文件引用场景由本地 Provider 桩回显 Markdown。
import assert from "node:assert/strict";
import { dirname, join } from "node:path";
import { mkdir, writeFile } from "node:fs/promises";
import { chromium } from "playwright";
import { expect } from "playwright/test";

import { configureChromiumTemporaryDirectory } from "./chromium-temporary-directory.mjs";

const fixture = JSON.parse(process.env.BOXTEAM_BROWSER_FIXTURE);
const output = process.env.BOXTEAM_BROWSER_RESULT_PATH;
assert.ok(output && process.env.BOXTEAM_BROWSER_BASE_URL);
const artifacts = dirname(output);
await mkdir(artifacts, { recursive: true });
const runtime = process.env.BOXTEAM_BROWSER_RUNTIME_PATH;
assert.ok(runtime);
const releaseChromiumTemporaryDirectory = await configureChromiumTemporaryDirectory(runtime);
const pageErrors = [], exchanges = [], pending = [];
let context, page;
try {
  context = await chromium.launchPersistentContext(join(runtime, `profile-${fixture.session_id}`), {
    executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH, headless: true,
    viewport: { width: 1920, height: 1100 },
  });
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
  const thread = page.locator(`section.boxteam-thread[data-session-id="${fixture.session_id}"]`);
  const selectSession = async () => {
    await page.locator(`button[data-session-id="${fixture.session_id}"]`).dblclick();
    await expect(thread).toBeVisible();
    const toggle = thread.getByRole("button", { name: "切换会话资源", exact: true });
    if (await toggle.getAttribute("aria-expanded") !== "true") await toggle.click();
    await thread.getByRole("tab", { name: "文件", exact: true }).click();
    await thread.locator(".files-tree-root button.root").first().waitFor();
  };
  const revealTreeRow = async (row) => {
    const scroller = thread.locator(".files-tree-virtualized");
    if (await row.count() === 0 && await scroller.count() > 0) {
      await scroller.evaluate((element) => element.scrollTo({ top: 0, behavior: "instant" }));
      await scroller.hover();
      await page.waitForTimeout(50);
      const deadline = Date.now() + 15_000;
      while (await row.count() === 0 && Date.now() < deadline) {
        // 大目录只挂载可视行；像用户一样逐屏滚动，保留点击和真实 HTTP 验证。
        await page.mouse.wheel(0, await scroller.evaluate((element) => Math.max(1, element.clientHeight * 0.75)));
        await page.waitForTimeout(50);
      }
    }
    await row.waitFor({ state: "visible" });
    return row;
  };
  await page.goto(new URL("/", process.env.BOXTEAM_BROWSER_BASE_URL).href, { waitUntil: "domcontentloaded" });
  await expect(page.locator("#workbench .monaco-workbench")).toBeVisible();
  await selectSession();
  const editor = thread.locator(".workspace-preview-text-editor:visible");
  const flushView = () => page.evaluate(() => globalThis.boxteamWorkbenchLifecycle.prepareToClose());
  const verifyNativeContent = async (content) => {
    const editor = page.locator(".monaco-editor:visible");
    await expect(editor).toBeVisible();
    await expect.poll(async () => (await editor.locator(".view-line").allTextContents())
      .join("\n").replaceAll("\u00a0", " ").trimEnd()).toBe(content.trimEnd());
  };
  if (fixture.references) {
    const opened = [];
    for (const reference of fixture.references) {
      await selectSession();
      const link = thread.locator("button.chat-file-reference").filter({ hasText: reference.label });
      await link.waitFor();
      assert.ok((await link.getAttribute("title")).includes(reference.uri));
      assert.ok((await link.getAttribute("title")).includes("第 2 行"));
      await link.click();
      await verifyNativeContent(reference.content);
      opened.push(reference.uri);
    }
    await selectSession();
    assert.equal(await thread.locator("button.chat-file-reference").filter({ hasText: "缺失绝对" }).count(), 0);
    await thread.locator(".chat-file-reference-fallback").filter({ hasText: "缺失绝对" }).waitFor();
    const inline = thread.locator("button.chat-file-reference.inline-code");
    await inline.waitFor();
    await inline.click();
    await verifyNativeContent(fixture.references[0].content);
    await flushView();
    await page.reload({ waitUntil: "domcontentloaded" });
    await selectSession();
    const reopened = thread.locator("button.chat-file-reference").filter({ hasText: fixture.references[3].label });
    await reopened.waitFor();
    await reopened.click();
    await verifyNativeContent(fixture.references[3].content);
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
    const roots = thread.locator(".files-tree-root button.root");
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
      await (await revealTreeRow(thread.getByTitle(child.path, { exact: true }))).click();
      listing = (await (await response).json()).data;
    }
    const directoryUri = listing.path;
    const file = listing.items.find((item) => item.name === fixture.file_name);
    assert.ok(file);
    await (await revealTreeRow(thread.getByTitle(file.path, { exact: true }))).click();

    await editor.waitFor();
    assert.equal(await editor.inputValue(), fixture.original);
    await thread.getByRole("button", { name: "编辑文件", exact: true }).click();
    await editor.fill(fixture.edited);
    const saveResponse = page.waitForResponse((response) => response.request().method() === "PUT"
      && new URL(response.url()).pathname === "/api/v1/workspace/files/content");
    await thread.getByRole("button", { name: "保存", exact: true }).click();
    const saved = await saveResponse;
    assert.equal(saved.status(), 200, await saved.text());
    await (await revealTreeRow(thread.getByTitle(directoryUri, { exact: true }))).click({ button: "right" });
    const shortcutResponse = page.waitForResponse((response) => response.request().method() === "POST"
      && new URL(response.url()).pathname === `/api/v1/sessions/${fixture.session_id}/file-tree-shortcuts`);
    await thread.getByRole("menuitem", { name: "添加到当前会话快捷路径", exact: true }).click();
    const added = await shortcutResponse;
    assert.equal(added.status(), 200, await added.text());
    const settings = (await added.json()).data;
    assert.equal(settings.session_shortcuts.length, 1);
    shortcut = settings.session_shortcuts[0];
    assert.ok(shortcut.path.startsWith("boxteam://gateway/"));
    await expect(editor).toHaveValue(fixture.edited);
    await flushView();
    await page.reload({ waitUntil: "domcontentloaded" });
    await selectSession();
  }
  if (fixture.persist_files) {
    await editor.waitFor();
    await expect(editor).toHaveValue(fixture.edited);
  }
  const shortcutRow = await revealTreeRow(thread.getByTitle(shortcut.path, { exact: true }).and(thread.locator("button.root")));
  assert.equal(await shortcutRow.getAttribute("title"), shortcut.path);
  // 冷启动必须直接恢复展开状态；不能靠测试重新展开而掩盖丢失的视图。
  await expect(shortcutRow).toHaveAttribute("aria-expanded", "true");
  const fileRow = thread.locator(".files-tree-root button.files-tree-item").filter({
    has: page.locator(".file-label").getByText(fixture.file_name, { exact: true }),
  }).first();
  await revealTreeRow(fileRow);
  assert.ok((await fileRow.getAttribute("title"))?.startsWith(shortcut.path + "/"));
  await fileRow.click();
  await editor.waitFor();
  assert.equal(await editor.inputValue(), fixture.edited);
  await Promise.all(pending);
  assert.deepEqual(pageErrors, []);
  const previewUri = await fileRow.getAttribute("title");
  assert.ok(exchanges.some(item => new URL(item.url).pathname.endsWith("/files/content")
    && item.status === 200 && item.body.data.path === previewUri
    && item.body.data.content === fixture.edited), "重开必须从真实文件 owner 读取已保存正文");
  await flushView();
  await writeFile(output, JSON.stringify({ shortcut_uri: shortcut.path, reopened_content: await editor.inputValue(),
    preview_uri: previewUri, expanded_restored: true, page_errors: pageErrors, exchanges }, null, 2) + "\n");
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
    await releaseChromiumTemporaryDirectory();
  }
}
