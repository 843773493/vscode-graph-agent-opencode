import assert from "node:assert/strict";
import { mkdir, writeFile } from "node:fs/promises";
import path from "node:path";
import { chromium } from "playwright";

function requiredEnvironment(name) {
  const value = process.env[name];
  if (!value) throw new Error(`缺少环境变量 ${name}`);
  return value;
}

const baseUrl = requiredEnvironment("BOXTEAM_BROWSER_BASE_URL");
const fixture = JSON.parse(requiredEnvironment("BOXTEAM_BROWSER_FIXTURE"));
const artifacts = path.resolve(requiredEnvironment("BOXTEAM_BROWSER_ARTIFACTS"));
const downloads = path.join(artifacts, "downloads");
await mkdir(downloads, { recursive: true });
const browser = await chromium.launch({
  executablePath: requiredEnvironment("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH"),
  headless: true, downloadsPath: downloads,
});
const context = await browser.newContext({ viewport: { width: 1600, height: 1000 }, acceptDownloads: true });
await context.tracing.start({ screenshots: true, snapshots: true, sources: true });
const page = await context.newPage();
const pageErrors = [];
const contextRequests = [];
const pendingResponses = [];
page.on("pageerror", (error) => pageErrors.push(String(error)));
page.on("response", (response) => {
  if (new URL(response.url()).pathname !== "/api/v1/context/read") return;
  pendingResponses.push((async () => {
    const request = response.request();
    const requestHeaders = await request.allHeaders();
    contextRequests.push({
      url: response.url(), request: request.postDataJSON(), status: response.status(),
      workspace_id: requestHeaders["x-boxteam-workspace-id"],
      request_id: response.headers()["x-request-id"], response: await response.json(),
    });
  })());
});

async function waitUntil(predicate, label, timeout = 30000) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    if (await predicate()) return;
    await new Promise((resolve) => setTimeout(resolve, 80));
  }
  throw new Error(`等待${label}超时`);
}

async function bootstrap() {
  await page.goto(baseUrl, { waitUntil: "domcontentloaded" });
  // 通过真实浏览器处理同源 cookie，不使用运行器自带的 HTTP 客户端。
  const catalog = await page.evaluate(async () => {
    const credential = await fetch("/api/gateway/auth/local-credential");
    if (!credential.ok) throw new Error(`读取本地凭证失败：${credential.status}`);
    const token = (await credential.json()).data.token;
    const guest = await fetch("/api/gateway/users/guest", {
      method: "POST",
      headers: { "X-Local-Token": token, "Content-Type": "application/json" },
      body: JSON.stringify({ tracking: { source: "itemized-context-browser-integration" } }),
    });
    if (!guest.ok) throw new Error(`访客登录失败：${guest.status}`);
    const workspaces = await fetch("/api/gateway/workspaces", { headers: { "X-Local-Token": token } });
    if (!workspaces.ok) throw new Error(`读取工作区失败：${workspaces.status}`);
    return (await workspaces.json()).data;
  });
  assert.equal(catalog.active_workspace_id, fixture.workspace_id);
  await page.reload({ waitUntil: "domcontentloaded" });
}

async function selectSession(sessionId) {
  const button = page.locator(`button[data-session-id="${sessionId}"]`);
  await button.waitFor({ state: "visible" });
  await button.click();
}

async function chooseView(name) {
  await page.locator("#viewModeButton").click();
  await page.getByRole("menuitemradio", { name: new RegExp(`^${name}`) }).click();
}

async function openFrozen(sessionId) {
  await chooseView("上下文状态");
  await page.getByRole("button", { name: "冻结请求", exact: true }).click();
  const inspector = page.getByRole("region", { name: "冻结请求上下文" });
  await inspector.waitFor({ state: "visible" });
  await waitUntil(async () => await inspector.getAttribute("data-session-id") === sessionId, "检查面 session owner");
  await waitUntil(async () => !(await inspector.getByText("正在读取封存请求…", { exact: true }).count()), "请求列表");
  assert.equal(await inspector.getByRole("alert").count(), 0);
  return inspector;
}

