import { openSync, writeSync, closeSync } from "node:fs";

// 仅观察真实页面，初始化脚本不修改业务状态或网络请求。
export async function observePage(page, perf, context) {
  if (!["core", "diagnostic"].includes(perf.mode)) return { checkpoint: async () => {} };
  await page.addInitScript(() => {
    const stats = { count: 0, totalMs: 0, maxMs: 0, longtask_supported: PerformanceObserver.supportedEntryTypes.includes("longtask") };
    globalThis.__testPerformanceLongTasks = stats;
    if (stats.longtask_supported) {
      const observer = new PerformanceObserver(list => {
        for (const item of list.getEntries()) {
          stats.count++;
          stats.totalMs += item.duration;
          stats.maxMs = Math.max(stats.maxMs, item.duration);
        }
      });
      observer.observe({ type: "longtask", buffered: true });
    }
  });
  const pending = new Map();
  let started = 0, finished = 0, failed = 0, transferred = 0;
  const request = item => { started++; pending.set(item, { started: performance.now() }); };
  const response = item => {
    const value = pending.get(item.request());
    if (value) Object.assign(value, { status: item.status(), requestId: item.headers()["x-request-id"] ?? null,
      headersAt: performance.now() });
  };
  const complete = item => {
    finished++;
    const value = pending.get(item);
    pending.delete(item);
    const pathname = new URL(item.url()).pathname;
    if (value && pathname.startsWith("/api/")) {
      perf.record("end", "http.browser.request", { stage_id: crypto.randomUUID(), inclusive: true,
        elapsed_ns: Math.round((performance.now() - value.started) * 1e6),
        response_headers_ns: value.headersAt === undefined ? null : Math.round((value.headersAt - value.started) * 1e6),
        status: value.status >= 400 ? "http_error" : "passed", status_code: value.status ?? null,
        request_id: value.requestId ?? null, boundary: "playwright_requestfinished_event" },
        { path: pathname.replace(/(?:ses_|thr_|turn_|job_)[a-zA-Z0-9_-]+/g, "<id>"), method: item.method() });
    }
  };
  const rejected = item => { failed++; pending.delete(item); };
  const cdp = await context.newCDPSession(page);
  const listeners = [["request", request], ["response", response], ["requestfinished", complete], ["requestfailed", rejected]];
  for (const [event, handler] of listeners) page.on(event, handler);
  const tool = process.env.BOXTEAM_TEST_PERF_TOOL;
  let traceFd, traceBytes = 0, traceDropped = 0, traceFirst = true;
  try {
    await cdp.send("Performance.enable");
    await cdp.send("Network.enable");
    cdp.on("Network.loadingFinished", event => { transferred += event.encodedDataLength; });
    if (tool === "browser-trace") {
      traceFd = openSync(`${perf.path}.trace.json`, "wx");
      writeSync(traceFd, '{"traceEvents":[');
      cdp.on("Tracing.dataCollected", ({ value }) => {
        for (const item of value) {
          // timeline只保留标量时序，移除args中的URL、请求头、正文与页面内容。
          const safe = Object.fromEntries(["name", "cat", "ph", "ts", "dur", "pid", "tid"].filter(key => item[key] !== undefined).map(key => [key, item[key]]));
          const line = `${traceFirst ? "" : ","}${JSON.stringify(safe)}`;
          if (traceBytes + Buffer.byteLength(line) > perf.limit) { traceDropped++; continue; }
          writeSync(traceFd, line);
          traceFirst = false;
          traceBytes += Buffer.byteLength(line);
        }
      });
      await cdp.send("Tracing.start", { categories: "devtools.timeline", transferMode: "ReportEvents" });
    }
    if (tool === "browser-cpu") {
      await cdp.send("Profiler.enable");
      await cdp.send("Profiler.start");
    }
  } catch (error) {
    for (const [event, handler] of listeners) page.off(event, handler);
    try { if (traceFd !== undefined) closeSync(traceFd); }
    finally { await cdp.detach(); }
    throw error;
  }
  return {
    async checkpoint(label, round = 0) {
      const pageMetrics = await page.evaluate(() => ({
        domNodes: document.getElementsByTagName("*").length,
        ...globalThis.__testPerformanceLongTasks,
      }));
      const metrics = await cdp.send("Performance.getMetrics");
      const allowed = new Set(["JSHeapUsedSize", "JSHeapTotalSize", "Nodes", "Documents", "JSEventListeners", "TaskDuration"]);
      const values = Object.fromEntries(metrics.metrics.filter(item => allowed.has(item.name)).map(item => [item.name, item.value]));
      perf.record("metric", "browser.checkpoint", { label, round, ...pageMetrics, ...values,
        requests_started: started, requests_finished: finished, requests_failed: failed,
        requests_inflight: pending.size, transferred_bytes: transferred,
        sse_to_render: "not_correlated", scope: "page_cumulative_since_observation" });
    },
    async close() {
      for (const [event, handler] of listeners) page.off(event, handler);
      try {
        if (tool === "browser-cpu") {
          const { writeFile } = await import("node:fs/promises");
          const { profile } = await cdp.send("Profiler.stop");
          for (const node of profile.nodes) {
            const url = node.callFrame.url;
            if (url) node.callFrame.url = /^https?:/.test(url) ? new URL(url).origin + new URL(url).pathname : "<local-or-inline>";
          }
          const file = `${perf.path}.cpuprofile`;
          await writeFile(file, JSON.stringify(profile));
          perf.record("diagnostic", "browser.cpu", { tool: "CDP.Profiler", path: file, scope: "page" });
        }
        if (traceFd !== undefined) {
          try {
            const completed = new Promise(resolve => cdp.once("Tracing.tracingComplete", resolve));
            await cdp.send("Tracing.end");
            await completed;
            writeSync(traceFd, "]}");
          } finally { closeSync(traceFd); }
          perf.record("diagnostic", "browser.trace", { tool: "CDP.Tracing", path: `${perf.path}.trace.json`,
            dropped_events: traceDropped, payloads: "omitted", scope: "browser_timeline" });
        }
      } finally { await cdp.detach(); }
    },
  };
}
