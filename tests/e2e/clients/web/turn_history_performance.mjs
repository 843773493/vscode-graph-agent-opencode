import { writeFile } from "node:fs/promises";
import process from "node:process";
import { chromium } from "playwright";

function requiredEnvironment(name) {
  const value = process.env[name];
  if (!value) throw new Error(`缺少环境变量 ${name}`);
  return value;
}

function deferred() {
  let resolve;
  const promise = new Promise((complete) => {
    resolve = complete;
  });
  return { promise, resolve };
}

function withTimeout(promise, timeoutMs, message) {
  return new Promise((resolve, reject) => {
    const timeout = setTimeout(() => reject(new Error(message)), timeoutMs);
    promise.then(
      (value) => {
        clearTimeout(timeout);
        resolve(value);
      },
      (error) => {
        clearTimeout(timeout);
        reject(error);
      },
    );
  });
}

async function visibleAnchor(stream) {
  return stream.evaluate((element) => {
    const viewportTop = element.getBoundingClientRect().top;
    return [...element.querySelectorAll("[data-conversation-id]")]
      .map((turn) => ({
        id: turn.getAttribute("data-conversation-id"),
        top: turn.getBoundingClientRect().top,
        bottom: turn.getBoundingClientRect().bottom,
      }))
      .find((turn) => turn.bottom > viewportTop + 1) ?? null;
  });
}

async function settledAnchorDelta(page, anchorId, anchorTop) {
  const stream = page.locator(".chat-stream");
  const startedAt = Date.now();
  const minimumObservationMs = 650;
  const deadline = Date.now() + 1_500;
  let previousTop = null;
  let stableSamples = 0;
  let latestDelta = null;
  const diagnostics = [];
  while (Date.now() < deadline) {
    const currentTop = await page.evaluate((targetId) => {
      const element = [...document.querySelectorAll("[data-conversation-id]")]
        .find((candidate) => candidate.getAttribute("data-conversation-id") === targetId);
      return element?.getBoundingClientRect().top ?? null;
    }, anchorId);
    if (diagnostics.length < 12) {
      diagnostics.push(await stream.evaluate((element, targetId) => ({
        elapsed_ms: Date.now(),
        scroll_top: element.scrollTop,
        scroll_height: element.scrollHeight,
        client_height: element.clientHeight,
        target_id: targetId,
        mounted_turns: [...element.querySelectorAll("[data-conversation-id]")]
          .map((turn) => ({
            id: turn.getAttribute("data-conversation-id"),
            top: turn.getBoundingClientRect().top,
            bottom: turn.getBoundingClientRect().bottom,
          })),
      }), anchorId));
    }
    if (currentTop === null) {
      stableSamples = 0;
    } else {
      latestDelta = Math.abs(currentTop - anchorTop);
      stableSamples = previousTop !== null && Math.abs(currentTop - previousTop) <= 0.5
        ? stableSamples + 1
        : 0;
      previousTop = currentTop;
      if (
        Date.now() - startedAt >= minimumObservationMs
        && stableSamples >= 4
      ) return { delta: latestDelta, diagnostics };
    }
    await page.waitForTimeout(25);
  }
  return { delta: latestDelta, diagnostics };
}

async function waitForJobTerminal(page, baseUrl, jobId) {
  const deadline = Date.now() + 30_000;
  while (Date.now() < deadline) {
    const response = await page.request.get(`${baseUrl}/api/v1/jobs/${jobId}`);
    if (!response.ok()) {
      throw new Error(`读取竞态 Job 失败: HTTP ${response.status()}`);
    }
    const payload = await response.json();
    const status = payload?.data?.status;
    if (["completed", "succeeded", "failed", "cancelled", "timed_out"].includes(status)) {
      return status;
    }
    await page.waitForTimeout(50);
  }
  throw new Error(`竞态 Job 在 30 秒内未进入终态: ${jobId}`);
}