async function waitProjection(inspector, id) {
  const projection = inspector.locator(`.context-projection[data-assembly-id="${id}"]`);
  await waitUntil(async () => await projection.locator(".context-inspection-items > li").count() > 0, "冻结首屏");
  await waitUntil(async () => !(await projection.getByText("正在读取冻结上下文…", { exact: true }).count()), "冻结分页响应");
  assert.equal(await projection.getByRole("alert").count(), 0);
  return projection;
}

async function inspectAssembly(inspector, expected, label) {
  await inspector.getByLabel("选择冻结 assembly").selectOption(expected.assembly_id);
  const projection = await waitProjection(inspector, expected.assembly_id);
  let pages = 1;
  while (await projection.getByRole("button", { name: "加载下一页上下文", exact: true }).count()) {
    const responsePromise = page.waitForResponse((response) => {
      if (new URL(response.url()).pathname !== "/api/v1/context/read") return false;
      const body = response.request().postDataJSON();
      return body?.resource.endsWith(`#assembly=${expected.assembly_id}`) && body?.cursor;
    });
    await projection.getByRole("button", { name: "加载下一页上下文", exact: true }).click();
    const response = await responsePromise;
    assert.equal(response.status(), 200, await response.text());
    await waitProjection(inspector, expected.assembly_id);
    pages += 1;
    assert.ok(pages < 30, "分页必须收敛");
  }
  const manifest = await projection.locator(".context-selection-entry pre").allTextContents();
  assert.deepEqual(manifest.map((text) => JSON.parse(text)), expected.selection);
  const rows = await projection.locator(".context-selection-entry").evaluateAll((elements) => elements.map((element) => ({
    ordinal: Number(element.dataset.planOrdinal), ref: element.dataset.refId, included: element.dataset.included === "true",
  })));
  assert.deepEqual(rows, expected.selection.map((entry) => ({ ordinal: entry.plan_ordinal, ref: entry.ref.ref_id, included: entry.included })));
  const history = await projection.locator(".context-history-message").evaluateAll((elements) => elements.map((element) => ({
    id: element.dataset.messageId, text: element.querySelector("p")?.textContent ?? "",
  })));
  assert.deepEqual(history, expected.history);
  assert.deepEqual(await projection.locator(".context-capability-loss p").allTextContents(), expected.losses);
  const visibleText = await inspector.innerText();
  for (const secret of ["BROWSER_PRIVATE_REQUEST_BODY", "c2VjcmV0", "HTTP overlay base", "HTTP overlay delta"]) {
    assert.ok(!visibleText.includes(secret), `检查面泄漏 ${secret}`);
  }
  assert.equal(await inspector.evaluate((element) => getComputedStyle(element).overflowY), "auto");
  await inspector.hover();
  await page.mouse.wheel(0, -10000);
  await waitUntil(async () => await inspector.evaluate((element) => element.scrollTop) === 0, "检查区滚动到顶部");
  await page.mouse.wheel(0, 600);
  await waitUntil(async () => await inspector.evaluate((element) => element.scrollTop) > 0, "鼠标滚轮访问后续上下文");
  const firstManifest = projection.locator(".context-selection-entry details").first();
  await firstManifest.locator("summary").click();
  await firstManifest.scrollIntoViewIfNeeded();
  await page.screenshot({ path: path.join(artifacts, `${label}-selection.png`) });
  if (expected.losses.length) {
    await projection.locator(".context-capability-loss").last().scrollIntoViewIfNeeded();
    await page.screenshot({ path: path.join(artifacts, `${label}-loss.png`) });
  }
  return { pages, rows, history, loss: expected.losses };
}

