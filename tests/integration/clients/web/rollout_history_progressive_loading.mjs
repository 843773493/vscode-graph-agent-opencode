import { writeFile } from "node:fs/promises";
import process from "node:process";
import { chromium } from "playwright";
import { expect } from "playwright/test";
import { TestRunContext } from "../../../harness/js/run-context.mjs";

function requiredEnvironment(name) {
  const value = process.env[name];
  if (!value) throw new Error(`缺少环境变量 ${name}`);
  return value;
}

const baseUrl = requiredEnvironment("BOXTEAM_BROWSER_BASE_URL");
const workspaceId = requiredEnvironment("BOXTEAM_BROWSER_WORKSPACE_ID");
const sessionId = requiredEnvironment("BOXTEAM_BROWSER_SESSION_ID");
const owner = JSON.parse(requiredEnvironment("BOXTEAM_BROWSER_OWNER"));
const historyPath = `/api/v1/sessions/${sessionId}/threads/${owner.threadId}/history`;
const scope = TestRunContext.fromTestFile("tests/integration/clients/web/test_rollout_history_progressive_loading.py");
const resultPath = requiredEnvironment("BOXTEAM_BROWSER_RESULT_PATH");
const screenshotPath = requiredEnvironment("BOXTEAM_BROWSER_SCREENSHOT_PATH");
const expectedFinalText = "工具调用完成";
const executablePath =
  process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH || undefined;

async function localToken(page) {
  return page.evaluate(async () => {
    const response = await fetch("/api/gateway/auth/local-credential");
    if (!response.ok)
      throw new Error(`获取本地凭据失败: HTTP ${response.status}`);
    const payload = await response.json();
    if (typeof payload?.data?.token !== "string")
      throw new Error("本地凭据缺少 token");
    return payload.data.token;
  });
}