const baseUrl = requiredEnvironment("BOXTEAM_E2E_BASE_URL");
const metricsPath = requiredEnvironment("BOXTEAM_E2E_METRICS_PATH");
const screenshotPath = requiredEnvironment("BOXTEAM_E2E_SCREENSHOT_PATH");
const localToken = requiredEnvironment("BOXTEAM_E2E_LOCAL_TOKEN");
const fixture = JSON.parse(requiredEnvironment("BOXTEAM_E2E_FIXTURE"));
const executablePath = process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH || undefined;
const targetSessionPath = `/api/v1/sessions/${fixture.target.sessionId}`;

const browser = await chromium.launch({ executablePath, headless: true });
const page = await browser.newPage({
  viewport: { width: 1440, height: 1000 },
  extraHTTPHeaders: { "X-Local-Token": localToken },
});
const bootstrapGate = deferred();
const bootstrapCaptured = deferred();
const partialBootstrapServed = deferred();
const readyBootstrapGate = deferred();
const detailGate = deferred();
const detailCaptured = deferred();
const network = [];
const networkReads = [];
const turnPages = [];
const detailBatches = [];
const fullTraceRequests = [];
const forbiddenHistoryFallbackRequests = [];
const targetTraceStreamRequests = [];
const targetTraceHistoryRequests = [];
const streamResponses = [];
const requestFailures = [];
const browserErrors = [];
let diagnosticTraceHistoryActive = false;
let targetBootstrapRequestCount = 0;
let initialTargetBootstrapBody = null;

page.on("request", (request) => {
  const url = new URL(request.url());
  if (url.pathname === `${targetSessionPath}/traces/stream`) {
    targetTraceStreamRequests.push(request.url());
  }
  if (
    request.method() === "GET"
    && url.pathname === `${targetSessionPath}/traces`
  ) {
    targetTraceHistoryRequests.push(request.url());
    if (!diagnosticTraceHistoryActive) {
      forbiddenHistoryFallbackRequests.push(request.url());
    }
  }
  if (
    url.pathname.endsWith("/traces")
    && url.pathname !== `${targetSessionPath}/traces`
    && !url.searchParams.has("cursor")
    && !url.searchParams.has("limit")
  ) {
    fullTraceRequests.push(request.url());
    forbiddenHistoryFallbackRequests.push(request.url());
  }
  if (
    request.method() === "GET"
    && (
      url.pathname.endsWith("/agent-state/messages")
      || url.pathname.endsWith("/messages")
    )
  ) {
    forbiddenHistoryFallbackRequests.push(request.url());
  }
  if (url.pathname.endsWith("/turns/details")) {
    const payload = request.postDataJSON();
    if (payload && Array.isArray(payload.turn_ids)) {
      detailBatches.push(payload.turn_ids);
    }
  }
});

page.on("response", (response) => {
  const url = new URL(response.url());
  if (url.pathname === `${targetSessionPath}/traces/stream`) {
    streamResponses.push({
      status: response.status(),
      url: response.url(),
    });
  }
  const tracked = url.pathname.endsWith("/bootstrap")
    || url.pathname.endsWith("/turns")
    || url.pathname.endsWith("/turns/details")
    || url.pathname.endsWith("/messages");
  if (!tracked) return;
  const entry = {
    url: response.url(),
    method: response.request().method(),
    status: response.status(),
    bytes: null,
  };
  network.push(entry);
  networkReads.push(response.body().then((body) => {
    entry.bytes = body.byteLength;
  }).catch((error) => {
    metrics.failures.push(
      `读取响应体计量失败 ${response.url()}: ${error instanceof Error ? error.message : String(error)}`,
    );
  }));
});

page.on("requestfailed", (request) => {
  requestFailures.push({
    url: request.url(),
    error: request.failure()?.errorText ?? "未知请求错误",
  });
});
page.on("pageerror", (error) => {
  browserErrors.push(error.stack ?? error.message);
});

