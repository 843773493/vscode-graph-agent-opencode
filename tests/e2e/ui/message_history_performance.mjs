import { writeFile } from "node:fs/promises";
import process from "node:process";
import { chromium } from "playwright";

function requiredEnvironment(name) {
  const value = process.env[name];
  if (!value) throw new Error(`缺少环境变量 ${name}`);
  return value;
}

function percentile(values, ratio) {
  if (values.length === 0) return 0;
  const ordered = [...values].sort((left, right) => left - right);
  return ordered[Math.min(ordered.length - 1, Math.ceil(ordered.length * ratio) - 1)];
}

async function usedJsHeap(page) {
  return page.evaluate(() => {
    const candidate = performance;
    return "memory" in candidate ? candidate.memory.usedJSHeapSize : null;
  });
}

async function moveToTopAndReadAnchor(page, stream) {
  await stream.evaluate((element) => { element.scrollTop = 0; });
  await page.waitForTimeout(100);
  return stream.evaluate((element) => {
    const viewportTop = element.getBoundingClientRect().top;
    const visible = [...element.querySelectorAll(".chat-turn")]
      .map((turn) => ({
        id: turn.getAttribute("data-conversation-id"),
        top: turn.getBoundingClientRect().top,
        bottom: turn.getBoundingClientRect().bottom,
      }))
      .find((turn) => turn.bottom > viewportTop + 1);
    return visible ?? null;
  });
}

function isOriginalAttachmentResponse(entry) {
  if (!entry.url.includes("/attachments/content")) return false;
  return new URL(entry.url).searchParams.get("variant") !== "thumbnail";
}

async function selectSession(page, title, latestMarker) {
  const startedAt = performance.now();
  let button = page.locator(".session-item", { hasText: title }).first();
  if (await button.count() === 0) {
    const revealRemaining = page.getByRole("button", { name: /显示剩余.*会话/ });
    if (await revealRemaining.count() > 0) {
      await revealRemaining.click();
      button = page.locator(".session-item", { hasText: title }).first();
    }
  }
  await button.click();
  await page.getByText(latestMarker, { exact: true }).waitFor({ state: "visible", timeout: 10_000 });
  return performance.now() - startedAt;
}

const baseUrl = requiredEnvironment("BOXTEAM_E2E_BASE_URL");
const metricsPath = requiredEnvironment("BOXTEAM_E2E_METRICS_PATH");
const screenshotPath = requiredEnvironment("BOXTEAM_E2E_SCREENSHOT_PATH");
const fixture = JSON.parse(requiredEnvironment("BOXTEAM_E2E_FIXTURE"));
const executablePath = process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH || undefined;

