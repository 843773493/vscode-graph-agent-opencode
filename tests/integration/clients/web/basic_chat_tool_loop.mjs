import { writeFile } from "node:fs/promises";
import process from "node:process";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";
import { expect } from "playwright/test";
import { testControl } from "../../../support/e2e_control/browser_client.mjs";
import { webSelectors } from "../../../clients/selectors/web-selectors.mjs";
import { TestRunContext } from "../../../harness/js/run-context.mjs";

function requiredEnvironment(name) {
  const value = process.env[name];
  if (!value) throw new Error(`缺少环境变量 ${name}`);
  return value;
}

const baseUrl = requiredEnvironment("BOXTEAM_BROWSER_BASE_URL");
const expectedWorkspaceId = requiredEnvironment("BOXTEAM_BROWSER_WORKSPACE_ID");
const resultPath = requiredEnvironment("BOXTEAM_BROWSER_RESULT_PATH");
const screenshotPath = requiredEnvironment("BOXTEAM_BROWSER_SCREENSHOT_PATH");
const userMessage = "请读取 README.md，然后告诉我工具调用是否完成。";
const expectedFinalText = "浏览器 SSE 工具调用已完成。";
const executablePath =
  process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH || undefined;

async function localToken(page) {
  return page.evaluate(async () => {
    const response = await fetch("/api/gateway/auth/local-credential");
    if (!response.ok) {
      throw new Error(`获取本地凭据失败: HTTP ${response.status}`);
    }
    const payload = await response.json();
    if (typeof payload?.data?.token !== "string") {
      throw new Error("本地凭据缺少 token");
    }
    return payload.data.token;
  });
}

async function api(page, pathname, init = {}) {
  const token = await localToken(page);
  return page.evaluate(
    async ({ pathname: path, init: requestInit, token: localTokenValue }) => {
      const headers = new Headers(requestInit.headers ?? {});
      headers.set("X-Local-Token", localTokenValue);
      const response = await fetch(path, {
        ...requestInit,
        headers,
        credentials: "include",
      });
      const body = await response.text();
      if (!response.ok) throw new Error(`API ${response.status}: ${body}`);
      return body ? JSON.parse(body) : null;
    },
    { pathname, init, token },
  );
}

async function waitUntil(predicate, label, timeout = 30_000) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    if (await predicate()) return;
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  throw new Error(`等待${label}超时`);
}

async function ensureGuest(page) {
  const target = new URL(baseUrl);
  await page.goto(target.href, { waitUntil: "domcontentloaded", timeout: 30_000 });
  const current = await page.evaluate(async () => {
    const response = await fetch("/api/gateway/users/current");
    return { status: response.status, body: await response.text() };
  });
  if (current.status === 401) {
    await api(page, "/api/gateway/users/guest", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ tracking: { source: "basic-chat-tool-loop" } }),
    });
    await page.reload({ waitUntil: "domcontentloaded" });
  } else if (current.status !== 200) {
    throw new Error(`检查游客访问失败: ${current.status} ${current.body}`);
  }
}

function isSessionMessageRequest(request) {
  return request.method() === "POST"
    && /\/api\/v1\/sessions\/[^/]+\/threads\/[^/]+\/messages$/.test(new URL(request.url()).pathname);
}

async function waitForCompletedJob(page, jobId, headers) {
  await waitUntil(async () => {
    const job = (await api(page, `/api/v1/jobs/${jobId}`, { headers })).data;
    if (["failed", "cancelled", "timed_out", "execution_lost"].includes(job.status)) throw new Error(JSON.stringify(job));
    return job.status === "completed";
  }, `Job ${jobId} 结算`);
}

