import { mkdir, readFile, writeFile } from "node:fs/promises";
import path from "node:path";
import { chromium } from "playwright";
import { expect } from "playwright/test";

const input = JSON.parse(await readFile(process.argv[2], "utf8"));
const { gatewayUrl, gatewayId, workspaceId, resourceId, instanceId, artifactsDir } = input;
await mkdir(artifactsDir, { recursive: true });
const browser = await chromium.launch({
  executablePath: input.chromiumExecutable,
  headless: true,
  args: ["--no-sandbox", "--disable-dev-shm-usage"],
});
let page;
let failure;
const pageErrors = [];
const consoleMessages = [];
const requestFailures = [];
const httpFailures = [];
const staticAssetFailures = [];
const sockets = [];
const responses = [];
try {
  page = await browser.newPage();
  await page.context().addCookies(input.cookies);
  page.on("pageerror", error => pageErrors.push(String(error)));
  page.on("console", message => {
    consoleMessages.push({ type: message.type(), text: message.text() });
  });
  page.on("requestfailed", request => {
    requestFailures.push({
      method: request.method(),
      url: request.url(),
      failure: request.failure()?.errorText ?? null,
    });
  });
  page.on("websocket", socket => sockets.push(new URL(socket.url()).pathname));
  page.on("response", response => {
    const url = new URL(response.url());
    const record = { path: url.pathname, status: response.status() };
    if (response.status() < 200 || response.status() >= 300) {
      httpFailures.push(record);
      if (url.pathname.startsWith("/api/gateway/attach/")) staticAssetFailures.push(record);
    }
    if (url.pathname.startsWith("/api/v1/")) {
      responses.push({ ...record, requestId: response.headers()["x-request-id"] });
    }
  });
  const query = new URLSearchParams({ terminalId: resourceId, workspaceId, target_gateway_id: gatewayId });
  await page.goto(`${gatewayUrl}/api/gateway/attach/terminal/?${query}`, { waitUntil: "domcontentloaded" });
  await expect(page.getByRole("button", { name: "断开终端", exact: true })).toBeVisible({ timeout: 20_000 });
  const suffix = String(Date.now());
  const marker = `terminal-page-${suffix}`;
  await page.locator(".xterm-helper-textarea").focus();
  await page.keyboard.type(`printf '%s%s\\n' 'terminal-page-' '${suffix}'`);
  await page.keyboard.press("Enter");
  await expect(page.locator(".xterm-rows")).toContainText(marker, { timeout: 15_000 });
  expect(pageErrors).toEqual([]);
  expect(requestFailures).toEqual([]);
  expect(httpFailures).toEqual([]);
  expect(staticAssetFailures).toEqual([]);
  expect(consoleMessages.filter(message => message.type === "error")).toEqual([]);
  expect(sockets).toContain(`/api/gateway/service-plugins/boxteam.developer-tools/terminal/instances/${instanceId}/services/socket/terminal`);
  expect(responses.some(response => response.path.endsWith("/transport-grant") && response.status === 200 && response.requestId)).toBe(true);
  await page.screenshot({ path: path.join(artifactsDir, "terminal.png") });
  await writeFile(path.join(artifactsDir, "result.json"), JSON.stringify({
    gatewayId, workspaceId, resourceId, instanceId, marker, pageErrors, consoleMessages,
    requestFailures, httpFailures, staticAssetFailures, sockets, responses,
    screen: await page.locator(".xterm-rows").innerText(),
  }, null, 2), { flag: "wx" });
} catch (error) {
  failure = error;
  await writeFile(path.join(artifactsDir, "failure.json"), JSON.stringify({
    error: String(error), pageErrors, consoleMessages, requestFailures,
    httpFailures, staticAssetFailures, sockets, responses,
  }, null, 2), { flag: "wx" });
  if (page) await page.screenshot({ path: path.join(artifactsDir, "failure.png") });
  throw error;
} finally {
  try {
    await browser.close();
  } catch (error) {
    if (failure) throw new AggregateError([failure, error], "终端页面验收及浏览器关闭均失败");
    throw error;
  }
}
