import { writeFile } from "node:fs/promises";
import process from "node:process";
import { chromium } from "playwright";

function requiredEnvironment(name) {
  const value = process.env[name];
  if (!value) throw new Error(`缺少环境变量 ${name}`);
  return value;
}

const baseUrl = requiredEnvironment("BOXTEAM_BROWSER_BASE_URL");
const workspaceId = requiredEnvironment("BOXTEAM_BROWSER_WORKSPACE_ID");
const sessionId = requiredEnvironment("BOXTEAM_BROWSER_SESSION_ID");
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
  await page.goto(baseUrl, { waitUntil: "domcontentloaded", timeout: 30_000 });
  const current = await page.evaluate(async () => {
    const response = await fetch("/api/gateway/users/current");
    return { status: response.status, body: await response.text() };
  });
  if (current.status === 401) {
    await api(page, "/api/gateway/users/guest", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ tracking: { source: "rollout-history-browser" } }),
    });
    await page.reload({ waitUntil: "domcontentloaded" });
  } else if (current.status !== 200) {
    throw new Error(`检查游客访问失败: ${current.status} ${current.body}`);
  }
}

function historyPayload(response) {
  const request = response.request();
  if (request.method() !== "POST") return null;
  try {
    const value = JSON.parse(request.postData() || "{}");
    return value.direction === "before" ? value : null;
  } catch {
    return null;
  }
}

async function dispatchTopWheel(page, stream, deltaY) {
  await stream.hover();
  await stream.evaluate((element, wheelDelta) => {
    const event = new WheelEvent("wheel", {
      bubbles: true,
      deltaY: wheelDelta,
    });
    element.dispatchEvent(event);
    element.scrollTop = 0;
    element.dispatchEvent(new WheelEvent("wheel", {
      bubbles: true,
      deltaY: wheelDelta,
    }));
  }, deltaY);
  await page.waitForFunction(
    () => document.querySelector(".chat-stream")?.scrollTop <= 2,
    { timeout: 30_000, polling: "raf" },
  );
}

async function loadNextOlder(page, stream, expectedFirstOrdinal) {
  const started = performance.now();
  const responsePromise = page.waitForResponse(
    (response) => {
      return (
        response.url().includes("/history") &&
        response.status() === 200 &&
        historyPayload(response) !== null
      );
    },
    { timeout: 30_000 },
  );
  await dispatchTopWheel(page, stream, -160);
  const triggerStarted = performance.now();
  const response = await responsePromise;
  const responseReceivedMs = performance.now() - triggerStarted;
  const payload = await response.json();
  // prepend 后必须继续保持当前可见锚点，新增批次通常在视口上方，
  // 因此不能用“DOM 数量增加”判断。用列表外壳上的状态镜像确认
  // React 已经合并批次，列表外壳本身也已经重新渲染。
  await page.waitForFunction(
    ({ expectedFirstOrdinal, expectedCount }) => {
      const shell = document.querySelector(".chat-stream-virtual-shell");
      return (
        shell?.getAttribute("data-first-turn-id") ===
          `job-${String(expectedFirstOrdinal).padStart(4, "0")}` &&
        Number(shell?.getAttribute("data-turn-count")) >= expectedCount
      );
    },
    {
      expectedFirstOrdinal,
      expectedCount: Number(expectedFirstOrdinal) === 121
        ? 8
        : Number(expectedFirstOrdinal) === 118
          ? 11
          : 14,
    },
    { timeout: 30_000, polling: "raf" },
  );
  const renderedMs = performance.now() - triggerStarted;
  return {
    request: historyPayload(response),
    ordinals: payload.data.items.map((item) => item.ordinal),
    nextCursor: payload.data.next_cursor,
    responseReceivedMs,
    renderedMs,
    elapsedMs: performance.now() - started,
  };
}

const browser = await chromium.launch({ executablePath, headless: true });
const context = await browser.newContext({
  viewport: { width: 1920, height: 1100 },
});
const page = await context.newPage();
const pageErrors = [];
page.on("pageerror", (error) => pageErrors.push(String(error)));
let result;

