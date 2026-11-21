import { observeRealtime } from "../../support/workbench/realtime.mjs";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import path from "node:path";
import { chromium } from "playwright";
import { expect } from "playwright/test";
import { build } from "esbuild";

const input = JSON.parse(await readFile(process.argv[2], "utf8"));
const { gatewayUrl, gatewayId, workspaceId, resourceId, instanceId, artifactsDir } = input;
await mkdir(artifactsDir, { recursive: true });
const embeddedBundle = await buildEmbeddedHarness();
if (process.argv.includes("--check-bundle")) process.exit(0);
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
let embeddedResult;

async function buildEmbeddedHarness() {
  // 只提供测试宿主页；iframe、React hook、统一 WS 订阅 和所有 owner 请求均使用产品实现。
  const bundle = await build({
    stdin: { resolveDir: process.cwd(), sourcefile: "terminal-observation-harness.tsx", loader: "tsx", contents: `
      import React, { useRef, useState } from "react";
      import { createRoot } from "react-dom/client";
      import { useTerminalFrameObservation } from "./src/clients/web/src/hooks/workspace/useTerminalFrameObservation";
      import { getWorkspaceResource, createWorkspaceTerminal, controlWorkspaceResource } from "./src/clients/web/src/api/workspaceResources";
      import { gatewayWorkspaceKey } from "./src/clients/web/src/api/workspaceIdentity";
      const input = ${JSON.stringify({ gatewayUrl, gatewayId, workspaceId, resourceId })};
      const scope = gatewayWorkspaceKey({ gateway_id: input.gatewayId, workspace_id: input.workspaceId });
      const resources = [await getWorkspaceResource(0, scope, input.resourceId)];
      for (let i = 1; i < 6; i++) resources.push(await createWorkspaceTerminal(0, scope, resources[0].source_thread, "workspace"));
      function Frame({ resource, wide }) {
        const frame = useRef(null);
        const query = new URLSearchParams({ terminalId: resource.resource_id, workspaceId: input.workspaceId,
          target_gateway_id: input.gatewayId, embedded: "1" });
        const url = input.gatewayUrl + "/api/gateway/attach/terminal/?" + query;
        useTerminalFrameObservation(frame, url);
        return <iframe ref={frame} src={url} data-resource={resource.resource_id}
          style={{ width: wide ? 420 : 280, height: 200, border: 0 }} />;
      }
      function App() {
        const [mounted, setMounted] = useState(true), [wide, setWide] = useState(false);
        const [firstVisible, setFirstVisible] = useState(true);
        return <><button id="toggle" onClick={() => setMounted(value => !value)}>切换显示</button>
          <button id="toggle-first" onClick={() => setFirstVisible(value => !value)}>切换第一个终端</button>
          <button id="resize" onClick={() => setWide(value => !value)}>改变第一个终端尺寸</button>
          <button id="cleanup" onClick={async () => {
            setMounted(false);
            for (const resource of resources.slice(1)) {
              let current = await getWorkspaceResource(0, scope, resource.resource_id);
              current = await controlWorkspaceResource(0, scope, current, "detach", current.source_thread);
              await controlWorkspaceResource(0, scope, current, "delete");
            }
            document.body.dataset.cleaned = "true";
          }}>清理</button>
          <div style={{ display: "grid", gridTemplateColumns: "440px 300px 300px" }}>
            {mounted && resources.map((resource, index) => (index !== 0 || firstVisible) && <Frame key={resource.resource_id} resource={resource} wide={index === 0 && wide} />)}
          </div></>;
      }
      createRoot(document.getElementById("root")).render(<App />);
    ` },
    bundle: true, write: false, platform: "browser", format: "esm", target: "es2022",
    nodePaths: [path.resolve("src/clients/web/node_modules")],
    define: { "process.env.NODE_ENV": '"production"' },
  });
  return bundle.outputFiles[0].text;
}

