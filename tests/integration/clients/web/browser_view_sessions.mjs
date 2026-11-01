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

const baseUrl = required("BOXTEAM_BROWSER_BASE_URL");
const workspaceId = required("BOXTEAM_BROWSER_WORKSPACE_ID");
const owner = JSON.parse(required("BOXTEAM_BROWSER_OWNER"));
const unopenedSessionId = required("BOXTEAM_BROWSER_UNOPENED_SESSION_ID");
const unopenedThreadId = required("BOXTEAM_BROWSER_UNOPENED_THREAD_ID");
const resultPath = required("BOXTEAM_BROWSER_RESULT_PATH");
const screenshotPath = required("BOXTEAM_BROWSER_SCREENSHOT_PATH");
const scope = TestRunContext.fromTestFile("tests/integration/clients/web/test_browser_view_sessions.py");
const threadSelector = `section.boxteam-thread[data-workspace-id="${owner.workspace_id}"][data-session-id="${owner.session_id}"][data-thread-id="${owner.thread_id}"]`;
const draftA = "仅浏览器 A 保存的草稿";
const draftB = "仅浏览器 B 保存的草稿";
const pageErrors = [];
const serverViewWrites = [];
const requests = [];
const scrollMeasurements = [];
const historyRecovery = [];
const unreadReadProof = { heldRequests: [] };
let pageA;
let failure;

async function api(page, pathname, init = {}) {
  return page.evaluate(async ({ pathname, init, workspaceId }) => {
    const credential = await fetch("/api/gateway/auth/local-credential");
    if (!credential.ok) throw new Error(`读取本地凭据失败 ${credential.status}`);
    const token = (await credential.json()).data.token;
    const headers = new Headers(init.headers ?? {});
    headers.set("X-Local-Token", token);
    if (pathname.startsWith("/api/v1/")) headers.set("X-BoxTeam-Workspace-Id", workspaceId);
    const response = await fetch(pathname, { ...init, headers, credentials: "include" });
    const payload = await response.json();
    if (!response.ok) throw new Error(`API ${response.status}: ${JSON.stringify(payload)}`);
    if (!payload.request_id || response.headers.get("X-Request-ID") !== payload.request_id) {
      throw new Error(`响应 request_id 不一致：${pathname}`);
    }
    return payload.data;
  }, { pathname, init, workspaceId });
}

async function waitUntil(predicate, label, timeout = 30_000) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    if (await predicate()) return;
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  throw new Error(`等待${label}超时`);
}

function observe(page, name) {
  page.on("pageerror", error => pageErrors.push({ page: name, error: error.message }));
  page.on("request", request => {
    const path = new URL(request.url()).pathname;
    if (/(?:\/view-state|\/ui-settings)$/.test(path) && ["PUT", "PATCH", "POST"].includes(request.method())) {
      serverViewWrites.push({ page: name, path, method: request.method() });
    }
    if (path.startsWith("/api/")) requests.push({ page: name, path, method: request.method() });
  });
}

async function initialize(page) {
  await page.goto(new URL("/", baseUrl).href, { waitUntil: "domcontentloaded" });
  await expect(page.getByRole("button", { name: "刷新资源树", exact: true })).toBeVisible({ timeout: 30_000 });
  assert.equal((await api(page, "/api/gateway/users/current")).kind, "guest");
  const workspaces = await api(page, "/api/gateway/workspaces");
  assert.equal(workspaces.active_workspace_id, workspaceId);
  await api(page, "/api/v1/workspace");
  await expect(page.getByRole("button", { name: "用户视图", exact: true })).toHaveCount(0);
}

async function openThread(page) {
  await page.locator(`button[data-session-id="${owner.session_id}"]`).dblclick();
  const thread = page.locator(threadSelector);
  await expect(thread).toBeVisible();
  await expect(thread.locator("#input")).toBeVisible({ timeout: 30_000 });
  await expect(thread.locator('.chat-turn').first()).toBeVisible();
  return thread;
}

/** 只读真实数据库；展示态必须由真实控件操作产生，不能向数据库预写输入。 */
async function readView(page, gatewayId) {
  const key = JSON.stringify([gatewayId, "session", workspaceId, owner.session_id, owner.thread_id]);
  return page.evaluate(async key => {
    const databases = await indexedDB.databases();
    if (!databases.some(database => database.name === "boxteam-browser-views")) return null;
    return new Promise((resolve, reject) => {
      const opening = indexedDB.open("boxteam-browser-views");
      opening.onerror = () => reject(opening.error);
      opening.onsuccess = () => {
        const database = opening.result;
        const transaction = database.transaction("views", "readonly");
        const request = transaction.objectStore("views").get(key);
        transaction.oncomplete = () => { database.close(); resolve(request.result ?? null); };
        transaction.onabort = () => { database.close(); reject(transaction.error); };
      };
    });
  }, key);
}

