// Integration：生产页面与真实 Gateway/Backend；仅模型 Provider 使用协议桩。
import assert from "node:assert/strict";
import { dirname } from "node:path";
import { mkdir, writeFile } from "node:fs/promises";
import { chromium } from "playwright";

import { configureChromiumTemporaryDirectory } from "./chromium-temporary-directory.mjs";

const fixture = JSON.parse(process.env.BOXTEAM_BROWSER_FIXTURE);
const output = process.env.BOXTEAM_BROWSER_RESULT_PATH;
assert.ok(output && process.env.BOXTEAM_BROWSER_BASE_URL && process.env.BOXTEAM_BROWSER_RUNTIME_PATH);
await mkdir(dirname(output), { recursive: true });
const releaseChromiumTemporaryDirectory = await configureChromiumTemporaryDirectory(process.env.BOXTEAM_BROWSER_RUNTIME_PATH);
const errors = [], exchanges = [], pending = [];
let browser, context, page;
try {
  browser = await chromium.launch({ executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH, headless: true });
  context = await browser.newContext({ viewport: { width: 1920, height: 1100 } });
  await context.addCookies(fixture.cookies);
  page = await context.newPage();
  page.setDefaultTimeout(20_000);
  page.on("pageerror", (error) => errors.push(String(error)));
  page.on("response", (response) => {
    if (new URL(response.url()).pathname === "/api/gateway/local-directories") {
      pending.push((async () => {
        const body = await response.json();
        assert.equal(body.request_id, response.headers()["x-request-id"]);
        assert.ok(body.request_id);
        assert.ok(!JSON.stringify(body).includes(fixture.directory), "目录选择响应泄露真实路径");
        assert.equal(response.status(), 200, JSON.stringify(body));
        const { data } = body;
        assert.ok(data.path.startsWith("boxteam://gateway/"));
        assert.ok(data.home_path.startsWith("boxteam://gateway/"));
        assert.ok(data.parent_path === null || data.parent_path.startsWith("boxteam://gateway/"));
        assert.ok(data.entries.every((entry) => entry.path.startsWith("boxteam://gateway/")));
        exchanges.push({ request: response.request().url(), body });
      })());
    }
  });
  await page.goto(process.env.BOXTEAM_BROWSER_BASE_URL, { waitUntil: "domcontentloaded" });
  const openDialog = async () => {
    await page.getByRole("button", { name: "添加工作区", exact: true }).first().click();
    await page.getByRole("button").filter({ hasText: "当前电脑" }).click();
    await page.getByLabel("目录地址", { exact: true }).waitFor();
    await page.waitForFunction(() => {
      const input = document.querySelector('input[aria-label="目录地址"]');
      return input && !input.disabled && input.value.startsWith("boxteam://");
    });
  };
  const dialog = page.getByRole("dialog", { name: "选择工作区文件夹" });
  const address = page.getByLabel("目录地址", { exact: true });
  const loaded = async (name) => {
    await dialog.locator(".workspace-add-selection-summary strong").filter({ hasText: name }).waitFor();
    assert.ok(!(await address.isDisabled()));
  };
  const enter = async (text, name) => {
    await address.fill(text);
    await address.press("Enter");
    await loaded(name);
  };
  await openDialog();
  await enter(fixture.directory + "/", fixture.directory_name);
  const rootUri = await address.inputValue();
  await enter(".", fixture.directory_name);
  assert.equal(await address.inputValue(), rootUri, "相对当前目录被误当成目录名搜索");
  await address.fill("no-matching-directory");
  await address.press("Enter");
  await dialog.getByText("当前目录下没有子目录", { exact: true }).waitFor();
  assert.ok(await dialog.getByRole("button", { name: "添加工作区", exact: true }).isDisabled(), "搜索失败仍允许注册父目录");
  await enter(rootUri, fixture.directory_name);
  await enter("alpha", fixture.target_name);
  const targetUri = await address.inputValue();
  assert.ok(targetUri.startsWith("boxteam://gateway/"));
  await dialog.getByRole("button", { name: "返回上级目录", exact: true }).click();
  await loaded(fixture.directory_name);
  assert.equal(await address.inputValue(), rootUri);
  await dialog.getByRole("button", { name: "关闭", exact: true }).click();
  await page.reload({ waitUntil: "domcontentloaded" });
  await openDialog();
  await enter(targetUri, fixture.target_name);
  await dialog.getByRole("button", { name: "返回上级目录", exact: true }).click();
  await loaded(fixture.directory_name);
  // 地址栏失焦不能触发旧目录重载，覆盖刚选中的子目录。
  await address.focus();
  await dialog.locator(".workspace-add-directory-select").filter({ hasText: fixture.target_name }).click();
  await loaded(fixture.target_name);
  const created = page.waitForResponse((response) => response.request().method() === "POST"
    && new URL(response.url()).pathname === "/api/gateway/managed-workspaces", { timeout: 120_000 });
  await dialog.getByRole("button", { name: "添加工作区", exact: true }).click();
  const response = await created;
  const body = await response.json();
  assert.equal(response.status(), 200, JSON.stringify(body));
  assert.equal(body.request_id, response.headers()["x-request-id"]);
  const request = response.request().postDataJSON();
  assert.equal(request.root_path, targetUri);
  assert.equal(request.create_directory, false);
  await dialog.waitFor({ state: "hidden", timeout: 120_000 });
  await Promise.all(pending);
  assert.deepEqual(errors, []);
  await writeFile(output, JSON.stringify({ root_uri: rootUri, target_uri: targetUri, request,
    result: body, exchanges, page_errors: errors }, null, 2) + "\n");
} catch (error) {
  if (page) {
    await page.screenshot({ path: dirname(output) + "/failure.png", fullPage: true });
    await writeFile(dirname(output) + "/failure.html", await page.content());
  }
  throw error;
} finally {
  try { if (context) await context.close(); }
  finally { try { if (browser) await browser.close(); } finally { await releaseChromiumTemporaryDirectory(); } }
}