let result;
try {
  await bootstrap();
  await selectSession(fixture.session_id);
  await waitUntil(async () => (await page.locator(".chat-stream-shell").innerText()).includes("native-http-result"), "active history 新输出");
  assert.equal(contextRequests.length, 0, "默认聊天不得加载 assembly 检查数据");
  const inspector = await openFrozen(fixture.session_id);
  await waitUntil(async () => await inspector.getByLabel("选择冻结 assembly").locator("option").count() === 3, "第一页两个封存请求");
  const rich = await inspectAssembly(inspector, fixture.rich, "rich");
  assert.ok(rich.pages > 1);
  assert.ok(rich.rows.some((entry) => !entry.included));
  assert.ok(fixture.rich.selection.some((entry) => entry.ref.ref_type === "tool_set"));
  assert.ok(fixture.rich.losses.includes("browser-opaque:reasoning/opaque"));
  const olderPromise = page.waitForResponse((response) => new URL(response.url()).pathname === "/api/v1/context/read"
    && response.request().postDataJSON()?.view === "assemblies" && response.request().postDataJSON()?.cursor);
  await inspector.getByRole("button", { name: "加载更早请求", exact: true }).click();
  assert.equal((await olderPromise).status(), 200);
  await waitUntil(async () => await inspector.getByLabel("选择冻结 assembly").locator("option").count() === 4, "完整请求列表");
  const first = await inspectAssembly(inspector, fixture.first, "first");
  assert.ok(!first.history.some((message) => message.text === "native-http-result"));
  await chooseView("默认视图");
  await waitUntil(async () => (await page.locator(".chat-stream-shell").innerText()).includes("native-http-result"), "普通历史未被冻结 selection 裁剪");
  await page.screenshot({ path: path.join(artifacts, "active-history.png") });

  await page.reload({ waitUntil: "domcontentloaded" });
  await selectSession(fixture.session_id);
  const restoredInspector = await openFrozen(fixture.session_id);
  await waitUntil(async () => await restoredInspector.getByLabel("选择冻结 assembly").locator("option").count() === 3, "重载列表");
  const restored = await inspectAssembly(restoredInspector, fixture.rich, "reloaded");
  assert.deepEqual(restored.rows, rich.rows);
  assert.deepEqual(restored.history, rich.history);

  await selectSession(fixture.other_session_id);
  await openFrozen(fixture.other_session_id);
  await page.getByText("当前会话没有已封存请求。", { exact: true }).waitFor({ state: "visible" });
  assert.equal(await page.locator(".context-selection-entry").count(), 0);
  assert.equal(await page.locator(".context-projection").count(), 0);
  await page.screenshot({ path: path.join(artifacts, "session-isolation.png") });

  await Promise.all(pendingResponses);
  assert.ok(contextRequests.some((entry) => entry.request.view === "assembly" && entry.request.cursor));
  assert.ok(contextRequests.some((entry) => entry.request.view === "assemblies" && entry.request.cursor));
  for (const entry of contextRequests) {
    assert.equal(new URL(entry.url).origin, new URL(baseUrl).origin);
    assert.equal(entry.workspace_id, fixture.workspace_id);
    assert.equal(entry.status, 200);
    assert.ok(entry.request_id);
    assert.equal(entry.request_id, entry.response.request_id);
    assert.ok([fixture.session_id, fixture.other_session_id].some((id) => entry.request.resource.startsWith(`boxteam://session/${id}`)));
  }
  assert.deepEqual(pageErrors, []);
  result = { classification: "Integration", selection_order_equal: true, loss_equal: true,
    reloaded_same_assembly: true, active_history_preserved: true, session_isolation: true,
    network_routes_verified: true, page_errors: pageErrors, rich, first, restored };
  await writeFile(path.join(artifacts, "browser-result.json"), JSON.stringify(result, null, 2));
} catch (error) {
  await page.screenshot({ path: path.join(artifacts, "failure.png"), fullPage: true });
  await writeFile(path.join(artifacts, "failure.html"), await page.content());
  throw error;
} finally {
  await Promise.allSettled(pendingResponses);
  await writeFile(path.join(artifacts, "context-network.json"), JSON.stringify(contextRequests, null, 2));
  await writeFile(path.join(artifacts, "page-errors.json"), JSON.stringify(pageErrors, null, 2));
  await context.tracing.stop({ path: path.join(artifacts, "browser-trace.zip") });
  await browser.close();
}