async function assertRestored(thread, saved) {
  await expect(thread.locator("#input")).toHaveValue(draftA);
  await expect(thread.getByRole("button", { name: "切换会话资源", exact: true })).toHaveAttribute("aria-expanded", "true");
  await expect(thread.getByRole("tab", { name: "更改", exact: true })).toHaveAttribute("aria-selected", "true");
  const scroller = thread.locator(".chat-virtual-list");
  const anchor = thread.locator(`article.chat-turn[data-turn-id="${saved.turn_anchor}"]`);
  await expect(anchor).toBeVisible();
  await waitUntil(async () => {
    const viewport = await scroller.boundingBox();
    const item = await anchor.boundingBox();
    return viewport && item && Math.abs(viewport.y - item.y - saved.scroll_offset) < 5;
  }, "真实滚动锚点恢复");
}

/** 只暂停真实请求；新页面没有旧 JS 内存，先显内容只能来自浏览器持久缓存。 */
async function verifyHistoryBootstrap(page, expectCache, open) {
  const pathname = `/api/v1/sessions/${owner.session_id}/bootstrap`;
  const matches = url => url.pathname === pathname && url.searchParams.get("thread_id") === owner.thread_id;
  const heldRequests = [];
  const evidence = { expectCache, heldRequests, sessionId: owner.session_id,
    workspaceId: owner.workspace_id, threadId: owner.thread_id };
  historyRecovery.push(evidence);
  let release;
  const released = new Promise(resolve => { release = resolve; });
  const intercept = async route => {
    const request = route.request();
    if (request.method() !== "GET" || request.headers()["x-boxteam-workspace-id"] !== workspaceId) {
      await route.continue();
      return;
    }
    heldRequests.push({ url: request.url(), workspaceId: request.headers()["x-boxteam-workspace-id"],
      threadId: new URL(request.url()).searchParams.get("thread_id") });
    await released;
    await route.continue();
  };
  await page.route(matches, intercept);
  try {
    await open();
    const thread = page.locator(threadSelector);
    await expect(thread).toBeVisible();
    await waitUntil(() => heldRequests.length > 0, "精确 Thread 历史 bootstrap 请求进入延迟点");
    const cached = thread.locator(".native-thread-cached-history");
    let cachedTurnIds = [];
    let cachedControlCount = 0;
    if (expectCache) {
      await expect(cached).toBeVisible();
      await expect(cached.locator(".native-thread-status")).toHaveText("已显示上次历史缓存，正在同步会话…");
      // Playwright 的 disabled matcher 只识别表单控件，不识别 fieldset 自身。
      await expect(cached.locator("fieldset")).toHaveJSProperty("disabled", true);
      const controls = await cached.locator("button, input, select, textarea").evaluateAll(nodes =>
        nodes.map(node => ({ tag: node.tagName, disabled: node.matches(":disabled") })));
      cachedControlCount = controls.length;
      assert.ok(cachedControlCount > 0, "缓存正文必须包含真实操作控件，不能用空集合证明只读");
      assert.ok(controls.every(control => control.disabled), JSON.stringify(controls));
      await expect(cached.locator(".chat-turn").first()).toBeVisible();
      cachedTurnIds = await cached.locator(".chat-turn").evaluateAll(turns => turns.map(turn => turn.dataset.turnId));
      assert.ok(cachedTurnIds.length > 0);
      assert.ok(cachedTurnIds.every(id => /^job-000[1-8]$/.test(id)), "缓存正文必须来自当前 owner 已加载的真实历史");
      assert.match(await cached.innerText(), /用户问题 [1-8]/);
      await expect(thread.getByTestId("composer-input")).toHaveCount(0);
    } else {
      await expect(cached).toHaveCount(0);
      await expect(thread.locator(".chat-turn")).toHaveCount(0);
      await expect(thread.locator(".chat-stream-empty-title")).toHaveText("正在加载最新 Turn");
    }
    const responseReady = page.waitForResponse(response => {
      const request = response.request();
      return matches(new URL(response.url())) && request.method() === "GET"
        && request.headers()["x-boxteam-workspace-id"] === workspaceId;
    });
    release();
    const response = await responseReady;
    assert.equal(response.status(), 200, await response.text());
    const payload = await response.json();
    assert.equal(payload.data.session.session_id, owner.session_id);
    assert.equal(payload.data.session.workspace_id, owner.workspace_id);
    assert.equal(payload.data.thread_id, owner.thread_id);
    assert.ok(payload.request_id);
    assert.equal(response.headers()["x-request-id"], payload.request_id);
    await expect(thread.getByTestId("composer-input")).toBeVisible({ timeout: 30_000 });
    await expect(cached).toHaveCount(0);
    await expect(thread.locator(".chat-turn").first()).toBeVisible();
    Object.assign(evidence, { cachedTurnIds, cachedControlCount, requestId: payload.request_id,
      cachedActionsDisabled: expectCache ? true : null, liveAfterSync: true });
    return thread;
  } finally {
    release();
    await page.unrouteAll({ behavior: "wait" });
  }
}

