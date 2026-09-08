import { writeFile } from "node:fs/promises";
import process from "node:process";
import { chromium } from "playwright";

function requiredEnvironment(name) {
  const value = process.env[name];
  if (!value) throw new Error(`缺少环境变量 ${name}`);
  return value;
}

const baseUrl = requiredEnvironment("BOXTEAM_E2E_BASE_URL");
const fixture = JSON.parse(requiredEnvironment("BOXTEAM_E2E_FIXTURE"));
const resultPath = requiredEnvironment("BOXTEAM_E2E_RESULT_PATH");
const screenshotPath = requiredEnvironment("BOXTEAM_E2E_SCREENSHOT_PATH");
const executablePath = process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH || undefined;

const browser = await chromium.launch({ executablePath, headless: true });
const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
await context.grantPermissions(["clipboard-read", "clipboard-write"], { origin: baseUrl });
const page = await context.newPage();

try {
  await page.goto(baseUrl, { waitUntil: "domcontentloaded", timeout: 30_000 });
  const sessionAction = page.locator(`[data-session-id="${fixture.sessionId}"]`);
  await sessionAction.waitFor({ state: "visible", timeout: 15_000 });
  await sessionAction.locator("xpath=..").click({ button: "right" });

  const copyMenuItem = page.getByRole("menuitem", {
    name: "复制会话信息",
    exact: true,
  });
  await copyMenuItem.waitFor({ state: "visible", timeout: 10_000 });
  const informationResponsePromise = page.waitForResponse(
    (response) => response.request().method() === "GET"
      && response.url().includes(`/sessions/${fixture.sessionId}/information`),
  );
  await copyMenuItem.click();
  const informationResponse = await informationResponsePromise;
  if (!informationResponse.ok()) {
    throw new Error(`读取会话信息失败: ${informationResponse.status()} ${await informationResponse.text()}`);
  }
  const informationText = await informationResponse.text();
  const informationPayload = JSON.parse(informationText);
  const information = informationPayload.data;
  if (information.kind !== "session_diagnostic_snapshot") {
    throw new Error(`后端返回了错误的会话信息 kind: ${information.kind}`);
  }
  if (information.schema_version !== 2) {
    throw new Error(`后端返回了错误的会话信息 schema_version: ${information.schema_version}`);
  }
  if (typeof information.session !== "object" || information.session === null) {
    throw new Error("会话信息缺少有限 session 投影");
  }
  if (information.session.title_truncated !== true) {
    throw new Error("超长会话标题没有标记 title_truncated");
  }
  if (Object.prototype.hasOwnProperty.call(information, "child_session_ids")) {
    throw new Error("会话信息仍返回旧的 child_session_ids 字段");
  }
  if (!information.resources || Array.isArray(information.resources)) {
    throw new Error("会话信息 resources 没有使用有界摘要对象");
  }
  if (information.resources.active.length > 32 || information.resources.recent_closed.length > 16) {
    throw new Error("会话信息资源摘要超过协议上限");
  }
  if (!Array.isArray(information.recent_errors) || information.recent_errors.length > 5) {
    throw new Error("会话信息错误摘要超过协议上限");
  }

  await page.waitForFunction(async (sessionId) => {
    const text = await navigator.clipboard.readText();
    return text.includes('"kind": "session_diagnostic_snapshot"')
      && text.includes(`"id": "${sessionId}"`);
  }, fixture.sessionId, { timeout: 10_000 });
  const clipboardText = await page.evaluate(() => navigator.clipboard.readText());
  const dump = JSON.parse(clipboardText);
  if (dump.kind !== "session_diagnostic_snapshot" || dump.schema_version !== 2) {
    throw new Error("剪贴板不是新的会话诊断快照协议");
  }
  if (Object.prototype.hasOwnProperty.call(dump, "child_session_ids")) {
    throw new Error("剪贴板仍包含旧的 child_session_ids 字段");
  }
  if (dump.session.title_truncated !== true || dump.resources.active.length > 32) {
    throw new Error("剪贴板没有保留有界诊断投影");
  }

  await writeFile(resultPath, `${JSON.stringify({
    apiPayloadBytes: Buffer.byteLength(informationText),
    clipboardBytes: Buffer.byteLength(clipboardText),
    kind: dump.kind,
    schemaVersion: dump.schema_version,
    titleLength: dump.session.title.length,
    titleTruncated: dump.session.title_truncated,
    activeResourceCount: dump.resources.active.length,
    recentClosedResourceCount: dump.resources.recent_closed.length,
    recentErrorCount: dump.recent_errors.length,
  }, null, 2)}\n`, "utf8");
} catch (error) {
  await page.screenshot({ path: screenshotPath, fullPage: true }).catch(() => undefined);
  throw error;
} finally {
  await browser.close();
}