await page.route(`**${targetSessionPath}/bootstrap`, async (route) => {
  targetBootstrapRequestCount += 1;
  const response = await route.fetch();
  const body = await response.body();
  if (targetBootstrapRequestCount === 1) {
    initialTargetBootstrapBody = body;
    bootstrapCaptured.resolve({ response, body });
    await bootstrapGate.promise;
    const payload = JSON.parse(body.toString("utf8"));
    payload.data = {
      ...payload.data,
      projection_state: "partial",
      event_cursor: null,
      older_cursor: null,
    };
    await route.fulfill({ response, body: JSON.stringify(payload) });
    partialBootstrapServed.resolve();
    return;
  }
  if (targetBootstrapRequestCount === 2) {
    await readyBootstrapGate.promise;
    await route.fulfill({
      response,
      body: initialTargetBootstrapBody ?? body,
    });
    return;
  }
  await route.fulfill({ response, body });
});

await page.route(`**${targetSessionPath}/turns/details`, async (route) => {
  const response = await route.fetch();
  const body = await response.body();
  detailCaptured.resolve({ response, body });
  await detailGate.promise;
  await route.fulfill({ response, body });
});

const metrics = {
  schema_version: 1,
  fixture: {
    turn_count: fixture.turnCount,
    trace_event_count: fixture.traceEventCount,
    history_pages_to_load: fixture.historyPagesToLoad,
    control_text_end_only: fixture.control.textEndOnly,
  },
  thresholds: fixture.thresholds,
  measurements: {},
  failures: [],
};

function requireMetric(condition, message) {
  if (!condition) metrics.failures.push(message);
}

async function markStage(stage) {
  metrics.stage = stage;
  await writeFile(metricsPath, `${JSON.stringify(metrics, null, 2)}\n`, "utf8");
}