async function verifyEmbeddedTerminals(page) {
  await page.route("**/__terminal_observation_test__", route => route.fulfill({ contentType: "text/html",
    body: '<!doctype html><div id="root"></div><script type="module" src="/__terminal_observation_test__.js"></script>' }));
  await page.route("**/__terminal_observation_test__.js", route => route.fulfill({
    contentType: "text/javascript", body: embeddedBundle,
  }));
  const active = new Set();
  const readers = [];
  const observation = observeRealtime(page, { subscribe(request) {
    if (new URL(request.url).pathname !== '/api/v1/resources/events') return;
    active.add(request); readers.push({ transport: request.transport, subscriptions: 1 });
  }, end(request) { active.delete(request); } });
  await page.setViewportSize({ width: 1200, height: 900 });
  await page.goto(`${gatewayUrl}/__terminal_observation_test__`, { waitUntil: "domcontentloaded" });
  await expect(page.locator("iframe")).toHaveCount(6, { timeout: 30_000 });
  const createdIds = (await page.locator("iframe").evaluateAll(frames => frames.map(frame => frame.dataset.resource)))
    .filter(id => id !== resourceId);
  const frames = await Promise.all((await page.locator("iframe").all()).map(element => element.contentFrame()));
  for (const frame of frames) {
    await expect(frame.locator(".xterm-screen")).toBeVisible({ timeout: 20_000 });
    await expect(frame.locator("#attach-toggle")).toHaveAttribute("aria-label", "断开终端");
  }
  await expect.poll(() => active.size).toBe(1);
  await expect.poll(() => readers.at(-1)?.subscriptions).toBe(1);
  // 等待首次快照完成，跨过真实60秒对账；未变化不能向浏览器制造下一轮读取。
  await page.waitForTimeout(1000);
  const readCounts = () => responses.filter(item => /^\/api\/v1\/resources\/term_/.test(item.path))
    .reduce((counts, item) => ({ ...counts, [item.path]: (counts[item.path] ?? 0) + 1 }), {});
  const beforeIdle = readCounts();
  await page.waitForTimeout(65_000);
  expect(readCounts()).toEqual(beforeIdle);
  const selected = await page.locator("iframe").first().getAttribute("data-resource");
  const connectionsBeforeToggle = readers.length;
  await page.locator("#toggle-first").click();
  await expect(page.locator("iframe")).toHaveCount(5);
  await page.locator("#toggle-first").click();
  await expect(page.locator("iframe")).toHaveCount(6);
  const firstFrame = page.frameLocator(`iframe[data-resource="${selected}"]`);
  await expect(firstFrame.locator("#attach-toggle")).toHaveAttribute("aria-label", "断开终端");
  await page.waitForTimeout(500);
  expect(readers).toHaveLength(connectionsBeforeToggle);
  const afterToggle = readCounts();
  for (const [resourcePath, count] of Object.entries(beforeIdle)) {
    if (!resourcePath.startsWith(`/api/v1/resources/${selected}`)) expect(afterToggle[resourcePath]).toBe(count);
  }
  await page.locator("#resize").click();
  await expect.poll(() => readCounts()[`/api/v1/resources/${selected}`]).toBeGreaterThan(afterToggle[`/api/v1/resources/${selected}`]);
  await page.waitForTimeout(500);
  for (const [resourcePath, count] of Object.entries(afterToggle)) {
    if (resourcePath !== `/api/v1/resources/${selected}`) expect(readCounts()[resourcePath]).toBe(count);
  }
  // 一次真实读取失败后，由有界重试恢复；仍连接的终端也应清掉过期读取错误。
  let injected = false;
  const failurePath = `/api/v1/resources/${selected}`;
  const failureRoute = `**${failurePath}?include_output=false`;
  await page.route(failureRoute, route => {
    if (injected) return route.continue();
    injected = true;
    return route.fulfill({ status: 503, contentType: "application/json", body: JSON.stringify({ detail: "本次终端摘要读取故障" }) });
  });
  await page.locator("#resize").click();
  await expect(firstFrame.locator("#status-line")).toBeVisible();
  await expect(firstFrame.locator("#status-line")).toBeHidden({ timeout: 5000 });
  await expect(firstFrame.locator("#attach-toggle")).toHaveAttribute("aria-label", "断开终端");
  await page.unroute(failureRoute);
  expect(injected).toBe(true);
  await page.screenshot({ path: path.join(artifactsDir, "six-terminals.png") });
  await page.locator("#toggle").click();
  await expect.poll(() => active.size).toBe(0);
  await page.locator("#toggle").click();
  await expect.poll(() => active.size).toBe(1);
  await expect.poll(() => readers.at(-1)?.subscriptions).toBe(1);
  for (const frame of await page.locator("iframe").all()) {
    await expect(frame.contentFrame().locator("#attach-toggle")).toHaveAttribute("aria-label", "断开终端");
  }
  // 普通请求不被六个 iframe 占满的同源 HTTP/1.1 连接阻塞。
  const health = await page.evaluate(async () => {
    const response = await fetch("/api/gateway/health", { signal: AbortSignal.timeout(3000) });
    return response.status;
  });
  expect(health).toBe(200);
  await page.locator("#cleanup").click();
  // 原生进程终止逐项结算，五次管理请求共用75秒清理预算；每次HTTP期限保持不变。
  await expect(page.locator("body")).toHaveAttribute("data-cleaned", "true", { timeout: 75_000 });
  await expect.poll(() => active.size).toBe(0);
  observation.dispose();
  return { readers, beforeIdle, afterToggle, health, activeAfterCleanup: active.size, createdIds,
    expectedFailures: [{ path: failurePath, status: 503 }] };
}
try {
  // route.fulfill 的测试宿主页没有真实远端地址，Chromium 将它视为公共网络；显式授予测试上下文本机网络权限。
  const context = await browser.newContext({ permissions: ['local-network-access'] });
  // attach页面应能仅靠owner快照与内存游标运行；浏览器存储不可用不能阻断终端。
  await context.addInitScript(() => {
    if (window.location.pathname !== "/api/gateway/attach/terminal/") return;
    Object.defineProperty(window, "sessionStorage", { get() { throw new Error("本场景禁止读取终端sessionStorage"); } });
  });
  page = await context.newPage();
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
  observeRealtime(page, { headers(request, response) {
    responses.push({ path: new URL(request.url).pathname, status: response.status, requestId: response.headers['x-request-id'] });
  } });
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
  await expect.poll(() => responses.some(response => response.path === "/api/v1/resources/events" && response.status === 200)).toBe(true);
  await page.reload({ waitUntil: "domcontentloaded" });
  await expect(page.getByRole("button", { name: "断开终端", exact: true })).toBeVisible({ timeout: 20_000 });
  await expect(page.locator(".xterm-rows")).toContainText(marker);
  const reloadMarker = `terminal-reloaded-${suffix}`;
  await page.locator(".xterm-helper-textarea").focus();
  await page.keyboard.type(`printf '%s%s\\n' 'terminal-reloaded-' '${suffix}'`);
  await page.keyboard.press("Enter");
  await expect(page.locator(".xterm-rows")).toContainText(reloadMarker, { timeout: 15_000 });
  // 独立页验证真实输入输出；静止零补读在下方六终端完成初始化后覆盖完整心跳周期。
  expect(pageErrors).toEqual([]);
  expect(requestFailures).toEqual([]);
  expect(httpFailures).toEqual([]);
  expect(staticAssetFailures).toEqual([]);
  expect(consoleMessages.filter(message => message.type === "error")).toEqual([]);
  expect(sockets).toContain(`/api/gateway/service-plugins/boxteam.developer-tools/terminal/instances/${instanceId}/services/socket/terminal`);
  expect(responses.some(response => response.path.endsWith("/transport-grant") && response.status === 200 && response.requestId)).toBe(true);
  await page.screenshot({ path: path.join(artifactsDir, "terminal.png") });
  const screen = await page.locator(".xterm-rows").innerText();
  embeddedResult = await verifyEmbeddedTerminals(page);
  expect(pageErrors).toEqual([]);
  expect(httpFailures).toEqual(embeddedResult.expectedFailures);
  await writeFile(path.join(artifactsDir, "result.json"), JSON.stringify({
    gatewayId, workspaceId, resourceId, instanceId, marker, reloadMarker, pageErrors, consoleMessages,
    requestFailures, httpFailures, staticAssetFailures, sockets, responses,
    screen, embeddedResult,
  }, null, 2), { flag: "wx" });
} catch (error) {
  failure = error;
  await writeFile(path.join(artifactsDir, "failure.json"), JSON.stringify({
    error: String(error), pageErrors, consoleMessages, requestFailures,
    httpFailures, staticAssetFailures, sockets, responses,
  }, null, 2), { flag: "wx" });
  if (page) {
    try { await page.screenshot({ path: path.join(artifactsDir, "failure.png") }); }
    catch (screenshotError) { failure = new AggregateError([error, screenshotError], "终端页面验收及失败截图均失败"); }
  }
  throw failure;
} finally {
  try {
    await browser.close();
  } catch (error) {
    if (failure) throw new AggregateError([failure, error], "终端页面验收及浏览器关闭均失败");
    throw error;
  }
}