const browser = await chromium.launch({
  executablePath,
  headless: true,
  args: ["--enable-precise-memory-info"],
});
const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
const network = [];
const networkBodyReads = [];
page.on("response", (response) => {
  const url = response.url();
  if (!/\/messages(?:\?|$)|\/attachments\//.test(url)) return;
  const contentLength = Number(response.headers()["content-length"] ?? 0);
  const entry = {
    url,
    status: response.status(),
    bytes: Number.isFinite(contentLength) ? contentLength : 0,
    sizeMeasured: contentLength > 0,
    resourceType: response.request().resourceType(),
  };
  network.push(entry);
  if (entry.bytes === 0) {
    networkBodyReads.push(
      response.body().then((body) => {
        entry.bytes = body.byteLength;
        entry.sizeMeasured = true;
      }).catch(() => undefined),
    );
  }
});

async function settleNetworkMeasurements() {
  await Promise.race([
    Promise.allSettled([...networkBodyReads]),
    new Promise((resolve) => setTimeout(resolve, 3_000)),
  ]);
}

const metrics = {
  schema_version: 1,
  fixture: {
    session_count: fixture.sessions.length,
    turns_per_session: fixture.turnsPerSession,
    image_count_per_session: fixture.imageCountPerSession,
    original_image_bytes: fixture.originalImageBytes,
  },
  thresholds: fixture.thresholds,
  measurements: {},
  failures: [],
};

async function markStage(stage) {
  metrics.stage = stage;
  await writeFile(metricsPath, `${JSON.stringify(metrics, null, 2)}\n`, "utf8");
}

function requireMetric(condition, message) {
  if (!condition) metrics.failures.push(message);
}

try {
  const initialStartedAt = performance.now();
  await page.goto(baseUrl, { waitUntil: "domcontentloaded", timeout: 30_000 });
  const newest = fixture.sessions[fixture.sessions.length - 1];
  await page.getByText(newest.latestMarker, { exact: true }).waitFor({
    state: "visible",
    timeout: 10_000,
  });
  await markStage("initial-latest-visible");
  const initialOpenMs = performance.now() - initialStartedAt;
  await settleNetworkMeasurements();
  const initialTurnCount = await page.locator(".chat-turn").count();
  const initialDomNodes = await page.locator("body *").count();
  const initialMessageResponses = network.filter((entry) => /\/messages(?:\?|$)/.test(entry.url));
  const initialAttachmentResponses = network.filter((entry) => entry.url.includes("/attachments/"));
  const initialOriginalResponses = initialAttachmentResponses.filter(isOriginalAttachmentResponse);

  metrics.measurements.initial_open_ms = Math.round(initialOpenMs);
  metrics.measurements.initial_turn_dom_count = initialTurnCount;
  metrics.measurements.initial_total_dom_nodes = initialDomNodes;
  metrics.measurements.initial_message_response_count = initialMessageResponses.length;
  metrics.measurements.initial_message_transfer_bytes = initialMessageResponses.reduce(
    (sum, entry) => sum + entry.bytes,
    0,
  );
  metrics.measurements.initial_attachment_request_count = initialAttachmentResponses.length;
  metrics.measurements.initial_original_image_request_count = initialOriginalResponses.length;
  metrics.measurements.initial_attachment_transfer_bytes = initialAttachmentResponses.reduce(
    (sum, entry) => sum + entry.bytes,
    0,
  );
  metrics.measurements.initial_unmeasured_response_count = [
    ...initialMessageResponses,
    ...initialAttachmentResponses,
  ].filter((entry) => !entry.sizeMeasured).length;
  const initialMemory = await usedJsHeap(page);
  metrics.measurements.initial_used_js_heap_bytes = initialMemory;

  requireMetric(
    initialOpenMs <= fixture.thresholds.initialOpenMs,
    `初次打开最新页 ${Math.round(initialOpenMs)}ms，超过 ${fixture.thresholds.initialOpenMs}ms`,
  );
  requireMetric(
    initialTurnCount <= fixture.thresholds.maxRenderedTurns,
    `初次渲染 ${initialTurnCount} 个对话轮，超过 ${fixture.thresholds.maxRenderedTurns}，疑似全量 DOM`,
  );
  requireMetric(
    initialDomNodes <= fixture.thresholds.maxDomNodes,
    `初次页面 DOM ${initialDomNodes} 个节点，超过 ${fixture.thresholds.maxDomNodes}`,
  );
  requireMetric(
    metrics.measurements.initial_unmeasured_response_count === 0,
    `初次加载有 ${metrics.measurements.initial_unmeasured_response_count} 个响应未能取得传输大小`,
  );
  requireMetric(
    metrics.measurements.initial_message_transfer_bytes <= fixture.thresholds.maxInitialMessageBytes,
    `初次消息页传输 ${metrics.measurements.initial_message_transfer_bytes} bytes，超过 ${fixture.thresholds.maxInitialMessageBytes}`,
  );
  requireMetric(
    initialOriginalResponses.length === 0,
    `未打开查看器时请求了 ${initialOriginalResponses.length} 个原图`,
  );
  requireMetric(
    initialAttachmentResponses.length <= fixture.thresholds.maxInitialAttachmentRequests,
    `初次打开请求 ${initialAttachmentResponses.length} 个附件，超过 ${fixture.thresholds.maxInitialAttachmentRequests}`,
  );
  requireMetric(
    metrics.measurements.initial_attachment_transfer_bytes <= fixture.thresholds.maxInitialAttachmentBytes,
    `初次附件传输 ${metrics.measurements.initial_attachment_transfer_bytes} bytes，超过 ${fixture.thresholds.maxInitialAttachmentBytes}`,
  );

  const coldSwitchDurations = [];
  for (const session of fixture.sessions.slice(0, -1).reverse()) {
    coldSwitchDurations.push(await selectSession(page, session.title, session.latestMarker));
    await markStage(`cold-switch-${session.title}`);
  }
  const attachmentsBeforeWarmReturn = network.filter(
    (entry) => entry.url.includes("/attachments/"),
  ).length;
  const warmReturnMs = await selectSession(page, newest.title, newest.latestMarker);
  await markStage("warm-return-visible");
  await settleNetworkMeasurements();
  const attachmentsAfterWarmReturn = network.filter(
    (entry) => entry.url.includes("/attachments/"),
  ).length;
  const switchP95Ms = percentile(coldSwitchDurations, 0.95);
  metrics.measurements.switch_session_ms = coldSwitchDurations.map(Math.round);
  metrics.measurements.switch_session_p95_ms = Math.round(switchP95Ms);
  metrics.measurements.cached_session_return_ms = Math.round(warmReturnMs);
  metrics.measurements.cached_session_attachment_request_delta =
    attachmentsAfterWarmReturn - attachmentsBeforeWarmReturn;
  requireMetric(
    switchP95Ms <= fixture.thresholds.switchSessionP95Ms,
    `切换会话 P95 ${Math.round(switchP95Ms)}ms，超过 ${fixture.thresholds.switchSessionP95Ms}ms`,
  );
  requireMetric(
    warmReturnMs <= fixture.thresholds.cachedSessionReturnMs,
    `切回已访问会话 ${Math.round(warmReturnMs)}ms，超过 ${fixture.thresholds.cachedSessionReturnMs}ms`,
  );
  requireMetric(
    attachmentsAfterWarmReturn - attachmentsBeforeWarmReturn
      <= fixture.thresholds.maxCachedSessionAttachmentRequests,
    `切回已访问会话重复请求 ${attachmentsAfterWarmReturn - attachmentsBeforeWarmReturn} 个附件，超过 ${fixture.thresholds.maxCachedSessionAttachmentRequests}`,
  );

  const stream = page.locator(".chat-stream");
  const historyRequestsBefore = network.filter((entry) => /\/messages\?.*(cursor|before)/.test(entry.url)).length;
  const historyLoadDurations = [];
  const historyAnchorDeltas = [];
  for (let index = 0; index < fixture.historyPagesToLoad; index += 1) {
    await markStage(`history-page-${index + 1}-start`);
    const anchorBefore = await moveToTopAndReadAnchor(page, stream);
    const loadOlderButton = page.getByRole("button", { name: "加载更早消息" });
    await loadOlderButton.waitFor({ state: "visible", timeout: 10_000 });
    const responsePromise = page.waitForResponse(
      (response) => /\/messages\?.*(cursor|before)/.test(response.url()),
      { timeout: 10_000 },
    );
    const historyStartedAt = performance.now();
    await loadOlderButton.click();
    await responsePromise;
    await page.waitForTimeout(100);
    const historyLoadMs = performance.now() - historyStartedAt;
    const anchorAfter = anchorBefore?.id
      ? await page.locator(`[data-conversation-id="${anchorBefore.id}"]`).evaluate((turn) => ({
        id: turn.getAttribute("data-conversation-id"),
        top: turn.getBoundingClientRect().top,
      })).catch(() => null)
      : null;
    historyLoadDurations.push(historyLoadMs);
    historyAnchorDeltas.push(
      anchorBefore && anchorAfter ? Math.abs(anchorAfter.top - anchorBefore.top) : Infinity,
    );
    await markStage(`history-page-${index + 1}-complete`);
  }
  const historyRequestsAfter = network.filter((entry) => /\/messages\?.*(cursor|before)/.test(entry.url)).length;
  const historyLoadP95Ms = percentile(historyLoadDurations, 0.95);
  const maxAnchorDelta = Math.max(...historyAnchorDeltas);
  metrics.measurements.history_load_ms = historyLoadDurations.map(Math.round);
  metrics.measurements.history_load_p95_ms = Math.round(historyLoadP95Ms);
  metrics.measurements.history_request_delta = historyRequestsAfter - historyRequestsBefore;
  metrics.measurements.history_anchor_delta_px = historyAnchorDeltas.map(
    (value) => Number.isFinite(value) ? Math.round(value) : null,
  );
  metrics.measurements.turn_dom_count_after_history = await page.locator(".chat-turn").count();
  requireMetric(
    historyLoadP95Ms <= fixture.thresholds.historyLoadP95Ms,
    `向上加载历史 P95 ${Math.round(historyLoadP95Ms)}ms，超过 ${fixture.thresholds.historyLoadP95Ms}ms`,
  );
  requireMetric(
    historyRequestsAfter - historyRequestsBefore >= fixture.historyPagesToLoad,
    `只发出 ${historyRequestsAfter - historyRequestsBefore} 个历史分页请求，预期至少 ${fixture.historyPagesToLoad} 个`,
  );
  requireMetric(
    maxAnchorDelta <= fixture.thresholds.maxAnchorDeltaPx,
    `历史加载后最大滚动锚点偏移 ${Number.isFinite(maxAnchorDelta) ? Math.round(maxAnchorDelta) : "不可测"}px，超过 ${fixture.thresholds.maxAnchorDeltaPx}px`,
  );
  requireMetric(
    metrics.measurements.turn_dom_count_after_history <= fixture.thresholds.maxRenderedTurns,
    `加载历史后渲染 ${metrics.measurements.turn_dom_count_after_history} 个对话轮，超过 ${fixture.thresholds.maxRenderedTurns}`,
  );

  const imageButton = page.getByRole("button", { name: /查看图片/ }).first();
  await imageButton.scrollIntoViewIfNeeded();
  const originalsBeforeViewer = network.filter(isOriginalAttachmentResponse).length;
  await imageButton.click();
  await markStage("viewer-open-requested");
  await page.getByRole("dialog", { name: /查看图片/ }).waitFor({ state: "visible" });
  await page.waitForTimeout(300);
  await settleNetworkMeasurements();
  const originalsAfterViewer = network.filter(isOriginalAttachmentResponse).length;
  metrics.measurements.viewer_original_image_request_delta = originalsAfterViewer - originalsBeforeViewer;
  requireMetric(
    originalsAfterViewer - originalsBeforeViewer <= fixture.thresholds.maxViewerOriginalRequests,
    `打开查看器请求了 ${originalsAfterViewer - originalsBeforeViewer} 个原图，超过当前图加相邻预取预算 ${fixture.thresholds.maxViewerOriginalRequests}`,
  );
  await page.getByRole("button", { name: "关闭图片查看器" }).click();

  const memory = await usedJsHeap(page);
  metrics.measurements.used_js_heap_bytes = memory;
  if (typeof memory === "number") {
    requireMetric(
      memory <= fixture.thresholds.maxUsedJsHeapBytes,
      `JS heap ${memory} bytes，超过 ${fixture.thresholds.maxUsedJsHeapBytes}`,
    );
    if (typeof initialMemory === "number") {
      const growth = memory - initialMemory;
      metrics.measurements.used_js_heap_growth_bytes = growth;
      requireMetric(
        growth <= fixture.thresholds.maxUsedJsHeapGrowthBytes,
        `切换和加载历史后 JS heap 增长 ${growth} bytes，超过 ${fixture.thresholds.maxUsedJsHeapGrowthBytes}`,
      );
    }
  }

  await stream.evaluate((element) => { element.scrollTop = element.scrollHeight; });
  await page.getByText(newest.latestMarker, { exact: true }).waitFor({ state: "visible" });
  metrics.measurements.final_turn_dom_count = await page.locator(".chat-turn").count();
  metrics.measurements.network = network;
  requireMetric(
    await page.getByText(newest.latestMarker, { exact: true }).isVisible(),
    "性能操作结束后最新消息不可见",
  );
} catch (error) {
  metrics.failures.push(`测试执行异常: ${error instanceof Error ? error.stack : String(error)}`);
} finally {
  metrics.measurements.network = network;
  metrics.passed = metrics.failures.length === 0;
  metrics.stage = metrics.passed ? "complete" : metrics.stage;
  await writeFile(metricsPath, `${JSON.stringify(metrics, null, 2)}\n`, "utf8");
  if (!metrics.passed) {
    await page.screenshot({ path: screenshotPath }).catch(() => undefined);
  }
  await browser.close();
}

if (!metrics.passed) {
  process.stderr.write(`${metrics.failures.join("\n")}\n指标: ${metricsPath}\n`);
  process.exit(1);
}
