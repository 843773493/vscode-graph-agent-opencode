import assert from "node:assert/strict";
import { writeFile } from "node:fs/promises";
import { chromium } from "playwright";
import { expect } from "playwright/test";
import { TestRunContext } from "../../../harness/js/run-context.mjs";

const fixture = JSON.parse(process.env.BOXTEAM_BROWSER_FIXTURE);
const scope = TestRunContext.fromTestFile("tests/integration/clients/web/test_attachment_directory.py");
const result = { pageErrors: [], stage: "opening", restored: false, removed: false, sent: false };
const bytes = Buffer.from("浏览器直接上传原始字节 Ω\n");
let page;
let failure;
try {
  const browser = await chromium.launch({ executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH, headless: true });
  scope.addCleanup("附件上传 Chromium", () => browser.close());
  const context = await browser.newContext({ viewport: { width: 1400, height: 1000 } });
  scope.addCleanup("附件上传 context", () => context.close());
  page = await context.newPage();
  page.on("pageerror", error => result.pageErrors.push(error.message));
  const endpoint = `/api/v1/sessions/${fixture.sessionId}/threads/${fixture.threadId}/attachments`;
  const uploads = [];
  page.on("request", request => {
    if (new URL(request.url()).pathname === endpoint && request.method() === "POST") {
      uploads.push(request.url());
    }
  });
  await page.goto(process.env.BOXTEAM_BROWSER_BASE_URL, { waitUntil: "domcontentloaded" });
  await expect(page.locator("#workbench .monaco-workbench")).toBeVisible();
  await page.locator(`button[data-session-id="${fixture.sessionId}"]`).dblclick();
  const thread = page.locator(`section.boxteam-thread[data-workspace-id="${fixture.workspaceId}"][data-session-id="${fixture.sessionId}"][data-thread-id="${fixture.threadId}"]`);
  const selectFile = async () => {
    const [response] = await Promise.all([
      page.waitForResponse(response => new URL(response.url()).pathname === endpoint && response.request().method() === "POST"),
      thread.locator('input.composer-file-input').setInputFiles({ name: "source.txt", mimeType: "text/plain", buffer: bytes }),
    ]);
    assert.equal(response.status(), 200, await response.text());
    const body = await response.json();
    assert.equal(response.headers()["x-request-id"], body.request_id);
    const headers = await response.request().allHeaders();
    assert.equal(headers["content-type"], "text/plain");
    // Chromium 对 File 请求体不提供 postDataBuffer；从真实 Gateway 读回并核对字节。
    const content = await context.request.get(new URL(`/api/v1/sessions/${fixture.sessionId}/attachments/content?${new URLSearchParams({ file_id: body.data.file_id })}`, response.url()).href,
      { headers: Object.fromEntries(Object.entries(headers).filter(([name]) => name.startsWith("x-boxteam-") || name === "x-local-token")) });
    assert.equal(content.status(), 200, await content.text());
    assert.deepEqual(await content.body(), bytes);
    await expect(thread.locator(".composer-attachment .codicon-loading")).toHaveCount(0);
    await expect(thread.locator(".composer-attachment-name")).toHaveText("source.txt");
    return body.data;
  };
  result.stage = "first-upload";
  const first = await selectFile();
  // 上传不应触发模型执行；关闭页面不删除草稿，刷新后直接使用后端引用。
  result.stage = "reload";
  await page.evaluate(() => globalThis.boxteamWorkbenchLifecycle.prepareToClose());
  await page.reload({ waitUntil: "domcontentloaded" });
  await expect(thread.locator(".composer-attachment-name")).toHaveText("source.txt");
  assert.equal(uploads.length, 1);
  result.restored = true;
  result.stage = "remove";
  const deletion = page.waitForResponse(response => new URL(response.url()).pathname === endpoint && response.request().method() === "DELETE");
  await thread.getByRole("button", { name: "移除 source.txt", exact: true }).click();
  const removed = await deletion;
  assert.equal(removed.status(), 200, await removed.text());
  assert.equal(new URL(removed.url()).searchParams.get("file_id"), first.file_id);
  await expect(thread.locator(".composer-attachment")).toHaveCount(0);
  result.removed = true;
  result.stage = "send";
  const second = await selectFile();
  const sending = page.waitForResponse(response => new URL(response.url()).pathname.endsWith(`/threads/${fixture.threadId}/messages`) && response.request().method() === "POST");
  await thread.getByTestId("composer-input").fill("请读取附件");
  await thread.getByTestId("composer-send").click();
  const sent = await sending;
  assert.equal(sent.status(), 200, await sent.text());
  const payload = sent.request().postDataJSON();
  assert.equal(payload.message.attachments.length, 1);
  assert.equal(payload.message.attachments[0].file_id, second.file_id);
  assert.equal(payload.message.attachments[0].directory_uri, second.directory_uri);
  assert.equal(payload.message.attachments[0].data_url, undefined);
  result.jobId = (await sent.json()).data.job_id;
  // 首轮会冷初始化 Agent（已有日志约 8 秒），与普通上传的等待分开。
  await expect(thread.getByText("附件已通过独立上传进入消息。", { exact: true })).toBeVisible({ timeout: 30_000 });
  result.sent = true;
  assert.deepEqual(result.pageErrors, []);
  result.stage = "completed";
  await page.screenshot({ path: process.env.BOXTEAM_BROWSER_SCREENSHOT_PATH });
} catch (error) {
  failure = error;
} finally {
  const errors = failure ? [failure] : [];
  try {
    if (page) await page.screenshot({ path: process.env.BOXTEAM_BROWSER_SCREENSHOT_PATH });
    await writeFile(process.env.BOXTEAM_BROWSER_RESULT_PATH, JSON.stringify(result, null, 2));
  } catch (error) { errors.push(error); }
  try { await scope.close(); } catch (error) { errors.push(error); }
  if (errors.length) throw new AggregateError(errors, "附件预上传 Web 验收失败");
}