try {
  const browser = await chromium.launch({ executablePath: required("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH"), headless: true });
  scope.addCleanup("视图 Chromium", () => browser.close());
  const contextA = await browser.newContext({ viewport: { width: 1440, height: 700 } });
  scope.addCleanup("浏览器 A", () => contextA.close());
  const contextB = await browser.newContext({ viewport: { width: 1440, height: 700 } });
  scope.addCleanup("浏览器 B", () => contextB.close());
  pageA = await contextA.newPage();
  observe(pageA, "A");
  await initialize(pageA);
  const gatewayId = (await api(pageA, "/api/gateway/health")).gateway_id;
  let threadA = await openThread(pageA);
  await expect(threadA.locator('.chat-turn[data-turn-id="job-0008"]')).toBeVisible();
  await threadA.locator("#input").fill(draftA);
  await threadA.getByRole("button", { name: "切换会话资源", exact: true }).click();
  await threadA.getByRole("tab", { name: "更改", exact: true }).click();
  const scroller = threadA.locator(".chat-virtual-list");
  await scroller.hover();
  // 资源列展开会改变聊天宽度；以重排后真实尾部为滚轮基准。
  await waitUntil(async () => await scroller.evaluate(node => node.scrollHeight - node.clientHeight - node.scrollTop) < 2, "资源列重排后的聊天尾部");
  const before = await scroller.evaluate(node => node.scrollTop);
  scrollMeasurements.push({ phase: "before-wheel", ...await scroller.evaluate(node => ({ top: node.scrollTop, height: node.scrollHeight, viewport: node.clientHeight })) });
  assert.ok(before > 20, "装具必须产生真实可滚动聊天内容");
  await pageA.mouse.wheel(0, -350);
  await waitUntil(async () => {
    const position = await scroller.evaluate(node => ({ top: node.scrollTop, height: node.scrollHeight, viewport: node.clientHeight }));
    scrollMeasurements.push({ phase: "after-wheel", ...position });
    return position.top < before - 20;
  }, "鼠标滚轮上移");
  let saved;
  await waitUntil(async () => {
    saved = await readView(pageA, gatewayId);
    return saved?.follow_latest === false && saved.turn_anchor !== null;
  }, "浏览器持久化滚动位置");
  assert.equal(saved.workspace_id, workspaceId);
  assert.equal(saved.session_id, owner.session_id);
  // 原版 Workbench 的 IndexedDB 在卸载时只能乐观提交；明确等待既有生命周期完成。
  await pageA.evaluate(() => globalThis.boxteamWorkbenchLifecycle.prepareToClose());
  await pageA.reload({ waitUntil: "domcontentloaded" });
  threadA = pageA.locator(threadSelector);
  await expect(threadA.locator("#input")).toBeVisible({ timeout: 30_000 });
  await assertRestored(threadA, saved);

  const pageB = await contextB.newPage();
  observe(pageB, "B");
  const threadB = await verifyHistoryBootstrap(pageB, false, async () => {
    await initialize(pageB);
    await pageB.locator(`button[data-session-id="${owner.session_id}"]`).dblclick();
  });
  await expect(threadB.locator('.chat-turn[data-turn-id="job-0008"]')).toBeVisible();
  await expect(threadB.locator("#input")).toHaveValue("");
  await expect(threadB.getByRole("button", { name: "切换会话资源", exact: true })).toHaveAttribute("aria-expanded", "false");
  assert.notDeepEqual(await readView(pageB, gatewayId), saved, "独立浏览器不应继承 A 的滚动位置");
  await threadB.locator("#input").fill(draftB);
  await expect(threadA.locator("#input")).toHaveValue(draftA);

  await pageA.evaluate(() => globalThis.boxteamWorkbenchLifecycle.prepareToClose());
  await pageA.close();
  pageA = await contextA.newPage();
  observe(pageA, "A-reopened");
  threadA = await verifyHistoryBootstrap(pageA, true, () => initialize(pageA));
  await assertRestored(threadA, saved);
  await expect(threadB.locator("#input")).toHaveValue(draftB);

  // 未打开会话通过真实消息入口完成，通知不得泄露正文与工具活动。
  await expect(pageB.locator(`section.boxteam-thread[data-session-id="${unopenedSessionId}"]`)).toHaveCount(0);
  const accepted = await api(pageB, `/api/v1/sessions/${unopenedSessionId}/threads/${unopenedThreadId}/messages`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ message: { role: "user", content: "后台完成通知测试" }, run: { mode: "single_agent", agent_id: "default" } }),
  });
  assert.equal(accepted.thread_id, unopenedThreadId);
  let job;
  await waitUntil(async () => {
    job = await api(pageB, `/api/v1/jobs/${accepted.job_id}`);
    if (["failed", "cancelled", "timed_out"].includes(job.status)) throw new Error(`通知执行失败：${JSON.stringify(job)}`);
    return ["completed", "succeeded"].includes(job.status);
  }, "后台任务完成", 60_000);
  await expect(pageB.locator(`button[data-session-id="${unopenedSessionId}"]`)
    .getByRole("status", { name: "会话有未读结果", exact: true })).toBeVisible({ timeout: 30_000 });
  await expect(pageA.locator(`button[data-session-id="${unopenedSessionId}"]`)
    .getByRole("status", { name: "会话有未读结果", exact: true })).toBeVisible({ timeout: 30_000 });
  await pageB.bringToFront();
  let releaseRead;
  const readReleased = new Promise(resolve => { releaseRead = resolve; });
  const matchesRead = url => url.pathname === `/api/v1/sessions/${unopenedSessionId}/main-thread`;
  await pageB.route(matchesRead, async route => {
    const request = route.request();
    if (request.method() !== "GET" || request.headers()["x-boxteam-workspace-id"] !== workspaceId) {
      await route.continue();
      return;
    }
    unreadReadProof.heldRequests.push({ url: request.url(), workspaceId });
    await readReleased;
    await route.continue();
  });
  try {
    await pageB.locator(`button[data-session-id="${unopenedSessionId}"]`).dblclick();
    await waitUntil(() => unreadReadProof.heldRequests.length > 0, "未读会话的真实 main-thread 查询进入延迟点");
    await expect(threadB.getByTestId("composer-input")).toBeVisible();
    await expect(pageB.locator(`section.boxteam-thread[data-session-id="${unopenedSessionId}"]`)).toHaveCount(0);
    const openingRow = pageB.locator(`button[data-session-id="${unopenedSessionId}"]`);
    await expect(openingRow).toHaveAttribute("aria-busy", "true");
    await expect(openingRow.getByRole("img", { name: "正在打开会话", exact: true })).toBeVisible();
    // 树行在打开中以加载图标替换活动指示；用真实搜索中的同一会话观察未读集合。
    await pageB.getByRole("button", { name: "搜索会话", exact: true }).click();
    const searchReady = pageB.waitForResponse(response => {
      const url = new URL(response.url());
      return response.request().method() === "GET" && url.pathname === "/api/gateway/session-catalog/search"
        && url.searchParams.get("query") === unopenedSessionId;
    });
    await pageB.getByRole("searchbox", { name: "查找会话", exact: true }).fill(unopenedSessionId);
    const searchResponse = await searchReady;
    assert.equal(searchResponse.status(), 200, await searchResponse.text());
    const searchPayload = await searchResponse.json();
    assert.ok(searchPayload.request_id);
    assert.equal(searchResponse.headers()["x-request-id"], searchPayload.request_id);
    assert.equal(searchPayload.data.items.length, 1, "精确 Session ID 必须只返回所属会话");
    const found = searchPayload.data.items[0];
    assert.equal(found.workspace_id, workspaceId);
    assert.equal(found.session_id, unopenedSessionId);
    assert.equal(found.node_kind, "session");
    const searchResult = pageB.getByRole("region", { name: "跨工作区搜索结果", exact: true })
      .getByRole("button").filter({ has: pageB.locator("strong", { hasText: found.name }) });
    await expect(searchResult).toHaveCount(1);
    await expect(searchResult.getByRole("status", { name: "会话有未读结果", exact: true })).toBeVisible();
    await expect(threadB.getByTestId("composer-input")).toBeVisible();
    await expect(pageB.locator(`section.boxteam-thread[data-session-id="${unopenedSessionId}"]`)).toHaveCount(0);
    unreadReadProof.search = { requestId: searchPayload.request_id, workspaceId: found.workspace_id,
      sessionId: found.session_id, unreadBeforeEditorOpened: true };
    const responseReady = pageB.waitForResponse(response => matchesRead(new URL(response.url()))
      && response.request().method() === "GET" && response.request().headers()["x-boxteam-workspace-id"] === workspaceId);
    releaseRead();
    const response = await responseReady;
    assert.equal(response.status(), 200, await response.text());
    const payload = await response.json();
    assert.equal(payload.data.session_id, unopenedSessionId);
    assert.equal(payload.data.thread_id, unopenedThreadId);
    assert.ok(payload.request_id);
    assert.equal(response.headers()["x-request-id"], payload.request_id);
    Object.assign(unreadReadProof, { retainedUntilEditorOpened: true, requestId: payload.request_id,
      sessionId: payload.data.session_id, threadId: payload.data.thread_id });
  } finally {
    releaseRead();
    await pageB.unrouteAll({ behavior: "wait" });
  }
  const readThread = pageB.locator(`.editor-instance`).locator(
    `section.boxteam-thread[data-workspace-id="${owner.workspace_id}"][data-session-id="${unopenedSessionId}"][data-thread-id="${unopenedThreadId}"]`,
  );
  await expect(readThread.getByTestId("composer-input")).toBeVisible({ timeout: 30_000 });
  await expect(readThread).toContainText("后台完成通知已生成。");
  await expect(pageB.getByRole("region", { name: "跨工作区搜索结果", exact: true })
    .getByRole("status", { name: "会话有未读结果", exact: true })).toHaveCount(0);
  await pageB.getByRole("button", { name: "搜索会话", exact: true }).click();
  await expect(pageB.locator(`button[data-session-id="${unopenedSessionId}"]`)).toBeVisible();
  await expect(pageB.locator(`button[data-session-id="${unopenedSessionId}"]`)
    .getByRole("status", { name: "会话有未读结果", exact: true })).toHaveCount(0);
  await expect(pageA.locator(`button[data-session-id="${unopenedSessionId}"]`)
    .getByRole("status", { name: "会话有未读结果", exact: true })).toBeVisible();
  const activity = await api(pageB, "/api/v1/session-catalog/events?after=0&limit=100");
  const event = activity.items.find(item => item.session_id === unopenedSessionId);
  assert.ok(event, "必须取得目标会话真实活动事件");
  const activityText = JSON.stringify(event);
  for (const secret of ["后台完成通知测试", "tool_call", "tool_result"]) assert.ok(!activityText.includes(secret));
  assert.deepEqual(serverViewWrites, [], "展示态不应写回 Gateway 的用户视图档案");
  assert.deepEqual(pageErrors, [], "浏览器出现页面异常");
  await writeFile(resultPath, JSON.stringify({ classification: "Integration", guestAccess: true,
    viewRestoredAfterReload: true, viewRestoredInNewPage: true, browserContextsIsolated: true,
    historyCacheBeforeSync: true, historyCacheContextsIsolated: true,
    unopenedSessionUnread: true, openedSessionRead: true, unreadPagesIsolated: true,
    noPrivateActivityContent: true, noServerViewWrites: true,
    owner, routeWorkspaceId: workspaceId, savedView: saved, historyRecovery, unreadReadProof, pageErrors, requests, scrollMeasurements }, null, 2));
} catch (error) {
  failure = error;
} finally {
  const errors = failure ? [failure] : [];
  if (failure && pageA) {
    try {
      await pageA.screenshot({ path: screenshotPath, fullPage: true });
      await writeFile(resultPath.replace(/-result\.json$/, "-diagnostics.json"), JSON.stringify({ error: String(failure), pageErrors, serverViewWrites, requests, scrollMeasurements, historyRecovery, unreadReadProof,
        text: await pageA.locator(threadSelector).locator(".native-thread-conversation").innerText() }, null, 2));
    } catch (error) { errors.push(error); }
  }
  try { await scope.close(); } catch (error) { errors.push(error); }
  if (errors.length) throw new AggregateError(errors, "浏览器持久视图验收失败");
}