try {
  await page.goto(baseUrl, { waitUntil: "domcontentloaded", timeout: 30_000 });
  await page.getByRole("heading", {
    name: fixture.control.latestFinalMarker,
    exact: true,
  }).waitFor({
    state: "visible",
    timeout: 10_000,
  });
  await markStage("control-session-ready");

  metrics.measurements.session_buttons = await page.locator("button").evaluateAll(
    (buttons) => buttons
      .filter((button) => button.textContent?.includes("Turn 性能"))
      .map((button) => ({
        text: button.textContent?.trim() ?? "",
        session_id: button.getAttribute("data-session-id"),
        class_name: button.className,
      })),
  );
  const sessionButton = page.locator(
    `[data-session-id="${fixture.target.sessionId}"]`,
  );
  const switchStartedAt = performance.now();
  await sessionButton.click();
  const capturedBootstrap = await withTimeout(
    bootstrapCaptured.promise,
    10_000,
    "没有捕获目标会话 bootstrap",
  );
  metrics.measurements.bootstrap_body_bytes = capturedBootstrap.body.byteLength;

  const composer = page.locator("textarea#input");
  await composer.waitFor({ state: "visible", timeout: 2_000 });
  await composer.focus();
  await composer.fill("bootstrap 延迟期间仍可输入");
  const composerReadyMs = performance.now() - switchStartedAt;
  metrics.measurements.composer_ready_ms = Math.round(composerReadyMs);
  requireMetric(
    composerReadyMs <= fixture.thresholds.composerReadyMs,
    `Composer ${Math.round(composerReadyMs)}ms 后才可输入，超过 ${fixture.thresholds.composerReadyMs}ms`,
  );
  requireMetric(
    await composer.inputValue() === "bootstrap 延迟期间仍可输入",
    "bootstrap 延迟期间 Composer 没有保留输入",
  );
  requireMetric(
    await page.getByText(fixture.target.latestFinalMarker, { exact: false }).count() === 0,
    "释放 bootstrap 前意外显示 latest full detail",
  );
  await markStage("composer-ready-before-bootstrap");

  const raceResponsePromise = page.waitForResponse((response) =>
    response.request().method() === "POST"
    && new URL(response.url()).pathname.endsWith("/messages")
  );
  await composer.fill(fixture.racePrompt);
  await composer.press("Enter");
  const raceResponse = await raceResponsePromise;
  requireMetric(raceResponse.ok(), `竞态 Job 创建失败: HTTP ${raceResponse.status()}`);
  const raceAccepted = (await raceResponse.json()).data;
  const raceJobId = raceAccepted?.job_id;
  requireMetric(Boolean(raceJobId), "竞态 Job 响应缺少 job_id");
  const raceTerminalStatus = raceJobId
    ? await waitForJobTerminal(page, baseUrl, raceJobId)
    : "missing";
  metrics.measurements.race_job_id = raceJobId ?? null;
  metrics.measurements.race_job_terminal_status = raceTerminalStatus;
  requireMetric(
    ["completed", "succeeded"].includes(raceTerminalStatus),
    `旧 bootstrap 被阻塞期间 Job 未成功完成: ${raceTerminalStatus}`,
  );
  requireMetric(
    await page.getByText(fixture.target.latestFinalMarker, { exact: false }).count() === 0,
    "竞态 Job 完成前旧 bootstrap gate 被意外释放",
  );
  await markStage("new-job-completed-before-old-bootstrap");

  const bootstrapReleasedAt = performance.now();
  bootstrapGate.resolve();
  await withTimeout(
    partialBootstrapServed.promise,
    10_000,
    "目标会话没有进入 partial bootstrap",
  );
  await page.getByText("旧 Turn 正在迁移", { exact: false }).waitFor({
    state: "visible",
    timeout: 5_000,
  });
  await page.waitForTimeout(100);
  metrics.measurements.partial_trace_stream_requests = [...targetTraceStreamRequests];
  metrics.measurements.partial_trace_history_requests = [...targetTraceHistoryRequests];
  requireMetric(
    targetTraceStreamRequests.length === 0,
    `partial projection 阶段错误连接 Trace stream: ${targetTraceStreamRequests.join(", ")}`,
  );
  requireMetric(
    targetTraceHistoryRequests.length === 0,
    `partial projection 阶段错误读取 Trace 历史: ${targetTraceHistoryRequests.join(", ")}`,
  );
  await markStage("partial-bootstrap-without-trace-replay");
  readyBootstrapGate.resolve();
  const latestTurn = page.locator(
    `[data-conversation-id="job_turn_e2e_${String(fixture.turnCount).padStart(4, "0")}"]`,
  );
  await latestTurn.waitFor({ state: "visible", timeout: 10_000 });
  await latestTurn.getByText(fixture.target.latestUserMarker, { exact: false }).waitFor({
    state: "visible",
  });
  const latestSummaryMs = performance.now() - bootstrapReleasedAt;
  metrics.measurements.latest_summary_ms = Math.round(latestSummaryMs);
  requireMetric(
    latestSummaryMs <= fixture.thresholds.latestSummaryMs,
    `latest summary ${Math.round(latestSummaryMs)}ms 才显示，超过 ${fixture.thresholds.latestSummaryMs}ms`,
  );
  await withTimeout(
    detailCaptured.promise,
    10_000,
    "latest summary 后没有立即请求 full detail",
  );
  requireMetric(
    await composer.inputValue() === "",
    "竞态 Job 发送后旧 bootstrap 恢复了已发送草稿",
  );
  await markStage("latest-summary-before-detail");

  await composer.focus();
  const markdownInputStartedAt = performance.now();
  detailGate.resolve();
  await composer.press("End");
  await composer.type("Z");
  const markdownInputMs = performance.now() - markdownInputStartedAt;
  metrics.measurements.markdown_concurrent_input_ms = Math.round(markdownInputMs);
  requireMetric(
    markdownInputMs <= fixture.thresholds.composerReadyMs,
    `大型 Markdown 水合期间输入延迟 ${Math.round(markdownInputMs)}ms`,
  );
  await latestTurn.getByRole("heading", {
    name: fixture.target.latestFinalMarker,
  }).waitFor({ state: "visible", timeout: 15_000 });
  const latestDetailMs = performance.now() - bootstrapReleasedAt;
  metrics.measurements.latest_detail_ms = Math.round(latestDetailMs);
  requireMetric(
    latestDetailMs <= fixture.thresholds.latestDetailMs,
    `latest full detail ${Math.round(latestDetailMs)}ms 才显示，超过 ${fixture.thresholds.latestDetailMs}ms`,
  );
  requireMetric(
    (await composer.inputValue()).endsWith("Z"),
    "大型 Markdown 水合覆盖了 Composer 输入",
  );
  if (raceJobId) {
    const raceTurn = page.locator(`[data-conversation-id="${raceJobId}"]`);
    await raceTurn.waitFor({ state: "visible", timeout: 15_000 });
    await raceTurn.getByText(fixture.racePrompt, { exact: false }).waitFor({
      state: "visible",
      timeout: 10_000,
    });
    await raceTurn.getByText(fixture.liveResponse, { exact: false }).waitFor({
      state: "visible",
      timeout: 15_000,
    });
    requireMetric(
      await raceTurn.count() === 1,
      "旧 bootstrap 到达后竞态 Job Turn 丢失或重复",
    );
  }
  await markStage("latest-detail-hydrated");

  const stream = page.locator(".chat-stream");
  const anchorDeltas = [];
  const anchorDiagnostics = [];
  for (let index = 0; index < fixture.historyPagesToLoad; index += 1) {
    await stream.dispatchEvent("wheel", { deltaY: -1 });
    await stream.evaluate((element) => { element.scrollTop = 0; });
    await page.waitForTimeout(100);
    const anchorBefore = await visibleAnchor(stream);
    const responsePromise = page.waitForResponse((response) => {
      const url = new URL(response.url());
      return response.request().method() === "GET"
        && url.pathname.endsWith("/turns")
        && url.searchParams.has("cursor");
    });
    await page.getByRole("button", { name: "加载更早消息" }).click();
    const response = await responsePromise;
    const payload = (await response.json()).data;
    turnPages.push(payload);
    await page.waitForFunction(() => {
      const button = [...document.querySelectorAll("button")]
        .find((element) => element.textContent?.includes("加载更早消息"));
      return button instanceof HTMLButtonElement && !button.disabled;
    });
    const settledAnchor = anchorBefore?.id
      ? await settledAnchorDelta(page, anchorBefore.id, anchorBefore.top)
      : { delta: null, diagnostics: [] };
    anchorDeltas.push(settledAnchor.delta);
    anchorDiagnostics.push({
      before: anchorBefore,
      samples: settledAnchor.diagnostics,
    });
    await markStage(`history-page-${index + 1}`);
  }

  const returnedTurnIds = turnPages.flatMap((pagePayload) =>
    pagePayload.items.map((item) => item.turn_id)
  );
  requireMetric(
    turnPages.every((pagePayload) => pagePayload.items.length === 20),
    "历史分页没有固定返回 20 个完整 Turn",
  );
  requireMetric(
    turnPages.every((pagePayload) =>
      pagePayload.items.every((item) =>
        item.items_view === "summary"
        && item.turn_id === item.job_id
        && item.source_message_count >= 1
      )
    ),
    "历史页包含非 summary 或不完整 Job 边界",
  );
  requireMetric(
    new Set(returnedTurnIds).size === returnedTurnIds.length,
    "历史分页返回了重复 Turn",
  );
  const measuredAnchorDeltas = anchorDeltas.filter((value) => value !== null);
  const maxAnchorDelta = measuredAnchorDeltas.length > 0
    ? Math.max(...measuredAnchorDeltas)
    : Infinity;
  metrics.measurements.history_anchor_delta_px = anchorDeltas;
  metrics.measurements.history_anchor_diagnostics = anchorDiagnostics;
  requireMetric(
    anchorDeltas.every((value) => Number.isFinite(value)),
    `历史前插后存在无法定位的原锚点: ${JSON.stringify(anchorDeltas)}`,
  );
  requireMetric(
    maxAnchorDelta <= fixture.thresholds.maxAnchorDeltaPx,
    `历史前插后的最大锚点偏移 ${maxAnchorDelta}px，超过 ${fixture.thresholds.maxAnchorDeltaPx}px`,
  );

  const oldestLoadedTurnId = turnPages.at(-1)?.items.at(-1)?.turn_id;
  requireMetric(Boolean(oldestLoadedTurnId), "无法确定已加载旧历史锚点");
  const postPaginationResponsePromise = page.waitForResponse((response) =>
    response.request().method() === "POST"
    && new URL(response.url()).pathname.endsWith("/messages")
  );
  await composer.fill(fixture.postPaginationPrompt);
  await composer.press("Enter");
  const postPaginationResponse = await postPaginationResponsePromise;
  requireMetric(
    postPaginationResponse.ok(),
    `分页后 Job 创建失败: HTTP ${postPaginationResponse.status()}`,
  );
  const postPaginationAccepted = (await postPaginationResponse.json()).data;
  const postPaginationJobId = postPaginationAccepted?.job_id;
  requireMetric(Boolean(postPaginationJobId), "分页后 Job 响应缺少 job_id");
  const postPaginationTerminalStatus = postPaginationJobId
    ? await waitForJobTerminal(page, baseUrl, postPaginationJobId)
    : "missing";
  requireMetric(
    ["completed", "succeeded"].includes(postPaginationTerminalStatus),
    `分页后 Job 未成功进入终态: ${postPaginationTerminalStatus}`,
  );

  await stream.evaluate((element) => { element.scrollTop = element.scrollHeight; });
  if (postPaginationJobId) {
    const postPaginationTurn = page.locator(
      `[data-conversation-id="${postPaginationJobId}"]`,
    );
    await postPaginationTurn.waitFor({ state: "visible", timeout: 15_000 });
    await postPaginationTurn.getByText(
      fixture.postPaginationPrompt,
      { exact: false },
    ).waitFor({ state: "visible", timeout: 10_000 });
    await postPaginationTurn.getByText(
      fixture.liveResponse,
      { exact: false },
    ).waitFor({ state: "visible", timeout: 15_000 });
    requireMetric(
      await postPaginationTurn.count() === 1,
      "分页后终态 Turn 丢失或重复",
    );
    requireMetric(
      !returnedTurnIds.includes(postPaginationJobId),
      "分页后新 Turn 与已加载历史 Turn 身份冲突",
    );
    requireMetric(
      detailBatches.some((turnIds) => turnIds.includes(postPaginationJobId)),
      "分页后终态 Job 未通过受限 detail 请求完成水合",
    );
  }

  const latestRenderedTurnIds = await page.locator("[data-turn-id]").evaluateAll(
    (elements) => elements.map((element) => element.getAttribute("data-turn-id")),
  );
  requireMetric(
    latestRenderedTurnIds.every(Boolean)
      && new Set(latestRenderedTurnIds).size === latestRenderedTurnIds.length,
    "分页后 Job 进入终态时最新视口出现重复 Turn 身份",
  );

  await stream.dispatchEvent("wheel", { deltaY: -1 });
  await stream.evaluate((element) => { element.scrollTop = 0; });
  if (oldestLoadedTurnId) {
    const oldestLoadedTurn = page.locator(
      `[data-conversation-id="${oldestLoadedTurnId}"]`,
    );
    await page.waitForFunction((turnId) => {
      const matches = [...document.querySelectorAll("[data-conversation-id]")]
        .filter((element) => element.getAttribute("data-conversation-id") === turnId);
      return matches.length === 1
        && matches[0].getBoundingClientRect().bottom > 0;
    }, oldestLoadedTurnId, { timeout: 10_000 });
    requireMetric(
      await oldestLoadedTurn.count() === 1,
      "分页后 Job 终态水合导致最旧已加载 Turn 丢失或重复",
    );
  }
  const oldestRenderedTurnIds = await page.locator("[data-turn-id]").evaluateAll(
    (elements) => elements.map((element) => element.getAttribute("data-turn-id")),
  );
  requireMetric(
    oldestRenderedTurnIds.every(Boolean)
      && new Set(oldestRenderedTurnIds).size === oldestRenderedTurnIds.length,
    "分页后 Job 进入终态时历史视口出现重复 Turn 身份",
  );
  metrics.measurements.post_pagination_job_id = postPaginationJobId ?? null;
  metrics.measurements.post_pagination_job_terminal_status = (
    postPaginationTerminalStatus
  );
  metrics.measurements.oldest_loaded_turn_after_terminal = oldestLoadedTurnId ?? null;
  await markStage("terminal-turn-preserved-history");

  await Promise.allSettled(networkReads);
  const bootstrapEntry = network.find((entry) =>
    new URL(entry.url).pathname === `${targetSessionPath}/bootstrap`
  );
  const renderedTurns = await page.locator(".chat-turn").count();
  const domNodes = await page.locator("body *").count();
  metrics.measurements.bootstrap_transfer_bytes = bootstrapEntry?.bytes ?? null;
  metrics.measurements.rendered_turn_count = renderedTurns;
  metrics.measurements.total_dom_nodes = domNodes;
  metrics.measurements.detail_batch_sizes = detailBatches.map((ids) => ids.length);
  metrics.measurements.full_trace_requests = fullTraceRequests;
  metrics.measurements.network = network;
  requireMetric(
    typeof bootstrapEntry?.bytes === "number" && bootstrapEntry.bytes > 0,
    "bootstrap 响应体传输量未成功计量",
  );
  requireMetric(
    (bootstrapEntry?.bytes ?? Infinity) <= fixture.thresholds.maxBootstrapBytes,
    `bootstrap 传输 ${bootstrapEntry?.bytes ?? "未知"} bytes，超过 ${fixture.thresholds.maxBootstrapBytes}`,
  );
  requireMetric(
    renderedTurns <= fixture.thresholds.maxRenderedTurns,
    `渲染了 ${renderedTurns} 个 Turn，超过 ${fixture.thresholds.maxRenderedTurns}`,
  );
  requireMetric(
    domNodes <= fixture.thresholds.maxDomNodes,
    `DOM 节点 ${domNodes}，超过 ${fixture.thresholds.maxDomNodes}`,
  );
  requireMetric(
    detailBatches.every((ids) => ids.length <= fixture.thresholds.maxDetailBatchSize),
    `存在超过 ${fixture.thresholds.maxDetailBatchSize} 个 Turn 的详情批次`,
  );
  requireMetric(
    fullTraceRequests.length <= fixture.thresholds.maxFullTraceRequests,
    `主聊天发出了 ${fullTraceRequests.length} 个无上限 Trace 请求`,
  );

  const traceHistoryRequestCountBeforeOpen = targetTraceHistoryRequests.length;
  diagnosticTraceHistoryActive = true;
  const traceTailResponsePromise = page.waitForResponse((response) => {
    const url = new URL(response.url());
    return response.request().method() === "GET"
      && url.pathname === `${targetSessionPath}/traces`
      && url.searchParams.get("limit") === "100"
      && !url.searchParams.has("cursor");
  });
  await page.getByRole("button", { name: /打开视图菜单/ }).click();
  await page.getByRole("menuitemradio", { name: /事件视图/ }).click();
  const traceTailResponse = await traceTailResponsePromise;
  requireMetric(
    traceTailResponse.ok(),
    `打开事件视图读取 Trace tail 失败: HTTP ${traceTailResponse.status()}`,
  );
  await page.getByText("事件视图", { exact: true }).waitFor({
    state: "visible",
    timeout: 5_000,
  });
  requireMetric(
    targetTraceHistoryRequests.length === traceHistoryRequestCountBeforeOpen + 1,
    "事件视图没有按需发起且仅发起一次独立 Trace tail 请求",
  );
  metrics.measurements.diagnostic_trace_history_requests = [
    ...targetTraceHistoryRequests,
  ];
  await markStage("diagnostic-trace-tail-loaded-on-demand");
  await page.getByRole("button", { name: /打开视图菜单/ }).click();
  await page.getByRole("menuitemradio", { name: /默认视图/ }).click();
  diagnosticTraceHistoryActive = false;

  const corruptedSessionButton = page.locator(
    `[data-session-id="${fixture.corrupted.sessionId}"]`,
  );
  const corruptedBootstrapResponse = page.waitForResponse((response) =>
    new URL(response.url()).pathname
      === `/api/v1/sessions/${fixture.corrupted.sessionId}/bootstrap`
  );
  await corruptedSessionButton.click();
  const corruptedResponse = await corruptedBootstrapResponse;
  requireMetric(
    corruptedResponse.status() === 500,
    `损坏投影 bootstrap 应返回 500，实际 ${corruptedResponse.status()}`,
  );
  const corruptedPayload = await corruptedResponse.json();
  requireMetric(
    corruptedPayload?.detail?.code === fixture.corrupted.expectedErrorCode,
    `损坏投影错误码不明确: ${JSON.stringify(corruptedPayload?.detail)}`,
  );
  await composer.waitFor({ state: "visible", timeout: 2_000 });
  await composer.fill("损坏历史期间仍可编辑的草稿");
  requireMetric(await composer.isEnabled(), "损坏投影错误无关地禁用了 Composer");
  requireMetric(
    await composer.inputValue() === "损坏历史期间仍可编辑的草稿",
    "损坏投影错误导致 Composer 草稿丢失",
  );
  const projectionError = page.getByRole("alert").filter({
    hasText: /Turn 投影|manifest|projection/i,
  }).first();
  await projectionError.waitFor({ state: "visible", timeout: 5_000 });
  const projectionErrorText = await projectionError.textContent();
  requireMetric(
    Boolean(projectionErrorText?.match(/Turn 投影|manifest|projection/i)),
    `前端没有展示可诊断投影错误: ${projectionErrorText ?? "空"}`,
  );
  const retryButton = page.getByRole("button", { name: /重试加载/ });
  await retryButton.waitFor({ state: "visible", timeout: 2_000 });
  const retryResponsePromise = page.waitForResponse((response) =>
    new URL(response.url()).pathname
      === `/api/v1/sessions/${fixture.corrupted.sessionId}/bootstrap`
  );
  await retryButton.click();
  const retryResponse = await retryResponsePromise;
  requireMetric(retryResponse.status() === 500, "重试没有重新请求损坏 bootstrap");
  requireMetric(
    await composer.inputValue() === "损坏历史期间仍可编辑的草稿",
    "重试历史加载导致 Composer 草稿丢失",
  );
  requireMetric(
    forbiddenHistoryFallbackRequests.length === 0,
    `主聊天发生旧历史回退请求: ${forbiddenHistoryFallbackRequests.join(", ")}`,
  );
  metrics.measurements.corrupted_projection_error = projectionErrorText;
  metrics.measurements.forbidden_history_fallback_requests = forbiddenHistoryFallbackRequests;
  await markStage("corrupted-projection-visible-without-fallback");
} catch (error) {
  metrics.failures.push(
    `测试执行异常: ${error instanceof Error ? error.stack : String(error)}`,
  );
} finally {
  bootstrapGate.resolve();
  detailGate.resolve();
  await Promise.allSettled(networkReads);
  metrics.measurements.network = network;
  metrics.measurements.detail_batch_sizes = detailBatches.map((ids) => ids.length);
  metrics.measurements.target_trace_stream_requests = targetTraceStreamRequests;
  metrics.measurements.stream_responses = streamResponses;
  metrics.measurements.request_failures = requestFailures;
  metrics.measurements.browser_errors = browserErrors;
  metrics.passed = metrics.failures.length === 0;
  metrics.stage = metrics.passed ? "complete" : metrics.stage;
  await writeFile(metricsPath, `${JSON.stringify(metrics, null, 2)}\n`, "utf8");
  if (!metrics.passed) {
    await page.screenshot({ path: screenshotPath, fullPage: true }).catch(() => undefined);
  }
  await browser.close();
}

if (!metrics.passed) {
  process.stderr.write(`${metrics.failures.join("\n")}\n指标: ${metricsPath}\n`);
  process.exit(1);
}
