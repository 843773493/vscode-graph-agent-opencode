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

async function api(page, pathname, init = {}, preparedToken) {
  const token = preparedToken ?? await localToken(page);
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

// 在浏览器内从真实点击计时，直到正文、busy 和滚动几何一并稳定；不把 loading 当终点。
async function measureDisclosure(button) {
  return button.evaluate(async element => {
    const expected = element.getAttribute('aria-expanded') !== 'true';
    const scroller = element.closest('[data-virtuoso-scroller]');
    const started = performance.now();
    element.click();
    return new Promise((resolve,reject) => {
      let previous, stable = 0;
      const observe = () => {
        const body = element.parentElement.querySelector(':scope > .chat-thinking-body, :scope > .chat-tool-details');
        const ready = element.getAttribute('aria-expanded') === String(expected)
          && element.getAttribute('aria-busy') !== 'true' && (!expected || body?.getBoundingClientRect().height > 0);
        const snapshot = JSON.stringify([scroller?.scrollTop,scroller?.scrollHeight,element.getBoundingClientRect().top,body?.getBoundingClientRect().height]);
        stable = ready && previous === snapshot ? stable+1 : 0; previous = snapshot;
        if (stable >= 2) resolve({elapsedMs:performance.now()-started,open:expected});
        else if (performance.now()-started > 3000) reject(new Error('开关未在3秒内完成正文和布局'));
        else requestAnimationFrame(observe);
      };
      requestAnimationFrame(observe);
    });
  });
}

async function storedView(page, viewOwner=owner) {
  return page.evaluate(async owner => {
    const key=JSON.stringify([owner.gatewayId,'session',JSON.stringify([owner.gatewayId,owner.workspaceId]),owner.sessionId,owner.threadId]);
    return new Promise((resolve,reject)=>{
      const opening=indexedDB.open('boxteam-browser-views');
      opening.onerror=()=>reject(opening.error);
      opening.onsuccess=()=>{
        const database=opening.result,transaction=database.transaction('views','readonly');
        const request=transaction.objectStore('views').get(key);
        transaction.oncomplete=()=>{database.close();resolve(request.result??null);};
        transaction.onabort=()=>{database.close();reject(transaction.error);};
      };
    });
  },viewOwner);
}

async function readingPoint(scroller, anchor) {
  return scroller.evaluate((element,anchor)=>{
    const node=[...element.querySelectorAll('[data-transcript-node]')].find(node=>node.dataset.transcriptNode===JSON.stringify(anchor.node_key));
    if(!node)return null;
    const selector='p, pre, li, blockquote, h1, h2, h3, h4, table, img';
    const blocks=[...node.querySelectorAll(selector)].filter(block=>block.closest('[data-transcript-node]')===node
      && !block.parentElement?.closest(selector)?.closest('[data-transcript-node]')?.isSameNode(node));
    const block=(blocks[anchor.content_position.block]??node).getBoundingClientRect();
    const bounds=node.getBoundingClientRect(),view=element.getBoundingClientRect();
    return {visible:bounds.bottom>view.top&&bounds.top<view.bottom,
      offset:block.top+Math.min(anchor.content_position.offset_px,block.height)-view.top};
  },anchor);
}

async function loadHistoryPage(page, stream, expectedFirstOrdinal, direction = "before") {
  // history 的分页和详情共用 URL；按响应 request_id 绑定网络结束时间。
  const network = await page.context().newCDPSession(page);
  const timings = new Map();
  network.on('Network.requestWillBeSent', event => timings.set(event.requestId, {
    epochOffset: event.wallTime * 1000 - event.timestamp * 1000,
  }));
  network.on('Network.responseReceived', event => {
    const timing = timings.get(event.requestId);
    if (timing) timing.requestId = Object.entries(event.response.headers)
      .find(([name]) => name.toLowerCase() === 'x-request-id')?.[1];
  });
  network.on('Network.loadingFinished', event => {
    const timing = timings.get(event.requestId);
    if (timing) timing.endedAt = timing.epochOffset + event.timestamp * 1000;
  });
  await network.send('Network.enable');
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
      const visible = [...element.querySelectorAll('[data-transcript-node]')].filter(node => {
        const rect = node.getBoundingClientRect();
        return rect.bottom > viewport.top && rect.top < viewport.bottom;
      });
      const anchor = visible.filter(node => node.getBoundingClientRect().top <= viewport.top + 1).at(-1) ?? visible[0];
      if (!anchor) throw new Error('分页前缺少可见阅读节点');
      const anchorId = anchor.dataset.transcriptNode;
      const selector = 'p, pre, li, blockquote, h1, h2, h3, h4, table, img';
      const blocks = node => [...node.querySelectorAll(selector)].filter(block => block.closest('[data-transcript-node]') === node
        && !block.parentElement?.closest(selector)?.closest('[data-transcript-node]')?.isSameNode(node));
      const blockIndex = Math.max(0, blocks(anchor).findIndex(block => block.getBoundingClientRect().bottom > viewport.top));
      const originalBlock = (blocks(anchor)[blockIndex] ?? anchor).getBoundingClientRect();
      const contentOffset = Math.min(Math.max(0, viewport.top - originalBlock.top), originalBlock.height);
      const top = originalBlock.top + contentOffset;
      const startedAt = performance.timeOrigin + performance.now();
      window.__historyPageMeasure = new Promise(resolve => {
        let stableFrames = 0;
        const frames = [];
        const observe = () => {
          const now = performance.timeOrigin + performance.now();
          const current = [...element.querySelectorAll('[data-transcript-node]')].find(node => node.dataset.transcriptNode === anchorId);
          const block = current && (blocks(current)[blockIndex] ?? current).getBoundingClientRect();
          const offsetDelta = block ? block.top + Math.min(contentOffset, block.height) - top : null;
          const merged = direction === "before" ? shell.dataset.firstTurnId === expectedId
            : Number(shell.dataset.turnCount) === previousCount + 3;
          if (frames.length < 20) frames.push({ elapsedMs: now - startedAt, scrollTop: element.scrollTop,
            scrollHeight: element.scrollHeight, offsetDelta, merged });
          // 三层默认折叠后可能不足一屏。不可达时核合法边界，不能用空白垫高假保位。
          const clamped = offsetDelta !== null && Math.abs(offsetDelta) > 2 &&
            (offsetDelta > 0 ? element.scrollHeight - element.clientHeight - element.scrollTop <= 2 : element.scrollTop <= 2);
          stableFrames = merged && offsetDelta !== null && (Math.abs(offsetDelta) <= 2 || clamped) ? stableFrames + 1 : 0;
          if (stableFrames >= 2 || now - startedAt >= 3000) {
            resolve({ startedAt, visibleAt: now, anchorId, offsetDelta, restored: stableFrames >= 2 && !clamped,
              clamped: stableFrames >= 2 && clamped,
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
    // CDP 的同一网络事件时间轴不受 route 屏障或并发详情响应次序影响。
    await expect.poll(() => [...timings.values()].find(timing => timing.requestId === requestId)?.endedAt).toBeDefined();
    const responseEndedAt = [...timings.values()].find(timing => timing.requestId === requestId).endedAt;
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
    await network.detach();
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
  await page.addInitScript(() => {
    const tasks = [];
    const observer = new PerformanceObserver(list => {
      for (const entry of list.getEntries()) if (tasks.length < 200) tasks.push({start:entry.startTime,duration:entry.duration});
    });
    observer.observe({type:'longtask',buffered:true});
    globalThis.__rolloutReadTiming = () => {
      observer.disconnect();
      return {timeOrigin:performance.timeOrigin,now:performance.now(),longTasks:tasks,
        resources:performance.getEntriesByType('resource').map(entry=>({path:new URL(entry.name).pathname,start:entry.startTime,duration:entry.duration}))};
    };
  });
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
  // 新浏览器先完成原生欢迎编辑器的启动，避免它稍后激活时取消树中的打开意图。
  // 首次历史读取计时仍从用户点击开始，不把工作台启动算进历史查询。
  await expect(page.locator('.gettingStartedContainer:visible')).toBeVisible({timeout:30_000});
  const sessionButton = page.locator(`.native-session-navigation button[data-session-id="${sessionId}"]`);
  await expect(sessionButton).toBeVisible({ timeout: 30_000 });
  const firstReadStarted = performance.now();
  await sessionButton.click();
  const thread = page.locator(`section.boxteam-thread[data-workspace-id="${owner.workspaceId}"][data-session-id="${sessionId}"][data-thread-id="${owner.threadId}"]`);
  await expect(thread).toBeVisible({timeout:30_000});
  await page.evaluate(() => globalThis.workbench.commands.executeCommand('workbench.action.keepEditor'));
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
  result.firstReadMs = performance.now()-firstReadStarted;
  result.firstReadTiming = await page.evaluate(()=>globalThis.__rolloutReadTiming());
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

  const outlineResponsePromise = page.waitForResponse(response => {
    if (new URL(response.url()).pathname !== historyPath || response.status() !== 200) return false;
    const body = response.request().postDataJSON();
    return body?.turn_ids?.[0] === "job-0128" && body.include?.includes("activity_outline");
  });
  await latestTurn.locator(':scope > .chat-assistant-row > .chat-turn-activity > button').click();
  const outlineResponse = await outlineResponsePromise;
  const outlinePayload = await outlineResponse.json();
  const outlineParts = outlinePayload.data.items[0].response_parts;
  if (outlineParts.some(part => ["reasoning", "tool_call", "tool_result"].includes(part.kind) && (part.text || part.arguments || part.result))) {
    throw new Error("活动轮廓泄漏了大正文");
  }
  await latestTurn.locator('.chat-activity-group > button').first().click();
  const toolDetailsResponsePromise = page.waitForResponse(response => {
    if (new URL(response.url()).pathname !== historyPath || response.status() !== 200) return false;
    const body = response.request().postDataJSON();
    return body?.turn_ids?.[0] === "job-0128" && body.item_ids?.length > 0 && body.item_ids.length <= 4
      && body.source_view_id === outlinePayload.data.source_view_id
      && body.source_view_revision === outlinePayload.data.source_view_revision
      && body.include?.includes("tool_call") && body.include?.includes("tool_result");
  });
  await latestTurn.locator(".chat-tool-summary").first().click();
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
    throw new Error(`展开活动响应没有逐字保留当前工具参数和结果: ${JSON.stringify({call: expandedCall?.arguments?.slice(0,160), expectedCall: detailToolCall.arguments?.slice(0,160), resultLength: expandedResult?.result?.length, expectedResultLength: detailToolResult.result?.length})}`);
  }
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
    (pageResult) => (pageResult.layout.restored || pageResult.layout.clamped) && pageResult.renderAndAnchorMs <= 200,
  );
  if (!progressiveLoadsWithinBudget) {
    throw new Error(
      `before Turn 前端合并渲染超过 200ms: ${JSON.stringify(progressivePages)}`,
    );
  }

  const samples = [];
  const historyRequestReceipts = [];
  // 产品 getGatewayToken 缓存已取得的凭据；暖 history 不应每次额外测量一次取凭据请求。
  // 单独记录准备耗时；history 仍计入完整 Gateway 请求、正文读取和 JSON 解析。
  const credentialStarted = performance.now();
  const historyToken = await localToken(page);
  const historyCredentialPreparationMs = performance.now() - credentialStarted;
  for (let index = 0; index < 20; index += 1) {
    const responsePromise = page.waitForResponse(response =>
      new URL(response.url()).pathname === historyPath && historyPayload(response, "tail") !== null);
    const started = performance.now();
    const sample = await api(page, historyPath, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-BoxTeam-Workspace-Id": workspaceId,
      },
      body: JSON.stringify({ direction: "tail" }),
    }, historyToken);
    samples.push(performance.now() - started);
    const response = await responsePromise;
    const requestId = response.headers()["x-request-id"];
    expect(requestId).toBe(sample.request_id);
    historyRequestReceipts.push({ elapsedMs: samples.at(-1), requestId,
      bytes: (await response.body()).byteLength, status: response.status() });
  }
  result.historyRequestReceipts = historyRequestReceipts;
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
  await expect(boundaryTurn.locator(".chat-turn-activity > .chat-thinking-toggle")).toBeVisible();
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
  await expect(thread.getByRole('status').filter({hasText:'已定位指定轮次'})).toBeVisible();
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
  await expect(thread.locator('.chat-turn[data-turn-id="job-0016"]')).toBeVisible();
  await expect.poll(() => pendingNavigation.size).toBe(0);
  await page.keyboard.press('Escape');
  await expect(quick).not.toBeVisible();
  await expect(latestTurn).toBeVisible();

  result.stage = 'disclosure-budgets';
  const cachedDisclosures = [];
  const latestHeader = latestTurn.locator('.chat-turn-activity > .chat-thinking-toggle');
  for (let index=0;index<20;index++) cachedDisclosures.push(await measureDisclosure(latestHeader));
  result.cachedDisclosures = cachedDisclosures;
  expect(cachedDisclosures.every(sample => sample.elapsedMs <= 200)).toBe(true);
  const networkDisclosures = [];
  result.networkDisclosures = networkDisclosures;
  for (let ordinal=40;ordinal<60;ordinal++) {
    const turnId = `job-${String(ordinal).padStart(4,'0')}`;
    await page.evaluate(query => globalThis.workbench.commands.executeCommand('workbench.action.quickOpen',query),`@继续处理第 ${ordinal} 轮`);
    const target = quick.getByRole('option',{name:new RegExp(`继续处理第 ${ordinal} 轮`)});
    // 大纲默认仅加载最近20轮；通过真实续页入口找到远处目标。
    for (let pageIndex=0;pageIndex<7 && await target.count()===0;pageIndex++) {
      const previous = await quick.innerText();
      await quick.getByRole('option',{name:/加载更早轮次/}).click();
      await expect.poll(()=>quick.innerText()).not.toBe(previous);
    }
    await expect(target).toBeVisible();
    // 续页保留原活动项；用真实键盘从续页行移到已核验的唯一目标再接受。
    if (!(await quick.locator('.monaco-list-row.focused').getAttribute('aria-label')).includes(`继续处理第 ${ordinal} 轮`))
      await quick.locator('input').first().press('ArrowUp');
    await expect(quick.locator('.monaco-list-row.focused')).toHaveAttribute('aria-label', new RegExp(`继续处理第 ${ordinal} 轮`));
    await quick.locator('input').first().press('Enter');
    const header = thread.locator(`.chat-turn[data-turn-id="${turnId}"] .chat-turn-activity > .chat-thinking-toggle`);
    await expect(header).toBeVisible();
    await expect(header).toHaveAttribute('aria-expanded','false');
    const responsePromise = page.waitForResponse(response => new URL(response.url()).pathname === historyPath
      && response.request().postDataJSON()?.turn_ids?.[0] === turnId
      && response.request().postDataJSON()?.include?.includes('activity_outline'));
    const sample = await measureDisclosure(header);
    const response = await responsePromise, payload = await response.json();
    expect(response.status()).toBe(200);
    expect(payload.request_id).toBe(response.headers()['x-request-id']);
    networkDisclosures.push({...sample,turnId,requestId:payload.request_id,bytes:new TextEncoder().encode(JSON.stringify(payload)).length});
    expect(sample.elapsedMs).toBeLessThanOrEqual(1500);
    await measureDisclosure(header);
  }
  expect(latencyDistribution(networkDisclosures.map(sample=>sample.elapsedMs)).p95Ms).toBeLessThanOrEqual(500);
  await thread.getByRole('button',{name:'跳到最新消息',exact:true}).click();
  await expect(latestTurn).toBeVisible();

  // 同一真实 Thread 的六个原版编辑器检验运行态隔离，不直接改 React 或存储。
  result.stage = 'split-editors';
  // 原版 split 命令不等待 copyEditor 的异步加载；逐组等真实正文和输入挂载后再拆。
  await page.locator('section.boxteam-thread:visible').last().locator('textarea').first().focus();
  await page.evaluate(() => globalThis.workbench.commands.executeCommand('workbench.action.splitEditorRight'));
  await expect(page.locator('section.boxteam-thread:visible')).toHaveCount(2);
  await expect(page.locator('section.boxteam-thread:visible').last().locator('textarea').first()).toBeEditable();
  await page.evaluate(async () => {
    await globalThis.workbench.commands.executeCommand('vscode.setEditorLayout', {
      orientation: 0, groups: [{groups:[{},{}]}, {groups:[{},{}]}, {groups:[{},{}]}],
    });
  });
  // 先建二维网格再打开余下四组，避免先创建六列触及原版最小宽度后的冻结尺寸规则。
  for (let index = 2; index < 6; index++) {
    await page.evaluate(({uri,index}) => globalThis.workbench.commands.executeCommand('_workbench.open',uri,[index,{pinned:true}]),
      {uri:`boxteam-thread://${owner.gatewayId}/${owner.workspaceId}/${owner.sessionId}/${owner.threadId}`,index});
  }
  const editors = page.locator('section.boxteam-thread:visible');
  result.editorLayout = await page.evaluate(() => globalThis.workbench.commands.executeCommand('vscode.getEditorLayout'));
  result.editorPositions = await page.locator('.editor-group-container').evaluateAll(groups => groups.map(group => ({
    width:group.clientWidth,height:group.clientHeight,style:group.getAttribute('style'),
    rect:group.getBoundingClientRect().toJSON(),html:group.outerHTML.slice(0,1600),
  })));
  await expect(editors).toHaveCount(6);
  for (const editor of await editors.all()) {
    await expect(editor.locator('.chat-turn[data-turn-id="job-0128"]')).toBeVisible();
    const composer = editor.locator('textarea').first();
    await expect(composer).toBeVisible();
    await expect(composer).toBeEditable();
  }
  const firstHeader = editors.nth(0).locator('.chat-turn[data-turn-id="job-0128"] .chat-turn-activity > .chat-thinking-toggle');
  const secondHeader = editors.nth(1).locator('.chat-turn[data-turn-id="job-0128"] .chat-turn-activity > .chat-thinking-toggle');
  const secondOpen = await secondHeader.getAttribute('aria-expanded');
  await firstHeader.click();
  await expect(secondHeader).toHaveAttribute('aria-expanded', secondOpen);
  const narrowLayout = await editors.locator('.chat-turn[data-turn-id="job-0128"] .chat-turn-activity > .chat-thinking-toggle').evaluateAll(headers => headers.map(header => {
    const arrow = header?.lastElementChild;
    const box = header?.getBoundingClientRect(), arrowBox = arrow?.getBoundingClientRect();
    return {width:header.clientWidth,overflow:header.scrollWidth-header.clientWidth,
      arrowGap:box && arrowBox ? box.right-arrowBox.right : null};
  }));
  result.narrowLayout = narrowLayout;
  expect(narrowLayout.every(entry => entry.overflow <= 1 && entry.arrowGap !== null && entry.arrowGap >= 0 && entry.arrowGap <= 20)).toBe(true);
  const sixEditorScreenshot = screenshotPath.replace('-failure.png', '-six-editors.png');
  await page.screenshot({path:sixEditorScreenshot,fullPage:true});
  const splitStateIndependent = true;

  result.stage = 'reading-restore-and-layout';
  const firstStream = editors.nth(0).locator('[data-virtuoso-scroller]');
  const otherStream = editors.nth(1).locator('[data-virtuoso-scroller]');
  const otherScroll = await otherStream.evaluate(element=>element.scrollTop);
  await firstStream.hover();await page.mouse.wheel(0,-250);
  await expect.poll(async()=>{
    const saved=await storedView(page);
    if(!saved?.reading_anchor||saved.follow_latest)return false;
    const point=await readingPoint(firstStream,saved.reading_anchor);
    return point?.visible&&Math.abs(point.offset-saved.reading_anchor.viewport_offset_px)<2;
  }).toBe(true);
  const saved=await storedView(page);
  expect(await otherStream.evaluate(element=>element.scrollTop)).toBe(otherScroll);
  result.savedReading=saved;
  // 只扰动真实视口的展示几何，不写业务投影或 IndexedDB。模拟新增卡片、图片增高、再移除。
  const pointBefore=await readingPoint(firstStream,saved.reading_anchor);
  await firstStream.evaluate((element,anchor)=>{
    const node=[...element.querySelectorAll('[data-transcript-node]')].find(node=>node.dataset.transcriptNode===JSON.stringify(anchor.node_key));
    const card=document.createElement('div');card.dataset.layoutProbe='card';card.textContent='布局探针：上方独立卡片';card.style.height='80px';
    node.before(card);
    const image=document.createElement('img');image.dataset.layoutProbe='image';image.src='data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw==';
    image.style.height='1px';image.style.width='100%';card.append(image);
  },saved.reading_anchor);
  await expect.poll(async()=>Math.abs((await readingPoint(firstStream,saved.reading_anchor))?.offset-pointBefore.offset)).toBeLessThan(2);
  await firstStream.locator('[data-layout-probe="card"]').evaluate(card=>{card.style.height='180px';card.querySelector('img').style.height='160px';});
  await expect.poll(async()=>Math.abs((await readingPoint(firstStream,saved.reading_anchor))?.offset-pointBefore.offset)).toBeLessThan(2);
  await firstStream.locator('[data-layout-probe="card"]').evaluate(card=>card.remove());
  await expect.poll(async()=>Math.abs((await readingPoint(firstStream,saved.reading_anchor))?.offset-pointBefore.offset)).toBeLessThan(2);
  await page.evaluate(()=>globalThis.boxteamWorkbenchLifecycle.prepareToClose());
  await page.reload({waitUntil:'domcontentloaded'});
  const restored=page.locator(`section.boxteam-thread[data-session-id="${sessionId}"]:visible`).first();
  await expect(restored.locator('textarea')).toBeEditable({timeout:30000});
  await expect.poll(async()=>{
    const point=await readingPoint(restored.locator('[data-virtuoso-scroller]'),saved.reading_anchor);
    result.restoreObservation={point,saved:await storedView(page),panes:await page.locator('section.boxteam-thread:visible [data-virtuoso-scroller]').evaluateAll(elements=>elements.map(element=>({
      scrollTop:element.scrollTop,height:element.clientHeight,scrollHeight:element.scrollHeight,
      nodes:[...element.querySelectorAll('[data-transcript-node]')].map(node=>({key:node.dataset.transcriptNode,top:node.getBoundingClientRect().top-element.getBoundingClientRect().top})),
    })))};
    return point?.visible&&Math.abs(point.offset-saved.reading_anchor.viewport_offset_px)<2;
  },{timeout:15000}).toBe(true);
  expect((await storedView(page)).reading_anchor).toEqual(saved.reading_anchor);
  result.readingRestored=true;

  result.stage='six-independent-sessions';
  const forkOwner={...owner,...JSON.parse(requiredEnvironment('BOXTEAM_BROWSER_FORK_OWNER'))};
  await page.evaluate(uri=>globalThis.workbench.commands.executeCommand('_workbench.open',uri,[0,{pinned:true}]),
    `boxteam-thread://${forkOwner.gatewayId}/${forkOwner.workspaceId}/${forkOwner.sessionId}/${forkOwner.threadId}`);
  const forkPane=page.locator(`section.boxteam-thread[data-session-id="${forkOwner.sessionId}"]:visible`);
  const sourceHeader=forkPane.locator('.chat-turn[data-turn-id="job-0128"] .chat-turn-activity > .chat-thinking-toggle');
  await measureDisclosure(sourceHeader);
  await expect.poll(async()=>Object.keys((await storedView(page,forkOwner))?.disclosures??{}).length).toBeGreaterThan(0);
  const source=(await api(page,`/api/v1/sessions/${forkOwner.sessionId}/fork-source-point`)).data;
  const independent=[];
  for(let index=0;index<6;index++) {
    const fork=(await api(page,`/api/v1/sessions/${forkOwner.sessionId}/fork-context`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({
      idempotency_key:`process-layout-${index}`,source_uri:source.source_uri,source_view_id:source.source_view_id,source_view_revision:source.source_view_revision,
      anchor_item_id:source.anchor_item_id,mode:'view_prefix_fork',creation_kind:'new_session_main',target_container_uri:source.main_container_uri,title:`独立会话 ${index+1}`})})).data;
    independent.push({sessionId:fork.session.session_id,threadId:fork.thread_id});
    await page.evaluate(({uri,index})=>globalThis.workbench.commands.executeCommand('_workbench.open',uri,[index,{pinned:true}]),
      {uri:`boxteam-thread://${owner.gatewayId}/${owner.workspaceId}/${fork.session.session_id}/${fork.thread_id}`,index});
    const pane=page.locator(`section.boxteam-thread[data-session-id="${fork.session.session_id}"]:visible`);
    await expect(pane.locator('textarea')).toBeEditable();
    await expect(pane.locator('.chat-turn[data-turn-id="job-0128"] .chat-turn-activity > .chat-thinking-toggle')).toHaveAttribute('aria-expanded','false');
  }
  expect(new Set(independent.map(item=>item.sessionId)).size).toBe(6);
  await expect(page.locator('section.boxteam-thread:visible')).toHaveCount(6);
  const independentScreenshot=screenshotPath.replace('-failure.png','-six-sessions.png');
  await page.screenshot({path:independentScreenshot,fullPage:true});
  const narrowPane=page.locator(`section.boxteam-thread[data-session-id="${independent[0].sessionId}"]:visible`);
  const beforeDrag=await narrowPane.boundingBox();
  const sashes=await page.locator('.part.editor .monaco-sash.vertical:visible').evaluateAll(elements=>elements.map(element=>{
    const box=element.getBoundingClientRect();return {x:box.x,y:box.y,width:box.width,height:box.height};
  }));
  const sash=sashes.filter(box=>box.y<=beforeDrag.y+beforeDrag.height/2&&box.y+box.height>=beforeDrag.y+beforeDrag.height/2)
    .sort((left,right)=>Math.abs(left.x-beforeDrag.x-beforeDrag.width)-Math.abs(right.x-beforeDrag.x-beforeDrag.width))[0];
  expect(Math.abs(sash.x-beforeDrag.x-beforeDrag.width)).toBeLessThan(10);
  await page.mouse.move(sash.x+sash.width/2,beforeDrag.y+beforeDrag.height/2);
  await page.mouse.down();
  await page.mouse.move(beforeDrag.x+180,beforeDrag.y+beforeDrag.height/2,{steps:15});
  await page.mouse.up();
  await expect.poll(async()=>(await narrowPane.boundingBox()).width).toBeLessThan(beforeDrag.width);
  const extremeLayout=await narrowPane.locator('.chat-turn-activity > button').evaluate(header=>{
    const summary=header.querySelector('.chat-activity-summary').getBoundingClientRect(),duration=header.querySelector('.chat-activity-duration').getBoundingClientRect();
    return {width:header.clientWidth,overflow:header.scrollWidth-header.clientWidth,
      arrowGap:header.getBoundingClientRect().right-header.lastElementChild.getBoundingClientRect().right,
      durationVisible:duration.top>=summary.top&&duration.bottom<=summary.bottom+1};
  });
  expect(extremeLayout.width).toBeLessThan(320);
  expect(extremeLayout.overflow).toBeLessThanOrEqual(1);
  expect(extremeLayout.arrowGap).toBeLessThanOrEqual(20);
  expect(extremeLayout.durationVisible).toBe(true);
  result.extremeLayout=extremeLayout;
  await expect(narrowPane.locator('textarea')).toBeEditable();
  await page.setViewportSize({width:1100,height:1000});
  for(const item of independent)await expect(page.locator(`section.boxteam-thread[data-session-id="${item.sessionId}"]:visible`).locator('textarea')).toBeEditable();
  await page.setViewportSize({width:1920,height:1100});
  result.independentSessions=independent;result.independentScreenshot=independentScreenshot;

  result.stage='child-and-internal';
  const child=(await api(page,`/api/v1/sessions/${forkOwner.sessionId}/fork-context`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({
    idempotency_key:'process-child',source_uri:source.source_uri,source_view_id:source.source_view_id,source_view_revision:source.source_view_revision,
    anchor_item_id:source.anchor_item_id,mode:'view_prefix_fork',creation_kind:'new_child_thread',target_container_uri:source.child_container_uri})})).data;
  await page.evaluate(uri=>globalThis.workbench.commands.executeCommand('_workbench.open',uri,[0,{pinned:true}]),
    `boxteam-thread://${owner.gatewayId}/${owner.workspaceId}/${forkOwner.sessionId}/${child.thread_id}`);
  const childPane=page.locator(`section.boxteam-thread[data-thread-id="${child.thread_id}"]:visible`);
  await expect(childPane.locator('textarea')).toBeEditable();
  const childHeader=childPane.locator('.chat-turn[data-turn-id="job-0128"] .chat-turn-activity > .chat-thinking-toggle');
  await expect(childHeader).toHaveAttribute('aria-expanded','false');
  await measureDisclosure(childHeader);
  await childPane.locator('.chat-turn[data-turn-id="job-0128"] .chat-activity-group > button').first().click();
  await expect(childPane.locator('.chat-turn[data-turn-id="job-0128"] .chat-tool-summary').first()).toBeVisible();
  const internalOwner=JSON.parse(requiredEnvironment('BOXTEAM_BROWSER_INTERNAL_OWNER'));
  await page.evaluate(uri=>globalThis.workbench.commands.executeCommand('_workbench.open',uri,[1,{pinned:true}]),
    `boxteam-thread://${owner.gatewayId}/${owner.workspaceId}/${internalOwner.sessionId}/${internalOwner.threadId}`);
  const internalPane=page.locator(`section.boxteam-thread[data-session-id="${internalOwner.sessionId}"]:visible`);
  await expect(internalPane.locator('textarea')).toBeEditable();
  await expect(internalPane.locator('[data-turn-id]')).toHaveCount(0);
  const internal=internalPane.locator('[data-execution-id="internal-browser-execution"]').last();
  const internalHeader=internal.locator('.chat-turn-activity > button');
  await expect(internalHeader).toHaveAttribute('aria-expanded','false');
  await measureDisclosure(internalHeader);
  await internal.locator('.chat-activity-group > button').first().click();
  await internal.locator('.chat-activity-item > button').first().click();
  await expect(internal).toContainText('内部执行思考正文');
  result.childAndInternal=true;

  // 真实 owner 定位后，用户收起表示结束这次定位；父层卸载重挂不能重新打开它。
  const internalPath = `/api/v1/sessions/${internalOwner.sessionId}/threads/${internalOwner.threadId}/history`;
  const internalHistory = (await api(page,internalPath,{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({direction:'tail'})})).data;
  const targetItem = internalHistory.internal_executions.flatMap(entry=>entry.response_parts).find(part=>part.kind==='reasoning').source.item_id;
  const targetLocation = (await api(page,internalPath,{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({direction:'around',anchor_item_id:targetItem})})).data.located_item;
  const locate = () => page.evaluate(({uri,location})=>globalThis.workbench.commands.executeCommand('_workbench.open',uri,
    [1,{pinned:true,boxteamMessage:location}]),{uri:`boxteam-thread://${owner.gatewayId}/${owner.workspaceId}/${internalOwner.sessionId}/${internalOwner.threadId}`,location:targetLocation});
  const targetHeader = internal.locator('.chat-activity-item > button').first();
  await locate();
  await expect(targetHeader).toHaveAttribute('aria-expanded','true');
  await targetHeader.click();
  await expect(targetHeader).toHaveAttribute('aria-expanded','false');
  await internalHeader.click();
  await expect(internalHeader).toHaveAttribute('aria-expanded','false');
  await internalHeader.click();
  await expect(targetHeader).toHaveAttribute('aria-expanded','false');
  await locate();
  await expect(targetHeader).toHaveAttribute('aria-expanded','true');
  const explicitLocationCancellation = true;

  result = {
    extremeLayout:result.extremeLayout,
    readingRestored:result.readingRestored,savedReading:saved,independentSessions:independent,independentScreenshot,childAndInternal:true,
    firstReadMs:result.firstReadMs,firstReadTiming:result.firstReadTiming,cachedDisclosures,networkDisclosures,
    disclosurePerformance:{cached:latencyDistribution(cachedDisclosures.map(sample=>sample.elapsedMs)),network:latencyDistribution(networkDisclosures.map(sample=>sample.elapsedMs))},
    owner, explicitLocationCancellation,
    narrowLayout, splitStateIndependent, sixEditorScreenshot,
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
    historyCredentialPreparationMs,
    historyRequestReceipts,
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
  expect(result.firstReadMs).toBeLessThanOrEqual(3000);
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