async function verifyPreviewTabs(page, owner) {
  const headers = { "Content-Type": "application/json", "X-BoxTeam-Workspace-Id": owner.routeId };
  const group = page.locator(".editor-group-container");
  await expect(group).toHaveCount(1);
  const tab = threadId => group.locator(`.tabs-container .tab[data-resource-name="${threadId}"]`);
  const mainTab = tab(owner.threadId);
  await expect(mainTab.locator(".tab-label.italic")).toBeVisible();

  const spare = (await api(page, "/api/v1/sessions", {
    method: "POST", headers, body: JSON.stringify({ title: "原版预览标签验证", agent_id: "default" }),
  })).data;
  expect(spare.workspace_id).toBe(owner.workspaceId);
  const spareThread = (await api(page, `/api/v1/sessions/${spare.session_id}/main-thread`, { headers })).data;
  expect(spareThread.session_id).toBe(spare.session_id);
  const spareRow = page.locator(`button[data-session-id="${spare.session_id}"]`);
  const mainRow = page.locator(`button[data-session-id="${owner.sessionId}"]`);
  const spareTab = tab(spareThread.thread_id);
  await spareRow.click();
  await expect(spareTab.locator(".tab-label.italic")).toBeVisible();
  await expect(mainTab).toHaveCount(0);

  await spareRow.dblclick();
  await expect(spareTab.locator(".tab-label.italic")).toHaveCount(0);
  await mainRow.click();
  await expect(mainTab.locator(".tab-label.italic")).toBeVisible();
  await expect(spareTab).toBeVisible();
  await mainTab.dblclick();
  await expect(mainTab.locator(".tab-label.italic")).toHaveCount(0);

  // 只延迟真实主 Thread 查询：用户随后选择原版 Tab，迟到导航不能抢回活动编辑器。
  const held = [];
  let release;
  const gate = new Promise(resolve => { release = resolve; });
  const navigationUrl = url => url.pathname === `/api/v1/sessions/${owner.sessionId}/main-thread`;
  const holdNavigation = async route => {
    const request = route.request();
    if (request.method() !== "GET" || request.headers()["x-boxteam-workspace-id"] !== owner.routeId) {
      await route.continue();
      return;
    }
    const pending = gate.then(() => route.continue());
    held.push(pending);
    await pending;
  };
  await page.route(navigationUrl, holdNavigation);
  try {
    await mainRow.click();
    await expect.poll(() => held.length).toBe(1);
    await spareTab.click();
    await expect(spareTab).toHaveClass(/\bactive\b/);
    const responsePromise = page.waitForResponse(response => navigationUrl(new URL(response.url()))
      && response.request().headers()["x-boxteam-workspace-id"] === owner.routeId);
    release();
    const response = await responsePromise;
    expect(response.status()).toBe(200);
    const payload = await response.json();
    expect(payload.request_id).toBe(response.headers()["x-request-id"]);
    expect(payload.data.session_id).toBe(owner.sessionId);
    expect(payload.data.thread_id).toBe(owner.threadId);
    await expect(mainRow).not.toHaveAttribute("aria-busy", "true");
    await expect(spareTab).toHaveClass(/\bactive\b/);
    await expect(mainTab).not.toHaveClass(/\bactive\b/);
  } finally {
    release();
    await Promise.all(held);
    await page.unroute(navigationUrl, holdNavigation);
  }

  // 通过原版关闭按钮释放 EditorPane，再从导航重开；关闭不能变成删除业务会话。
  for (const current of [spareTab, mainTab]) {
    await current.hover();
    await current.getByRole("button", { name: /^Close(?: \(|$)/ }).click();
    await expect(current).toHaveCount(0);
  }
  await expect(group.locator("section.boxteam-thread")).toHaveCount(0);
  for (const sessionId of [owner.sessionId, spare.session_id]) {
    const session = (await api(page, `/api/v1/sessions/${sessionId}`, { headers })).data;
    expect(session.session_id).toBe(sessionId);
    expect(session.workspace_id).toBe(owner.workspaceId);
  }
  await mainRow.click();
  const restored = group.locator(`section.boxteam-thread[data-workspace-id="${owner.workspaceId}"][data-session-id="${owner.sessionId}"][data-thread-id="${owner.threadId}"]`);
  await expect(restored.locator(webSelectors.composer)).toBeVisible();
  await expect(mainTab.locator(".tab-label.italic")).toBeVisible();
  return { spareSessionId: spare.session_id, spareThreadId: spareThread.thread_id,
    previewReplaced: true, treeDoubleClickPinned: true, tabDoubleClickPinned: true,
    closePreservedSessions: true, reopenedThreadId: owner.threadId,
    lateNavigationPreservedActiveTab: true };
}

async function verifyClipboardFailure(page, owner, composer) {
  const detail = "fixture-clipboard-operation-rejected";
  const draft = "右键操作失败后保留的聊天草稿";
  await composer.fill(draft);
  // 只替换浏览器剪贴板能力及其既有兼容复制；Gateway和业务API保持真实。
  await page.evaluate(detail => {
    const target = navigator.clipboard;
    if (!target) throw new Error("本场景要求浏览器真实提供Clipboard API");
    const writeDescriptor = Object.getOwnPropertyDescriptor(target, "writeText");
    const copyDescriptor = Object.getOwnPropertyDescriptor(document, "execCommand");
    const counts = { write: 0, fallback: 0 };
    Object.defineProperty(target, "writeText", { configurable: true, value: async () => {
      counts.write += 1; throw new Error(detail);
    } });
    Object.defineProperty(document, "execCommand", { configurable: true, value: command => {
      if (command !== "copy") throw new Error(`未声明的剪贴板装具命令：${command}`);
      counts.fallback += 1; return false;
    } });
    globalThis.clipboardFailureFixture = { counts, restore() {
      if (writeDescriptor) Object.defineProperty(target, "writeText", writeDescriptor);
      else delete target.writeText;
      if (copyDescriptor) Object.defineProperty(document, "execCommand", copyDescriptor);
      else delete document.execCommand;
    } };
  }, detail);
  try {
    await page.locator(`button[data-session-id="${owner.sessionId}"]`).click({ button: "right" });
    await page.getByRole("menuitem", { name: "复制 ID", exact: true }).click();
    const message = `复制会话 ID 失败 (${owner.routeId}/${owner.sessionId}): Clipboard API 失败：${detail}；兼容复制失败：浏览器拒绝复制文本`;
    await expect(page.locator(".notifications-toasts").getByText(message, { exact: true })).toBeVisible();
    await expect(composer).toHaveValue(draft);
    const counts = await page.evaluate(() => ({ ...globalThis.clipboardFailureFixture.counts }));
    expect(counts).toEqual({ write: 1, fallback: 1 });
    await composer.fill("");
    return { boundary: "浏览器Clipboard API及兼容复制为显式拒绝替身，无业务HTTP替身",
      routeId: owner.routeId, sessionId: owner.sessionId, message, counts, errorVisible: true, draftPreserved: true };
  } finally {
    await page.evaluate(() => { globalThis.clipboardFailureFixture.restore(); delete globalThis.clipboardFailureFixture; });
  }
}

async function verifyParallelThreads(page, owner, initialJobId) {
  const headers = { "Content-Type": "application/json", "X-BoxTeam-Workspace-Id": owner.routeId };
  await waitForCompletedJob(page, initialJobId, headers);
  const source = (await api(page, `/api/v1/sessions/${owner.sessionId}/fork-source-point`, { headers })).data;
  const fork = (await api(page, `/api/v1/sessions/${owner.sessionId}/fork-context`, {
    method: "POST", headers, body: JSON.stringify({
      idempotency_key: "workbench-parallel-child", source_uri: source.source_uri,
      source_view_id: source.source_view_id, source_view_revision: source.source_view_revision,
      anchor_item_id: source.anchor_item_id, target_container_uri: source.child_container_uri,
      mode: "view_prefix_fork", creation_kind: "new_child_thread",
    }),
  })).data;
  expect(fork.session.session_id).toBe(owner.sessionId);
  expect(fork.thread_id).not.toBe(owner.threadId);

  // 固定主 Tab，再通过原版分屏命令和真实资源面板打开 child。
  await page.locator(`button[data-session-id="${owner.sessionId}"]`).dblclick();
  await page.evaluate(() => globalThis.workbench.commands.executeCommand("workbench.action.splitEditorRight"));
  const groups = page.locator(".editor-group-container");
  await expect(groups).toHaveCount(2);
  const threadSelector = `section.boxteam-thread[data-workspace-id="${owner.workspaceId}"][data-session-id="${owner.sessionId}"]`;
  const main = groups.nth(0).locator(`${threadSelector}[data-thread-id="${owner.threadId}"]`);
  const mainResourceToggle = main.getByRole("button", { name: "切换会话资源", exact: true });
  await expect(mainResourceToggle).toHaveAttribute("aria-expanded", "false");
  const right = groups.nth(1).locator(threadSelector);
  await expect(right).toBeVisible();
  const resourceToggle = right.getByRole("button", { name: "切换会话资源", exact: true });
  if (await resourceToggle.getAttribute("aria-expanded") !== "true") await resourceToggle.click();
  await right.getByRole("tab", { name: "运行与连接", exact: true }).click();
  await expect(mainResourceToggle).toHaveAttribute("aria-expanded", "false");
  await expect(main.getByRole("tab", { name: "运行与连接", exact: true })).toHaveCount(0);
  await right.locator(`.child-thread-item[data-thread-id="${fork.thread_id}"] .child-thread-main`).click();
  const child = groups.nth(1).locator(`${threadSelector}[data-thread-id="${fork.thread_id}"]`);
  await expect(main).toBeVisible();
  await expect(child).toBeVisible();
  const mainComposer = main.locator(webSelectors.composer);
  const childComposer = child.locator(webSelectors.composer);
  const messages = ["仅主线程输入：alpha", "仅子线程输入：beta"];
  const replies = ["主线程独立回复 alpha", "子线程独立回复 beta"];
  await mainComposer.fill(messages[0]);
  await childComposer.fill(messages[1]);
  await mainComposer.click();
  await expect(mainComposer).toHaveValue(messages[0]);
  await expect(childComposer).toHaveValue(messages[1]);

  const calls = [];
  const armed = [];
  const receipts = [];
  const targets = [main, child];
  try {
    for (const [index, threadId] of [owner.threadId, fork.thread_id].entries()) {
      const planned = await testControl({ action: "plan_model", session_id: owner.sessionId, thread_id: threadId,
        interactions: [JSON.parse(requiredEnvironment("BOXTEAM_BROWSER_PARALLEL_INTERACTIONS"))[index]] });
      expect(planned).toHaveLength(1);
      calls.push({ threadId, callId: planned[0], message: messages[index], reply: replies[index] });
      await testControl({ action: "arm", operation_id: planned[0], phase: "model.response_ready" });
      armed.push(planned[0]);
    }
    for (const [index, target] of targets.entries()) {
      const path = `/api/v1/sessions/${owner.sessionId}/threads/${calls[index].threadId}/messages`;
      const response = page.waitForResponse(entry => entry.request().method() === "POST"
        && new URL(entry.url()).pathname === path);
      await target.locator(webSelectors.sendButton).click();
      const sent = await response;
      expect(sent.status()).toBe(200);
      expect(sent.request().headers()["x-boxteam-workspace-id"]).toBe(owner.routeId);
      expect(sent.request().postDataJSON().message.content).toBe(messages[index]);
      const receipt = (await sent.json()).data;
      expect(receipt.thread_id).toBe(calls[index].threadId);
      receipts.push(receipt);
      await testControl({ action: "acknowledged", operation_id: calls[index].callId, phase: "model.response_ready" });
    }
    // 两个模型调用同时被精确 owner 屏障持有，证明 child 没有排在 main 后面。
    for (const receipt of receipts) {
      const job = (await api(page, `/api/v1/jobs/${receipt.job_id}`, { headers })).data;
      expect(job.status).toBe("running");
    }
    const interruptFailure = await verifyInterruptFailure(page, owner, main, child, receipts[0]);
    await testControl({ action: "release", operation_id: calls[1].callId, phase: "model.response_ready" });
    const childTurn = child.locator(`.chat-turn[data-turn-id="${receipts[1].turn_id}"]`);
    await expect(childTurn.getByText(replies[1], { exact: true })).toBeVisible();
    await expect(main.getByText(replies[1], { exact: true })).toHaveCount(0);
    await expect(main.getByText(replies[0], { exact: true })).toHaveCount(0);
    await testControl({ action: "release", operation_id: calls[0].callId, phase: "model.response_ready" });
    const mainTurn = main.locator(`.chat-turn[data-turn-id="${receipts[0].turn_id}"]`);
    await expect(mainTurn.getByText(replies[0], { exact: true })).toBeVisible();
    await expect(child.getByText(replies[0], { exact: true })).toHaveCount(0);
    await expect(main.locator(`.chat-turn[data-turn-id="${receipts[1].turn_id}"]`)).toHaveCount(0);
    await expect(child.locator(`.chat-turn[data-turn-id="${receipts[0].turn_id}"]`)).toHaveCount(0);
    for (const receipt of receipts) await waitForCompletedJob(page, receipt.job_id, headers);
    return { sessionId: owner.sessionId, calls, receipts, draftsIsolated: true, concurrentCalls: true,
      repliesIsolated: true, groupResourcesIsolated: true, interruptFailure };
  } finally {
    for (const callId of armed) await testControl({ action: "release", operation_id: callId, phase: "model.response_ready" });
  }
}

async function verifyInterruptFailure(page, owner, main, child, receipt) {
  const detail = "停止被拒绝（显式 HTTP 拒绝装具）";
  const path = `/api/v1/sessions/${owner.sessionId}/interrupt`;
  const url = new URL(`${path}?thread_id=${owner.threadId}`, baseUrl).href;
  const interceptions = [];
  let rejectedAt;
  const reject = async route => {
    const request = route.request();
    expect(request.method()).toBe("POST");
    expect(request.headers()["x-boxteam-workspace-id"]).toBe(owner.routeId);
    rejectedAt = Date.now();
    interceptions.push({ path, threadId: new URL(request.url()).searchParams.get("thread_id"),
      routeId: request.headers()["x-boxteam-workspace-id"] });
    await route.fulfill({ status: 409, contentType: "application/json", body: JSON.stringify({ detail }) });
  };
  await page.route(url, reject);
  try {
    const draft = "停止失败后保留的下一条输入";
    await main.locator(webSelectors.composer).fill(draft);
    const recovery = page.waitForResponse(response => {
      const request = response.request(), target = new URL(request.url());
      return rejectedAt && request.timing().startTime >= rejectedAt && request.method() === "GET"
        && target.pathname === `/api/v1/sessions/${owner.sessionId}/pending-requests`
        && target.searchParams.get("thread_id") === owner.threadId
        && request.headers()["x-boxteam-workspace-id"] === owner.routeId && response.status() === 200;
    });
    await main.locator("#interruptButton").click();
    const response = await recovery, snapshot = await response.json();
    expect(response.headers()["x-request-id"]).toBe(snapshot.request_id);
    expect(snapshot.request_id).toBeTruthy();
    expect(snapshot.data.session_id).toBe(owner.sessionId);
    expect(snapshot.data.thread_id).toBe(owner.threadId);
    expect(snapshot.data.active_job_id).toBe(receipt.job_id);
    await expect(main.getByRole("alert")).toContainText(detail);
    await expect(child.getByText(detail, { exact: false })).toHaveCount(0);
    await expect(main.locator(webSelectors.composer)).toHaveValue(draft);
    await expect(main.locator("#interruptButton")).toBeVisible();
    expect(interceptions).toHaveLength(1);
    return { boundary: "仅精确 main Thread 的 interrupt POST 使用409替身；队列重读和双Thread模型执行经过真实Gateway",
      interceptions, requestId: snapshot.request_id, sessionId: snapshot.data.session_id,
      threadId: snapshot.data.thread_id, activeJobId: snapshot.data.active_job_id,
      errorVisible: true, errorIsolated: true, draftPreserved: true };
  } finally {
    await page.unroute(url, reject);
  }
}

async function verifyNativeFileEditor(page, owner) {
  const headers = { "X-BoxTeam-Workspace-Id": owner.routeId };
  const listing = (await api(page, "/api/v1/workspace/files", { headers })).data;
  const file = listing.items.find(item => item.name === "README.md");
  expect(file).toBeDefined();
  const group = page.locator(".editor-group-container").nth(0);
  const main = group.locator(`section.boxteam-thread[data-workspace-id="${owner.workspaceId}"][data-session-id="${owner.sessionId}"][data-thread-id="${owner.threadId}"]`);
  const toggle = main.getByRole("button", { name: "切换会话资源", exact: true });
  if (await toggle.getAttribute("aria-expanded") !== "true") await toggle.click();
  await main.getByRole("tab", { name: "文件", exact: true }).click();
  // 根按钮本身承载 title，避免查询另一个工作区或文件系统根。
  const rootRow = main.getByTitle(listing.path, { exact: true }).and(main.locator("button.root"));
  await expect(rootRow).toBeVisible();
  if (await rootRow.getAttribute("aria-expanded") !== "true") await rootRow.click();
  const row = main.getByTitle(file.path, { exact: true }).and(main.locator("button.files-tree-item"));
  const tabs = group.locator(".tabs-container .tab");
  const before = await tabs.count();
  await row.click();
  await expect(main.locator('[aria-label="文件代码预览区"]:visible')).toContainText("统一测试工作区");
  await expect(tabs).toHaveCount(before);
  await row.click({ button: "right" });
  await main.getByRole("menuitem", { name: "VS Code", exact: true }).click();
  // 原版 Menu 延迟 100ms 安装 mouseup，防止打开菜单的那次点击误触动作。
  await page.getByRole("menuitem", { name: "在编辑器中打开", exact: true }).click({ delay: 120 });
  await expect(tabs).toHaveCount(before + 1);
  // 原版 Monaco 在 Chromium 中使用 EditContext，按文本输入语义定位。
  const editor = group.locator(".monaco-editor:visible").getByRole("textbox");
  await expect(editor).toBeVisible();
  const query = new URLSearchParams({ scope: "workspace", path: file.path });
  const original = (await api(page, `/api/v1/workspace/files/content?${query}`, { headers })).data;
  const content = `${original.content}\nWorkbench 原生编辑器保存验证\n`;
  await editor.press("ControlOrMeta+A");
  await page.keyboard.insertText(content);
  const response = page.waitForResponse(entry => entry.request().method() === "PUT"
    && new URL(entry.url()).pathname === "/api/v1/workspace/files/content"
    && new URL(entry.url()).searchParams.get("path") === file.path);
  await editor.press("ControlOrMeta+S");
  const saved = await response;
  expect(saved.status()).toBe(200);
  expect(saved.request().headers()["x-boxteam-workspace-id"]).toBe(owner.routeId);
  expect(saved.request().postDataJSON()).toEqual({ content, expected_revision: original.revision });
  const result = (await saved.json()).data;
  expect(result.path).toBe(file.path);
  expect(result.content).toBe(content);
  expect(result.revision).not.toBe(original.revision);
  return { path: file.path, content, revision: result.revision, inlinePreviewFirst: true, nativeEditorSaved: true };
}

async function verifyReplayFailure(page, owner, turnId, messageId, jobId) {
  const headers = { "X-BoxTeam-Workspace-Id": owner.routeId };
  await waitForCompletedJob(page, jobId, headers);
  expect(typeof messageId).toBe("string");
  expect(messageId.length).toBeGreaterThan(0);
  const thread = page.locator(`section.boxteam-thread[data-workspace-id="${owner.workspaceId}"][data-session-id="${owner.sessionId}"][data-thread-id="${owner.threadId}"]`);
  const turn = thread.locator(`.chat-turn[data-turn-id="${turnId}"]`);
  const composer = thread.locator(webSelectors.composer);
  const draft = "编辑失败后仍保留的下一条草稿";
  const edited = "被拒绝的编辑内容，不能进入后端历史";
  const detail = "上下文窗口已失效，无法回放（显式 HTTP 拒绝装具）";
  const replayPath = `/api/v1/sessions/${owner.sessionId}/messages/${messageId}/replay`;
  const bootstrapPath = `/api/v1/sessions/${owner.sessionId}/bootstrap`;
  const before = (await api(page, `${bootstrapPath}?thread_id=${owner.threadId}`, { headers })).data;
  const interceptions = [];
  // 只替代这一条编辑请求的拒绝响应；后续历史重读和继续发送仍经过真实 Gateway。
  const rejectReplay = async route => {
    const request = route.request();
    expect(request.method()).toBe("POST");
    expect(request.headers()["x-boxteam-workspace-id"]).toBe(owner.routeId);
    expect(request.postDataJSON()).toEqual({ action: "edit_and_continue", content: edited, acknowledge_context_only: true });
    interceptions.push({ method: request.method(), path: new URL(request.url()).pathname,
      routeId: request.headers()["x-boxteam-workspace-id"], payload: request.postDataJSON() });
    await route.fulfill({ status: 409, contentType: "application/json", body: JSON.stringify({ detail }) });
  };
  const replayUrl = new URL(replayPath, baseUrl).href;
  await page.route(replayUrl, rejectReplay);
  try {
    await composer.fill(draft);
    await turn.getByRole("button", { name: "消息操作", exact: true }).click();
    await thread.getByRole("menuitem", { name: "编辑并从此处继续", exact: true }).click();
    const edit = turn.getByRole("textbox", { name: "编辑用户消息", exact: true });
    await expect(edit).toHaveValue(userMessage);
    await edit.fill(edited);
    const recovery = page.waitForResponse(response => {
      const request = response.request();
      const url = new URL(request.url());
      return request.method() === "GET" && url.pathname === bootstrapPath
        && url.searchParams.get("thread_id") === owner.threadId
        && request.headers()["x-boxteam-workspace-id"] === owner.routeId && response.status() === 200;
    }, { timeout: 30_000 });
    const [response] = await Promise.all([
      recovery, turn.getByRole("button", { name: "编辑并从此处继续", exact: true }).click(),
    ]);
    const refreshed = await response.json();
    expect(response.headers()["x-request-id"]).toBe(refreshed.request_id);
    expect(refreshed.request_id).toBeTruthy();
    expect(refreshed.data.session.session_id).toBe(owner.sessionId);
    expect(refreshed.data.session.workspace_id).toBe(owner.workspaceId);
    expect(refreshed.data.thread_id).toBe(owner.threadId);
    expect(refreshed.data.latest_turn.turn_id).toBe(turnId);
    expect(refreshed.data.projection_epoch).toBe(before.projection_epoch);
    expect(refreshed.data.active_job_id ?? null).toBeNull();
    await expect(turn.getByRole("alert")).toContainText(detail);
    await expect(turn.getByText(expectedFinalText, { exact: true })).toBeVisible();
    await expect(edit).toHaveValue(edited);
    await expect(edit).toBeEnabled();
    await expect(composer).toHaveValue(draft);
    await expect(composer).toBeEnabled();
    await expect(thread.locator(webSelectors.sendButton)).toBeEnabled();
    await expect(thread.locator(".app-fatal-error,.error-state")).toHaveCount(0);
    expect(interceptions).toHaveLength(1);
    await turn.getByRole("button", { name: "取消", exact: true }).click();
    await expect(edit).toHaveCount(0);
    await expect(turn.getByRole("alert")).toHaveCount(0);
    await expect(composer).toHaveValue(draft);
    return { boundary: "仅精确 replay POST 使用显式 409 替身；真实 Gateway 重读与后续发送不拦截",
      interceptions, requestId: refreshed.request_id, threadId: refreshed.data.thread_id, turnId,
      errorVisible: true, editPreserved: true, draftPreserved: true, authoritativeHistoryReloaded: true,
      chatOperable: true, projectionEpoch: refreshed.data.projection_epoch };
  } finally {
    await page.unroute(replayUrl, rejectReplay);
  }
}

async function verifyDeletionRefresh(page, owner, tabs) {
  const headers = { "Content-Type": "application/json", "X-BoxTeam-Workspace-Id": owner.routeId };
  const source = { sessionId: tabs.spareSessionId, threadId: tabs.spareThreadId };
  const row = page.locator(`button[data-session-id="${source.sessionId}"]`);
  await page.evaluate(() => globalThis.workbench.commands.executeCommand("workbench.action.joinAllGroups"));
  const groups = page.locator(".editor-group-container");
  await expect(groups).toHaveCount(1);
  await row.dblclick();
  await expect(groups.nth(0).locator(`section.boxteam-thread[data-session-id="${source.sessionId}"][data-thread-id="${source.threadId}"]`)
    .getByTestId("composer-input")).toBeVisible();
  await page.evaluate(() => globalThis.workbench.commands.executeCommand("workbench.action.splitEditorRight"));
  await expect(groups).toHaveCount(2);
  const sourceTabs = page.locator(`.tabs-container .tab[data-resource-name="${source.threadId}"]`);
  await expect(sourceTabs).toHaveCount(2);
  // 先将两个原版标签真实持久化，后续立即刷新不能靠恰好尚未保存而通过。
  await page.evaluate(() => globalThis.boxteamWorkbenchLifecycle.prepareToClose());
  const snapshot = (await api(page, "/api/v1/session-catalog/snapshot", { headers })).data;
  const operationId = `op_${Bun.randomUUIDv7().replaceAll("-", "")}`;
  const acceptance = await api(page, "/api/v1/session-catalog/operations:enqueue", {
    method: "POST", headers, body: JSON.stringify({ intents: [{
      client_operation_id: operationId, client_sequence: 1, base_catalog_revision: snapshot.catalog_revision,
      kind: "delete_session", target_node_id: source.sessionId,
    }] }),
  });
  expect(acceptance.data.accepted_count).toBe(1);
  let receipt;
  await waitUntil(async () => {
    receipt = (await api(page, `/api/v1/session-catalog/operations?operation_id=${operationId}`, { headers })).data.items[0];
    return receipt?.state === "committed" && receipt.settlement === "settled";
  }, "删除会话完整提交");
  await expect(sourceTabs).toHaveCount(0);
  await expect(page.locator(`section.boxteam-thread[data-session-id="${source.sessionId}"]`)).toHaveCount(0);
  const restoredReads = [];
  const record = request => {
    if (new URL(request.url()).pathname === `/api/v1/sessions/${source.sessionId}`) {
      restoredReads.push({ url: request.url(), routeId: request.headers()["x-boxteam-workspace-id"] });
    }
  };
  page.on("request", record);
  try {
    // 此处验证用户直接刷新，不能提前 flush 替产品保存删除后的展示态。
    await page.reload({ waitUntil: "domcontentloaded" });
    await expect(page.locator("#workbench .monaco-workbench")).toBeVisible();
    await expect(page.locator(`button[data-session-id="${owner.sessionId}"]`)).toBeVisible();
    await page.evaluate(() => globalThis.workbench.commands.executeCommand("workbench.action.focusFirstEditorGroup"));
    await page.locator(`button[data-session-id="${owner.sessionId}"]`).dblclick();
    await expect(groups.nth(0).locator(`section.boxteam-thread[data-session-id="${owner.sessionId}"][data-thread-id="${owner.threadId}"]`)
      .getByTestId("composer-input")).toBeVisible();
    await expect(sourceTabs).toHaveCount(0);
    expect(restoredReads, "删除后立即刷新不得从旧布局恢复已删除 Session").toEqual([]);
    return { ...source, operationId, receipt, removedFromBothGroups: true, restoredReads, survivorOperable: true };
  } finally { page.off("request", record); }
}

const runContext = TestRunContext.fromTestFile(fileURLToPath(import.meta.url));
let page;
let executionError;
const pageErrors = [];
const streamResponses = [];
const traceStreams = [];
let result = { phase: "browser-setup" };

try {
  const browser = await chromium.launch({ executablePath, headless: true });
  runContext.addCleanup("聊天 Chromium", () => browser.close());
  const context = await browser.newContext({
    viewport: { width: 1920, height: 1100 },
  });
  runContext.addCleanup("聊天 context", () => context.close());
  page = await context.newPage();
  page.on("pageerror", (error) => pageErrors.push(String(error)));
  page.on("response", (response) => {
    if (
      response.status() === 200
      && (response.url().includes("/workspace/observations/stream")
        || response.url().includes("/message-stream?"))
    ) {
      streamResponses.push(response.url());
      if (response.url().includes("/workspace/observations/stream")) {
        const subscriptions = response.request().postDataJSON().subscriptions;
        if (response.headers()["content-type"]?.includes("text/event-stream")) {
          traceStreams.push(...subscriptions.filter((entry) => entry.kind === "session_trace"));
        }
      }
    }
  });
  result.phase = "gateway-bootstrap";
  await ensureGuest(page);
  const workspaces = await api(page, "/api/gateway/workspaces");
  const workspaceId = workspaces?.data?.active_workspace_id;
  if (workspaceId !== expectedWorkspaceId) {
    throw new Error(
      `Gateway active workspace 不一致: expected=${expectedWorkspaceId}, actual=${workspaceId}`,
    );
  }

  const workspaceHeaders = {
    "Content-Type": "application/json",
    "X-BoxTeam-Workspace-Id": workspaceId,
  };
  result.phase = "session-create";
  result.workspaceId = workspaceId;
  const created = await api(page, "/api/v1/sessions", {
    method: "POST",
    headers: workspaceHeaders,
    body: JSON.stringify({ title: "浏览器工具循环", agent_id: "default" }),
  });
  const sessionId = created?.data?.session_id;
  if (typeof sessionId !== "string" || !sessionId) {
    throw new Error("创建浏览器工具循环会话缺少 session_id");
  }
  const updated = await api(page, `/api/v1/sessions/${sessionId}`, {
    method: "PATCH",
    headers: workspaceHeaders,
    body: JSON.stringify({ provider_id: "primary" }),
  });
  if (updated?.data?.current_provider_id !== "primary") {
    throw new Error("浏览器工具循环会话没有切换到 primary provider");
  }
  const mainThread = await api(page, `/api/v1/sessions/${sessionId}/main-thread`, { headers: workspaceHeaders });
  const threadId = mainThread.data.thread_id;
  Object.assign(result, { sessionId, threadId, businessWorkspaceId: created.data.workspace_id });
  const modelCallIds = await testControl({
    action: "plan_model", session_id: sessionId, thread_id: threadId,
    interactions: JSON.parse(requiredEnvironment("BOXTEAM_BROWSER_MODEL_INTERACTIONS")),
  });
  result.modelCallIds = modelCallIds;

  result.phase = "thread-open";
  await page.reload({ waitUntil: "domcontentloaded" });
  const sessionButton = page.locator(`button[data-session-id="${sessionId}"]`);
  await sessionButton.waitFor({ state: "visible", timeout: 30_000 });
  await sessionButton.click();
  const thread = page.locator(`section.boxteam-thread[data-workspace-id="${created.data.workspace_id}"][data-session-id="${sessionId}"][data-thread-id="${threadId}"]`);
  await thread.waitFor({ state: "visible", timeout: 30_000 });
  const composer = thread.locator(webSelectors.composer);
  await composer.waitFor({ state: "visible", timeout: 30_000 });
  await waitUntil(
    async () => traceStreams.some((entry) => entry.session_id === sessionId),
    "会话 trace SSE",
  );
  result.phase = "tab-lifecycle";
  const tabs = await verifyPreviewTabs(page, { routeId: workspaceId,
    workspaceId: created.data.workspace_id, sessionId, threadId });
  result.tabs = tabs;

  result.phase = "navigation-clipboard-failure";
  result.clipboardFailure = await verifyClipboardFailure(page, { routeId: workspaceId, sessionId }, composer);

  result.phase = "send-message";
  const sendRequestPromise = page.waitForRequest(isSessionMessageRequest, {
    timeout: 30_000,
  });
  const sendResponsePromise = page.waitForResponse(
    (response) => isSessionMessageRequest(response.request()) && response.status() === 200,
    { timeout: 30_000 },
  );
  await composer.fill(userMessage);
  await thread.locator(webSelectors.sendButton).click();
  const [sendRequest, sendResponse] = await Promise.all([
    sendRequestPromise,
    sendResponsePromise,
  ]);
  const sentPayload = sendRequest.postDataJSON();
  if (sendRequest.headers()["x-boxteam-workspace-id"] !== workspaceId
    || new URL(sendRequest.url()).pathname !== `/api/v1/sessions/${sessionId}/threads/${threadId}/messages`) {
    throw new Error(`Composer 发送 owner 不一致: ${JSON.stringify(sentPayload)}`);
  }
  if (sentPayload?.message?.content !== userMessage) {
    throw new Error(`Composer 发送内容不一致: ${JSON.stringify(sentPayload)}`);
  }
  if (sendResponse.status() !== 200) {
    throw new Error(`Composer 发送失败: HTTP ${sendResponse.status()}`);
  }
  const accepted = (await sendResponse.json()).data;
  if (accepted.thread_id !== threadId) throw new Error("Composer acceptance Thread 归属不一致");
  const turnId = accepted?.turn_id;
  if (typeof turnId !== "string" || !turnId) {
    throw new Error("Composer acceptance 缺少 turn_id");
  }
  Object.assign(result, { turnId, jobId: accepted.job_id });
  const turn = thread.locator(`.chat-turn[data-turn-id="${turnId}"]`);

  result.phase = "message-stream";
  await waitUntil(
    async () => streamResponses.some((url) => url.includes("/message-stream?")),
    "Turn message SSE",
  );
  result.phase = "final-reply";
  await waitUntil(
    async () => turn.getByText(expectedFinalText, { exact: true }).isVisible(),
    "浏览器最终回复",
  );
  result.phase = "tool-details";
  const thinkingToggle = turn.getByRole("button", { name: /^展开 Turn 中间消息：/ });
  await thinkingToggle.waitFor({ state: "visible", timeout: 30_000 });
  await thinkingToggle.click();
  const completedTool = turn.locator(".chat-tool-row.is-complete").filter({
    hasText: "已运行 read_file",
  });
  await completedTool.waitFor({ state: "visible", timeout: 30_000 });
  await completedTool.locator(".chat-tool-summary").click();
  await completedTool.getByText("README.md", { exact: false }).waitFor({
    state: "visible",
    timeout: 30_000,
  });
  const completedToolText = await completedTool.innerText();
  if (
    completedToolText.includes("未完成")
    || completedToolText.includes("失败")
    || completedToolText.includes("未知")
  ) {
    throw new Error(`read_file 工具执行状态异常: ${completedToolText}`);
  }
  if (!completedToolText.includes("README.md")) {
    throw new Error(`read_file 工具详情缺少输入参数: ${completedToolText}`);
  }

  const completedToolVisible = await completedTool.isVisible();
  const owner = { routeId: workspaceId, workspaceId: created.data.workspace_id, sessionId, threadId };
  result.phase = "replay-failure";
  result.replayFailure = await verifyReplayFailure(page, owner, turnId, accepted.message_id, accepted.job_id);
  result.phase = "parallel-threads";
  const parallel = await verifyParallelThreads(page, owner, accepted.job_id);
  result.parallel = parallel;
  result.phase = "native-file-editor";
  const file = await verifyNativeFileEditor(page, owner);
  result.phase = "deletion-refresh";
  result.deletionRefresh = await verifyDeletionRefresh(page, owner, tabs);
  result = {
    ...result,
    phase: "complete",
    workspaceId,
    sessionId,
    threadId,
    turnId,
    modelCallIds,
    composerSendRequest: true,
    sentMessage: sentPayload.message.content,
    traceSseConnected: traceStreams.some((entry) => entry.session_id === sessionId),
    messageSseConnected: streamResponses.some((url) => url.includes("/message-stream?")),
    completedToolVisible,
    completedToolText,
    finalTextVisible: true,
    pageErrors,
    noPageErrors: pageErrors.length === 0,
    workbenchLoaded: await page.locator("#workbench .monaco-workbench").count() === 1,
    tabs,
    parallel,
    file,
  };
  await writeFile(resultPath, JSON.stringify(result, null, 2));
} catch (error) {
  executionError = error;
  const diagnostics = await Promise.allSettled([
    writeFile(resultPath, JSON.stringify({ ...result, error: String(error),
      pageUrl: page?.url(), pageErrors, streamResponses, traceStreams }, null, 2)),
    ...(page ? [Promise.resolve().then(() => page.screenshot({ path: screenshotPath, fullPage: true }))] : []),
  ]);
  const failures = diagnostics.filter(entry => entry.status === "rejected").map(entry => entry.reason);
  if (failures.length) executionError = new AggregateError([error, ...failures], "聊天执行与诊断保存均失败");
  throw executionError;
} finally {
  try {
    await runContext.close();
  } catch (cleanupError) {
    if (executionError) throw new AggregateError([executionError, cleanupError], "聊天执行与清理均失败");
    throw cleanupError;
  }
}