async function api(page, pathname, init = {}) {
  const token = await localToken(page);
  return page.evaluate(
    async ({ pathname: path, init: requestInit, token: localTokenValue, workspaceId, gatewayId }) => {
      const headers = new Headers(requestInit.headers ?? {});
      headers.set("X-Local-Token", localTokenValue);
      if (path.startsWith("/api/v1/")) {
        headers.set("X-BoxTeam-Workspace-Id", workspaceId);
        headers.set("X-BoxTeam-Target-Gateway-Id", gatewayId);
      }
      const response = await fetch(path, {
        ...requestInit,
        headers,
        credentials: "include",
      });
      const body = await response.text();
      if (!response.ok) throw new Error(`API ${response.status}: ${body}`);
      return body ? JSON.parse(body) : null;
    },
    { pathname, init, token, workspaceId, gatewayId: owner.gatewayId },
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

async function openWorkbenchPage(page) {
  await page.goto(new URL("/", baseUrl).href, { waitUntil: "domcontentloaded", timeout: 30_000 });
}

function historyPayload(response, direction = "before") {
  const request = response.request();
  if (request.method() !== "POST") return null;
  try {
    const value = JSON.parse(request.postData() || "{}");
    return value.direction === direction ? value : null;
  } catch {
    return null;
  }
}

function latencyDistribution(samples) {
  const ordered = [...samples].sort((left, right) => left - right);
  return { count: ordered.length, p50Ms: ordered[Math.ceil(ordered.length * 0.5) - 1],
    p95Ms: ordered[Math.ceil(ordered.length * 0.95) - 1], maxMs: ordered.at(-1) };
}

async function loadHistoryPage(page, stream, expectedFirstOrdinal, direction = "before") {
  const resourceUrl = new URL(historyPath, baseUrl).href;
  const resourceCount = await page.evaluate(url => performance.getEntriesByName(url, 'resource').length, resourceUrl);
  // 仅延迟真实分页请求，保证取锚点前不会合并响应；不替换任何业务响应。
  let release;
  let requestStarted = false;
  const ready = new Promise(resolve => { release = resolve; });
  const route = async route => {
    if (route.request().method() === 'POST' && route.request().postDataJSON()?.direction === direction) {
      requestStarted = true;
      await ready;
    }
    await route.continue();
  };
  const pattern = `**${historyPath}`;
  await page.route(pattern, route);
  try {
    // 先滚到对应页边界；等待 Virtuoso 提交滚动布局后再取锚点。
    await stream.hover({ position: { x: 20, y: 20 } });
    for (let gesture = 0; gesture < 5 && !requestStarted; gesture += 1) {
      // wheel() 返回不代表合成器已把手势交给列表；先确认真实事件到达再等布局。
      await stream.evaluate(element => {
        window.__historyWheelDelivered = new Promise((resolve, reject) => {
          const listener = () => { clearTimeout(timer); resolve(); };
          const timer = setTimeout(() => { element.removeEventListener('wheel', listener); reject(new Error('滚轮事件未到达历史列表')); }, 3000);
          element.addEventListener('wheel', listener, { once: true, passive: true });
        });
      });
      await page.mouse.wheel(0, direction === "before" ? -1_000_000 : 1_000_000);
      await page.evaluate(async () => { await window.__historyWheelDelivered; delete window.__historyWheelDelivered; });
      await stream.evaluate(async element => {
        // 虚拟列表会分帧测量高度并补偿滚动；以真实位置稳定为准，不在中间帧采锚点。
        await new Promise((resolve, reject) => {
          let previous, stableFrames = 0;
          const deadline = performance.now() + 3000;
          const observe = () => {
            const snapshot = JSON.stringify([element.scrollTop, element.scrollHeight,
              ...[...element.querySelectorAll('[data-turn-id]')].map(turn =>
                [turn.dataset.turnId, turn.getBoundingClientRect().top, turn.getBoundingClientRect().height])]);
            stableFrames = snapshot === previous ? stableFrames + 1 : 0;
            previous = snapshot;
            if (stableFrames >= 4) resolve();
            else if (performance.now() >= deadline) reject(new Error('分页前列表布局未稳定'));
            else requestAnimationFrame(observe);
          };
          requestAnimationFrame(observe);
        });
      });
      const atEdge = await stream.evaluate((element, direction) => direction === 'before'
        ? element.scrollTop <= 2 : element.scrollHeight - element.scrollTop - element.clientHeight <= 2, direction);
      if (atEdge) break;
    }
    const responsePromise = page.waitForResponse(response =>
      new URL(response.url()).pathname === historyPath && response.status() === 200 && historyPayload(response, direction) !== null,
      { timeout: 30_000 });
    const expectedId = `job-${String(expectedFirstOrdinal).padStart(4, "0")}`;
    // 在页面内测量真实布局，避免把 Playwright 的轮询间隔计入渲染与锚点预算。
    const startedAt = await stream.evaluate((element, { expectedId, direction }) => {
      const shell = element.closest('.chat-stream-virtual-shell');
      const previousCount = Number(shell.dataset.turnCount);
      const viewport = element.getBoundingClientRect();
      const anchor = [...element.querySelectorAll('[data-turn-id]')].find(turn => {
        const rect = turn.getBoundingClientRect();
        return rect.bottom > viewport.top && rect.top < viewport.bottom;
      });
      if (!anchor) throw new Error('分页前缺少可见 Turn 锚点');
      const anchorId = anchor.dataset.turnId, top = anchor.getBoundingClientRect().top;
      const startedAt = performance.timeOrigin + performance.now();
      window.__historyPageMeasure = new Promise(resolve => {
        let stableFrames = 0;
        const frames = [];
        const observe = () => {
          const now = performance.timeOrigin + performance.now();
          const current = [...element.querySelectorAll('[data-turn-id]')].find(turn => turn.dataset.turnId === anchorId);
          const offsetDelta = current ? current.getBoundingClientRect().top - top : null;
          const merged = direction === "before" ? shell.dataset.firstTurnId === expectedId
            : Number(shell.dataset.turnCount) === previousCount + 3;
          if (frames.length < 20) frames.push({ elapsedMs: now - startedAt, scrollTop: element.scrollTop,
            scrollHeight: element.scrollHeight, offsetDelta, merged });
          stableFrames = merged && offsetDelta !== null && Math.abs(offsetDelta) <= 2 ? stableFrames + 1 : 0;
          if (stableFrames >= 2 || now - startedAt >= 3000) {
            resolve({ startedAt, visibleAt: now, anchorId, offsetDelta, restored: stableFrames >= 2,
              firstTurnId: shell.dataset.firstTurnId, turnCount: Number(shell.dataset.turnCount), frames });
          } else requestAnimationFrame(observe);
        };
        requestAnimationFrame(observe);
      });
      return startedAt;
    }, { expectedId, direction });
    if (!requestStarted) await page.mouse.wheel(0, direction === "before" ? -160 : 160);
    release();
    const response = await responsePromise;
    await response.finished();
    const payload = await response.json();
    const layout = await page.evaluate(async () => {
      const measurement = await window.__historyPageMeasure;
      delete window.__historyPageMeasure;
      return measurement;
    });
    const requestId = response.headers()['x-request-id'];
    if (!requestId || requestId !== payload.request_id) throw new Error('历史分页 request_id 缺失或不一致');
    // 渲染与响应结束使用浏览器同一时钟；CDP timing 在请求拦截后可能重复计入等待时间。
    await expect.poll(() => page.evaluate(url => performance.getEntriesByName(url, 'resource').length, resourceUrl)).toBe(resourceCount + 1);
    const responseEndedAt = await page.evaluate(url => {
      const timing = performance.getEntriesByName(url, 'resource').at(-1);
      return performance.timeOrigin + timing.responseEnd;
    }, resourceUrl);
    const responseReceivedMs = responseEndedAt - startedAt;
    const renderAndAnchorMs = layout.visibleAt - responseEndedAt;
    expect(renderAndAnchorMs).toBeGreaterThanOrEqual(0);
    return {
      request: historyPayload(response, direction), requestId,
      bytes: new TextEncoder().encode(JSON.stringify(payload)).length,
      ordinals: payload.data.items.map(item => item.ordinal), nextCursor: payload.data.next_cursor,
      responseReceivedMs, renderedMs: layout.visibleAt - startedAt,
      renderAndAnchorMs,
      elapsedMs: layout.visibleAt - startedAt, layout,
    };
  } finally {
    result.lastPaginationViewport = await stream.evaluate(element => ({
      scrollTop: element.scrollTop, scrollHeight: element.scrollHeight, clientHeight: element.clientHeight,
      turns: [...element.querySelectorAll('[data-turn-id]')].map(turn => ({
        id: turn.dataset.turnId, top: turn.getBoundingClientRect().top, height: turn.getBoundingClientRect().height,
      })),
    }));
    release();
    await page.unroute(pattern, route);
  }
}

const pageErrors = [];
let page;
let result = { stage: "bootstrap" };
let failure;
try {
  const browser = await chromium.launch({ executablePath, headless: true });
  scope.addCleanup("长历史 Chromium", () => browser.close());
  const context = await browser.newContext({ viewport: { width: 1920, height: 1100 } });
  scope.addCleanup("长历史 context", () => context.close());
  page = await context.newPage();
  page.on("pageerror", error => pageErrors.push(String(error)));
  const pendingNavigation = new Set();
  const navigationRequests = new Map();
  result.navigationRequests = [];
  page.on('request', request => {
    if (new URL(request.url()).pathname !== historyPath || request.method() !== 'POST') return;
    const body = request.postDataJSON();
    if (!['around', 'after'].includes(body.direction)) return;
    const receipt = { direction: body.direction, turnIds: body.turn_ids, anchor: body.anchor_turn_id, startedAt: Date.now() };
    navigationRequests.set(request, receipt);
    result.navigationRequests.push(receipt);
    if (body.direction === 'around' && !body.turn_ids) pendingNavigation.add(request);
  });
  page.on('requestfinished', request => { pendingNavigation.delete(request); });
  page.on('requestfailed', request => { pendingNavigation.delete(request); });
  page.on('response', response => {
    const receipt = navigationRequests.get(response.request());
    if (receipt) { receipt.respondedAt = Date.now(); receipt.status = response.status(); }
  });
  await openWorkbenchPage(page);
  const sessionButton = page.locator(`.native-session-navigation button[data-session-id="${sessionId}"]`);
  await expect(sessionButton).toBeVisible({ timeout: 30_000 });
  await sessionButton.dblclick();
  const thread = page.locator(`section.boxteam-thread[data-workspace-id="${owner.workspaceId}"][data-session-id="${sessionId}"][data-thread-id="${owner.threadId}"]`);
  await expect(thread).toBeVisible();
  await thread
    .locator('.chat-turn[data-turn-id="job-0128"]')
    .waitFor({ state: "visible", timeout: 30_000 });
  await expect(thread.locator(".chat-stream-virtual-shell")).toHaveAttribute("data-owner-thread-id", owner.threadId);
  await expect(thread.locator(".chat-stream-virtual-shell")).toHaveAttribute("data-owner-session-id", sessionId);
  const latestTurn = thread.locator('.chat-turn[data-turn-id="job-0128"]').last();
  await waitUntil(
    async () => (await latestTurn.innerText()).includes(expectedFinalText),
    "最新 Turn 最终响应",
  );
  const latestText = await latestTurn.innerText();
  if (
    !latestText.includes("耗时") ||
    !latestText.includes("Item ") ||
    latestText.includes("已完成思考")
  ) {
    throw new Error(`最新 Turn 没有显示活动统计折叠行: ${latestText}`);
  }

  const defaultPage = await api(page, historyPath, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-BoxTeam-Workspace-Id": workspaceId,
    },
    body: JSON.stringify({ direction: "tail" }),
  });
  const defaultItem = defaultPage.data.items.find(
    (item) => item.turn_id === "job-0128",
  );
  if (!defaultItem) throw new Error("默认历史页缺少 job-0128");
  const defaultJson = JSON.stringify(defaultItem);
  const defaultProjectionSafe =
    defaultItem.user_messages.length === 1 &&
    defaultItem.tool_summary.length === 2 &&
    defaultItem.items.every(
      (item) => Object.keys(item.raw ?? {}).length === 0,
    ) &&
    !defaultJson.includes("LARGE_CALL") &&
    !defaultJson.includes("LARGE_RESULT");

  const detailPage = await api(page, historyPath, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-BoxTeam-Workspace-Id": workspaceId,
    },
    body: JSON.stringify({
      turn_ids: ["job-0128"],
      include: [
        "user",
        "assistant_text",
        "thinking",
        "tool_call",
        "tool_result",
        "final_response",
      ],
    }),
  });
  const detailItem = detailPage.data.items.find(
    (item) => item.turn_id === "job-0128",
  );
  if (!detailItem) throw new Error("工具详情响应缺少 job-0128");
  const detailToolCall = detailItem.response_parts.find(
    (part) => part.kind === "tool_call",
  );
  const detailToolResult = detailItem.response_parts.find(
    (part) => part.kind === "tool_result",
  );
  const detailProjectionSafe =
    detailItem.thinking_blocks.some(
      (block) => block.kind === "reasoning" && block.text === "已读取 README，",
    ) &&
    detailItem.thinking_blocks.some(
      (block) => block.kind === "reasoning" && block.text === "整理最终答复。",
    ) &&
    detailItem.thinking_blocks.filter((block) => block.kind === "summary")
      .length === 4;
  const toolDetailsLoaded =
    detailToolCall?.arguments?.includes('"marker": "turn-0128"') === true &&
    detailToolResult?.result?.includes("LARGE_RESULT turn-0128_BEGIN") === true;

  const largeToolSummaryPage = await api(
    page,
    historyPath,
    {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-BoxTeam-Workspace-Id": workspaceId,
      },
      body: JSON.stringify({
        turn_ids: ["job-0121"],
        include: ["user", "thinking", "tool_summary", "final_response"],
      }),
    },
  );
  const largeToolSummaryItem = largeToolSummaryPage.data.items[0];
  const largeToolSummaryJson = JSON.stringify(largeToolSummaryItem);
  const largeToolSummarySafe =
    largeToolSummaryItem.tool_summary.length === 1 &&
    largeToolSummaryItem.tool_summary.every(
      (item) => item.tool_name === "invoke_extension_tool",
    ) &&
    largeToolSummaryItem.items.every(
      (item) => Object.keys(item.raw ?? {}).length === 0,
    ) &&
    !largeToolSummaryJson.includes("LARGE_CALL") &&
    !largeToolSummaryJson.includes("LARGE_RESULT");

  const largeToolDetailPage = await api(
    page,
    historyPath,
    {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-BoxTeam-Workspace-Id": workspaceId,
      },
      body: JSON.stringify({
        turn_ids: ["job-0121"],
        include: [
          "user",
          "assistant_text",
          "thinking",
          "tool_call",
          "tool_result",
          "final_response",
        ],
      }),
    },
  );
  const largeToolDetailItem = largeToolDetailPage.data.items[0];
  const largeToolDetailCall = largeToolDetailItem.response_parts.find(
    (part) => part.kind === "tool_call",
  );
  const largeToolDetailsBounded =
    largeToolDetailItem.detail_truncated === false &&
    largeToolDetailItem.items.length === 1 &&
    largeToolDetailItem.items.every(
      (item) => Object.keys(item.raw ?? {}).length === 0,
    ) &&
    largeToolDetailItem.tool_summary.length === 0 &&
    largeToolDetailCall?.arguments?.length === 65536;

  const aroundPage = await api(
    page,
    historyPath,
    {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-BoxTeam-Workspace-Id": workspaceId,
      },
      body: JSON.stringify({
        direction: "around",
        anchor_turn_id: "job-0064",
      }),
    },
  );
  const aroundOrdinals = aroundPage.data.items.map((item) => item.ordinal);
  const aroundCursorsPresent =
    typeof aroundPage.data.before_cursor === "string" &&
    typeof aroundPage.data.after_cursor === "string";
  if (
    JSON.stringify(aroundOrdinals) !== JSON.stringify(
      Array.from({ length: 7 }, (_, index) => index + 61),
    ) || !aroundCursorsPresent
  ) {
    throw new Error(`around(anchor) 窗口或双向游标错误: ${JSON.stringify(aroundPage.data)}`);
  }
  const beforeAroundPage = await api(
    page,
    historyPath,
    {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-BoxTeam-Workspace-Id": workspaceId,
      },
      body: JSON.stringify({
        direction: "before",
        cursor: aroundPage.data.before_cursor,
      }),
    },
  );
  const afterAroundPage = await api(
    page,
    historyPath,
    {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-BoxTeam-Workspace-Id": workspaceId,
      },
      body: JSON.stringify({
        direction: "after",
        cursor: aroundPage.data.after_cursor,
      }),
    },
  );
  const aroundBidirectionalSafe =
    JSON.stringify(beforeAroundPage.data.items.map((item) => item.ordinal)) ===
      JSON.stringify([58, 59, 60]) &&
    JSON.stringify(afterAroundPage.data.items.map((item) => item.ordinal)) ===
      JSON.stringify([68, 69, 70]);

  const toolDetailsResponsePromise = page.waitForResponse(
    (response) => {
      if (new URL(response.url()).pathname !== historyPath || response.status() !== 200)
        return false;
      try {
        const body = JSON.parse(response.request().postData() || "{}");
        return (
          Array.isArray(body.turn_ids) &&
          body.turn_ids.length === 1 && body.turn_ids[0] === "job-0128" &&
          body.include?.includes("reasoning_detail") &&
          body.include?.includes("tool_summary") &&
          body.include?.includes("tool_call") &&
          body.include?.includes("tool_result")
        );
      } catch {
        return false;
      }
    },
    { timeout: 30_000 },
  );
  await latestTurn.locator('button.chat-thinking-toggle[aria-expanded="false"]').click();
  const toolDetailsResponse = await toolDetailsResponsePromise;
  const toolDetailsPayload = await toolDetailsResponse.json();
  const toolDetailsBody = JSON.stringify(toolDetailsPayload);
  const expandedTurn = toolDetailsPayload.data.items.find((item) => item.turn_id === "job-0128");
  const expandedTools = expandedTurn.response_parts.filter((part) => part.kind === "tool_call" || part.kind === "tool_result");
  if (expandedTools.length !== 2 || expandedTools.some((part) => part.tool_call_id !== detailToolCall.tool_call_id || part.tool_name !== "invoke_extension_tool")) {
    throw new Error(`展开活动响应的工具身份不一致: ${JSON.stringify(expandedTools)}`);
  }
  const expandedCall = expandedTools.find((part) => part.kind === "tool_call");
  const expandedResult = expandedTools.find((part) => part.kind === "tool_result");
  if (expandedCall?.arguments !== detailToolCall.arguments || expandedResult?.result !== detailToolResult.result) {
    throw new Error("展开活动响应没有逐字保留当前工具参数和结果");
  }
  await latestTurn.locator(".chat-tool-summary").first().click();
  const displayedTool = latestTurn.locator(".chat-tool-details").first();
  await displayedTool.waitFor({ state: "visible", timeout: 30_000 });
  const displayedToolText = await displayedTool.textContent();
  if (!displayedToolText.includes("turn-0128") || !displayedToolText.includes("LARGE_RESULT turn-0128_BEGIN")) {
    throw new Error(`展开工具正文不完整: ${displayedToolText}`);
  }
  if (
    !toolDetailsBody.includes("turn-0128") ||
    !toolDetailsBody.includes("LARGE_RESULT turn-0128_BEGIN")
  ) {
    throw new Error(`当前 Turn 工具详情响应不完整: ${toolDetailsBody}`);
  }

  // 会话进入稳定展示态后，先完成一次同链路热身；性能指标不把首次
  // Gateway/SQLite 连接建立成本混入滚动加载热路径。
  for (let index = 0; index < 2; index += 1) {
    await api(page, historyPath, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-BoxTeam-Workspace-Id": workspaceId,
      },
      body: JSON.stringify({ direction: "tail" }),
    });
  }

  const stream = thread.locator(".chat-stream");
  await expect(stream).toBeVisible();
  const progressivePages = [];
  result.progressivePages = progressivePages;
  for (const expected of Array.from({ length: 20 }, (_, index) => 121 - index * 3)) {
    const pageResult = await loadHistoryPage(page, stream, expected);
    if (pageResult.ordinals[0] !== expected) {
      throw new Error(
        `渐进历史批次首 Turn 不符: expected=${expected}, actual=${pageResult.ordinals[0]}`,
      );
    }
    progressivePages.push(pageResult);
  }
  const progressiveOrdinals = progressivePages.map(
    (pageResult) => pageResult.ordinals,
  );
  const progressiveLoadsWithinBudget = progressivePages.every(
    (pageResult) => pageResult.layout.restored && pageResult.renderAndAnchorMs <= 200,
  );
  if (!progressiveLoadsWithinBudget) {
    throw new Error(
      `before Turn 前端合并渲染超过 200ms: ${JSON.stringify(progressivePages)}`,
    );
  }

  const samples = [];
  for (let index = 0; index < 20; index += 1) {
    const started = performance.now();
    await api(page, historyPath, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-BoxTeam-Workspace-Id": workspaceId,
      },
      body: JSON.stringify({ direction: "tail" }),
    });
    samples.push(performance.now() - started);
  }
  const orderedSamples = [...samples].sort((left, right) => left - right);
  const historyRequestP95Ms = orderedSamples[Math.ceil(orderedSamples.length * 0.95) - 1];
  const canonical = await api(page, historyPath, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-BoxTeam-Workspace-Id": workspaceId,
    },
    body: JSON.stringify({
      direction: "tail",
      include: ["user", "final_response"],
    }),
  });
  const canonicalItem = canonical.data.items.find(
    (item) => item.turn_id === "job-0128",
  );
  if (!canonicalItem) throw new Error("规范历史页缺少 job-0128");
  const canonicalMixedMessageRestored =
    canonicalItem.final_response === expectedFinalText;

  await thread.getByRole("button", { name: "跳到最新消息", exact: true }).click();
  await latestTurn.waitFor({ state: "visible", timeout: 30_000 });
  await expect(latestTurn.locator(".chat-response-actions")).toHaveCount(0);
  const responseCard = latestTurn.getByLabel("模型回复", { exact: true }).last();
  await responseCard.click({ button: "right" });
  const copyItem = page.getByRole("menuitem", { name: "复制此项", exact: true });
  await expect(copyItem).toBeVisible();
  await expect(copyItem).toBeEnabled();
  const responseCopyInContextMenu = true;
  await page.keyboard.press("Escape");
  const boundaryTurn = thread.locator('.chat-turn[data-turn-id="job-0127"]').last();
  await boundaryTurn.waitFor({ state: "visible", timeout: 30_000 });
  await expect(boundaryTurn.locator(".chat-response-actions")).toHaveCount(0);
  await expect(boundaryTurn.locator(".chat-turn-activity-header")).toBeVisible();
  const boundarySummaryWithoutToolbar = true;

  const quick = page.locator('.quick-input-widget');
  await page.evaluate(() => globalThis.workbench.commands.executeCommand('workbench.action.quickOpen', '@继续处理第 32 轮'));
  for (let count = 40; count <= 100; count += 20) {
    await quick.getByRole('option', { name: /加载更早轮次/ }).click();
    await expect(quick).toContainText(`已载入 ${count} 轮`);
  }
  await expect(quick.getByRole('option', { name: /继续处理第 32 轮/ })).toHaveCount(1);
  const aroundResponse = page.waitForResponse(response =>
    new URL(response.url()).pathname === historyPath && response.status() === 200
      && response.request().postDataJSON()?.direction === 'around');
  await quick.getByRole('option', { name: /继续处理第 32 轮/ }).click();
  await expect(quick).not.toBeVisible();
  const aroundPayload = await (await aroundResponse).json();
  expect(aroundPayload.data.items.map(turn => turn.ordinal)).toEqual([29, 30, 31, 32, 33, 34, 35]);
  await expect.poll(() => pendingNavigation.size).toBe(0);
  await expect(thread).toContainText('已定位第 32 轮');
  await expect(thread.locator('.chat-turn[data-turn-id="job-0032"]').last()).toBeVisible();
  const appendPages = [];
  result.appendPages = appendPages;
  for (let expected = 36; expected <= 93; expected += 3) {
    const sample = await loadHistoryPage(page, stream, expected, 'after');
    appendPages.push(sample);
    expect(sample.ordinals).toEqual([expected, expected + 1, expected + 2]);
    expect(sample.layout.restored).toBe(true);
    expect(sample.renderAndAnchorMs).toBeLessThanOrEqual(200);
  }

  const tailResponse = page.waitForResponse(response => new URL(response.url()).pathname === historyPath
    && response.status() === 200 && response.request().postDataJSON()?.direction === 'tail');
  await thread.getByRole('button', { name: '跳到最新消息', exact: true }).click();
  expect((await (await tailResponse).json()).data.items.at(-1).turn_id).toBe('job-0128');
  await expect(latestTurn).toBeVisible();

  // 真实大纲预览取消必须恢复原先的最新位置，不能停在预览窗口末尾。
  await page.evaluate(() => globalThis.workbench.commands.executeCommand('workbench.action.quickOpen', '@'));
  // 先过滤到续页动作，避免把虚拟列表尚未挂载的行当作缺失。
  await quick.locator('input').first().fill('@继续处理第 16 轮');
  for (let count = 40; count <= 140; count += 20) {
    await quick.getByRole('option', { name: /加载更早轮次/ }).click();
    await expect(quick).toContainText(`已载入 ${Math.min(count, 128)} 轮`);
  }
  await page.keyboard.press('ArrowDown');
  await expect(thread).toContainText('已定位第 16 轮');
  await expect.poll(() => pendingNavigation.size).toBe(0);
  await page.keyboard.press('Escape');
  await expect(quick).not.toBeVisible();
  await expect(latestTurn).toBeVisible();

  result = {
    owner,
    navigationRequests: result.navigationRequests,
    appendPages,
    paginationPerformance: {
      prepend: latencyDistribution(progressivePages.map(sample => sample.renderAndAnchorMs)),
      append: latencyDistribution(appendPages.map(sample => sample.renderAndAnchorMs)),
    },
    latestWindowRestored: true,
    outlinePreviewCancelled: true,
    defaultProjectionSafe,
    detailProjectionSafe,
    canonicalMixedMessageRestored,
    responseCopyInContextMenu,
    boundarySummaryWithoutToolbar,
    toolDetailsLoaded,
    largeToolSummarySafe,
    largeToolDetailsBounded,
    aroundOrdinals,
    aroundCursorsPresent,
    aroundBidirectionalSafe,
    beforeOrdinals: progressiveOrdinals,
    progressiveBatchMetrics: progressivePages.map((pageResult) => ({
      count: pageResult.ordinals.length,
      firstOrdinal: pageResult.ordinals[0],
      responseMs: pageResult.responseReceivedMs,
      renderedMs: pageResult.renderedMs,
      renderAndAnchorMs: pageResult.renderAndAnchorMs,
      requestId: pageResult.requestId, bytes: pageResult.bytes, layout: pageResult.layout,
    })),
    progressiveLoadsWithinBudget,
    historyRequestSamplesMs: samples,
    historyRequestP50Ms: orderedSamples[Math.ceil(orderedSamples.length * 0.5) - 1],
    historyRequestP95Ms,
    historyRequestMaxMs: orderedSamples.at(-1),
    browserPrependMs: progressivePages[0].elapsedMs,
    browserHistoryResponseMs: progressivePages[0].responseReceivedMs,
    browserTimelineRenderedMs: progressivePages[0].renderedMs,
    noPageErrors: pageErrors.length === 0,
    pageErrors,
  };
  await writeFile(resultPath, JSON.stringify(result, null, 2));
} catch (error) {
  failure = error;
} finally {
  const errors = failure ? [failure] : [];
  try {
    if (failure && page) await page.screenshot({ path: screenshotPath, fullPage: true });
    await writeFile(resultPath, JSON.stringify({ ...result, pageErrors, failure: failure?.stack }, null, 2) + "\n");
  } catch (error) { errors.push(error); }
  try { await scope.close(); } catch (error) { errors.push(error); }
  if (errors.length) throw new AggregateError(errors, "长历史浏览器验收失败");
}