try {
  await ensureGuest(page);
  const sessionButton = page.locator(`button[data-session-id="${sessionId}"]`);
  await sessionButton.waitFor({ state: "visible", timeout: 30_000 });
  await sessionButton.click();
  await page
    .locator('[data-turn-id="job-0128"]')
    .waitFor({ state: "visible", timeout: 30_000 });
  const latestTurn = page.locator('[data-turn-id="job-0128"]').last();
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

  const defaultPage = await api(page, `/api/v1/sessions/${sessionId}/history`, {
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

  const detailPage = await api(page, `/api/v1/sessions/${sessionId}/history`, {
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
    `/api/v1/sessions/${sessionId}/history`,
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
    `/api/v1/sessions/${sessionId}/history`,
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
    `/api/v1/sessions/${sessionId}/history`,
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
    `/api/v1/sessions/${sessionId}/history`,
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
    `/api/v1/sessions/${sessionId}/history`,
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

  await latestTurn
    .locator('button.chat-thinking-toggle[aria-expanded="false"]')
    .click();

  const toolDetailsResponsePromise = page.waitForResponse(
    (response) => {
      if (!response.url().includes("/history") || response.status() !== 200)
        return false;
      try {
        const body = JSON.parse(response.request().postData() || "{}");
        return (
          Array.isArray(body.turn_ids) &&
          body.turn_ids.includes("job-0128") &&
          body.include?.includes("tool_result") &&
          body.tool_call_ids?.includes("call_chat_reasoning_tool")
        );
      } catch {
        return false;
      }
    },
    { timeout: 30_000 },
  );
  await latestTurn.locator(".chat-tool-summary").first().click();
  const toolDetailsResponse = await toolDetailsResponsePromise;
  const toolDetailsPayload = await toolDetailsResponse.json();
  const toolDetailsBody = JSON.stringify(toolDetailsPayload);
  if (
    !toolDetailsBody.includes("turn-0128") ||
    !toolDetailsBody.includes("LARGE_RESULT turn-0128_BEGIN")
  ) {
    throw new Error(`当前 Turn 工具详情响应不完整: ${toolDetailsBody}`);
  }

  // 会话进入稳定展示态后，先完成一次同链路热身；性能指标不把首次
  // Gateway/SQLite 连接建立成本混入滚动加载热路径。
  for (let index = 0; index < 2; index += 1) {
    await api(page, `/api/v1/sessions/${sessionId}/history`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-BoxTeam-Workspace-Id": workspaceId,
      },
      body: JSON.stringify({ direction: "tail" }),
    });
  }

  const stream = page.locator(".chat-stream");
  const progressivePages = [];
  for (const expected of [121, 118, 115]) {
    const pageResult = await loadNextOlder(page, stream, expected);
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
    (pageResult) => pageResult.renderedMs - pageResult.responseReceivedMs <= 200,
  );
  if (!progressiveLoadsWithinBudget) {
    throw new Error(
      `before Turn 前端合并渲染超过 200ms: ${JSON.stringify(progressivePages)}`,
    );
  }

  const samples = [];
  for (let index = 0; index < 5; index += 1) {
    const started = performance.now();
    await api(page, `/api/v1/sessions/${sessionId}/history`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-BoxTeam-Workspace-Id": workspaceId,
      },
      body: JSON.stringify({ direction: "tail" }),
    });
    samples.push(performance.now() - started);
  }
  samples.sort((left, right) => left - right);
  const historyRequestP95Ms =
    samples[Math.min(samples.length - 1, Math.floor(samples.length * 0.95))];
  const canonical = await api(page, `/api/v1/sessions/${sessionId}/history`, {
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

  await stream.evaluate((element) => {
    element.scrollTop = element.scrollHeight;
    element.dispatchEvent(new Event("scroll", { bubbles: true }));
  });
  await latestTurn.waitFor({ state: "visible", timeout: 30_000 });
  await latestTurn.hover();
  const responseActions = latestTurn.locator(".chat-response-actions");
  await responseActions.waitFor({ state: "attached", timeout: 30_000 });
  const responseActionsVisible = await waitUntil(
    async () => responseActions.evaluate((element) => {
      const style = getComputedStyle(element);
      return style.visibility === "visible"
        && Number.parseFloat(style.opacity) > 0
        && style.pointerEvents === "auto";
    }),
    "历史回复操作栏悬停显示",
  ).then(() => true);
  const responseActionLabels = await responseActions.locator("button").evaluateAll(
    (buttons) => buttons
      .map((button) => button.getAttribute("aria-label"))
      .filter((label) => Boolean(label)),
  );
  for (const label of ["复制", "有帮助（暂未开放）", "没有帮助（暂未开放）"]) {
    if (!responseActionLabels.includes(label)) {
      throw new Error(`历史回复操作栏缺少 ${label}: ${JSON.stringify(responseActionLabels)}`);
    }
  }
  if (responseActionLabels.includes("重新生成最后回复")) {
    throw new Error(`回复操作栏不应再显示重新生成: ${JSON.stringify(responseActionLabels)}`);
  }
  const boundaryTurn = page.locator('[data-turn-id="job-0127"]').last();
  await boundaryTurn.waitFor({ state: "visible", timeout: 30_000 });
  await boundaryTurn.hover();
  const boundaryResponseActions = boundaryTurn.locator(".chat-response-actions");
  await boundaryResponseActions.waitFor({ state: "attached", timeout: 30_000 });
  const boundaryResponseActionsVisible = await waitUntil(
    async () => boundaryResponseActions.evaluate((element) => {
      const style = getComputedStyle(element);
      return style.visibility === "visible"
        && Number.parseFloat(style.opacity) > 0
        && style.pointerEvents === "auto";
    }),
    "无正文边界 Turn 操作栏悬停显示",
  ).then(() => true);
  const boundaryResponseActionLabels = await boundaryResponseActions.locator("button").evaluateAll(
    (buttons) => buttons
      .map((button) => button.getAttribute("aria-label"))
      .filter((label) => Boolean(label)),
  );
  for (const label of ["复制（暂无可复制内容）", "有帮助（暂未开放）", "没有帮助（暂未开放）"]) {
    if (!boundaryResponseActionLabels.includes(label)) {
      throw new Error(`无正文边界 Turn 操作栏缺少 ${label}: ${JSON.stringify(boundaryResponseActionLabels)}`);
    }
  }

  result = {
    defaultProjectionSafe,
    detailProjectionSafe,
    canonicalMixedMessageRestored,
    responseActionsVisible,
    responseActionLabels,
    boundaryResponseActionsVisible,
    boundaryResponseActionLabels,
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
    })),
    progressiveLoadsWithinBudget,
    historyRequestP95Ms,
    browserPrependMs: progressivePages[0].elapsedMs,
    browserHistoryResponseMs: progressivePages[0].responseReceivedMs,
    browserTimelineRenderedMs: progressivePages[0].renderedMs,
    noPageErrors: pageErrors.length === 0,
    pageErrors,
  };
  await writeFile(resultPath, JSON.stringify(result, null, 2));
} catch (error) {
  await page
    .screenshot({ path: screenshotPath, fullPage: true })
    .catch(() => undefined);
  throw error;
} finally {
  await context.close();
  await browser.close();
}
