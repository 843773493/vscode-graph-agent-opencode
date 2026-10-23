import assert from "node:assert/strict";
import test from "node:test";
import { BrowserSession } from "../browserSession.js";
import { summarizeBrowserStreamMetrics } from "./streamMetrics.js";

for (const clockShiftMs of [-60_000, 0, 60_000]) {
  test(`实际attach和帧入口在校时${clockShiftMs}ms后仍按经过时间统计`, async (t) => {
    const originUnixMs = Date.parse("2026-10-20T00:00:00.000Z");
    let wallClockMs = originUnixMs;
    let elapsedClockMs = 1_000.25;
    t.mock.method(Date, "now", () => wallClockMs);
    t.mock.method(performance, "now", () => elapsedClockMs);
    const browser = new BrowserSession({
      manager: { persist: async () => undefined },
      record: {
        browser_id: "browser_metrics_clock", status: "running", resource_state: "background",
        reconciliation_status: "verified", transport_owner_revision: 1,
        created_at: new Date(originUnixMs).toISOString(), device_scale_factor_override: 1,
      },
    });
    t.after(() => clearTimeout(browser.streamRelaxTimer));
    const sentFrames = [];
    const commands = [];
    const client = { sendFrame: (frame) => sentFrames.push(frame) };
    browser.transports.set(client, { transportOwnerRevision: 1 });
    const page = { isClosed: () => false, url: () => "about:blank", title: async () => "统计测试" };
    const cdpSession = { send: async (method, parameters) => {
      commands.push({ method, parameters });
      if (method === "Page.captureScreenshot") {
        elapsedClockMs += 80.5;
        wallClockMs += 80 + clockShiftMs;
        return { data: Buffer.alloc(100).toString("base64") };
      }
      assert(["Page.startScreencast", "Page.screencastFrameAck"].includes(method), method);
    } };
    browser.browser = {};
    browser.context = {};
    browser.page = page;
    browser.cdpSession = cdpSession;
    browser.activePageId = "page_metrics_clock";
    browser.pageEntries.set(browser.activePageId, {
      pageId: browser.activePageId, page, cdpSession, webSockets: new Set(),
    });

    // 通过真实attach设置计时起点，再让手动首帧与CDP后续帧共用采样入口。
    const attached = await browser.attachClient(client);
    const observedMetrics = [attached.stream_metrics];
    let sessionId = 0;
    for (const [elapsedMs, bytes, correctionCount] of [[1_000, 200, 2], [5_000, 300, 3], [5_001, 400, 4]]) {
      elapsedClockMs = 1_080.75 + elapsedMs;
      wallClockMs = originUnixMs + 80 + elapsedMs + correctionCount * clockShiftMs;
      sessionId += 1;
      const sourceTimestamp = 1_790_000_000 + sessionId;
      await browser.handleScreencastFrame({
        data: Buffer.alloc(bytes).toString("base64"), sessionId,
        metadata: { timestamp: sourceTimestamp, pageScaleFactor: 1 },
      });
      assert.equal(browser.lastFrame.timestamp, sourceTimestamp, "CDP线格式保留原始Unix秒");
      observedMetrics.push(browser.snapshot().stream_metrics);
    }
    assert.deepEqual(commands.map(command => command.method), [
      "Page.startScreencast", "Page.captureScreenshot",
      "Page.screencastFrameAck", "Page.screencastFrameAck", "Page.screencastFrameAck",
    ]);
    assert.deepEqual(commands.filter(command => command.method === "Page.screencastFrameAck")
      .map(command => command.parameters.sessionId), [1, 2, 3]);
    assert.equal(sentFrames.length, 1);
    assert.equal(sentFrames[0].timestamp, (originUnixMs + 80 + clockShiftMs) / 1_000,
      "手动截图线格式仍为Unix秒");
    assert.equal(attached.client_count, 1);
    assert.deepEqual(observedMetrics.map(({sample_frames, fps, bitrate_bps, last_attach_first_frame_ms}) => (
      {sample_frames, fps, bitrate_bps, last_attach_first_frame_ms}
    )), [
      {sample_frames: 1, fps: 0, bitrate_bps: 0, last_attach_first_frame_ms: 80},
      {sample_frames: 2, fps: 1, bitrate_bps: 2400, last_attach_first_frame_ms: 80},
      {sample_frames: 3, fps: 0.4, bitrate_bps: 960, last_attach_first_frame_ms: 80},
      {sample_frames: 3, fps: 0.5, bitrate_bps: 1800, last_attach_first_frame_ms: 80},
    ]);
    elapsedClockMs += 10_000;
    wallClockMs += 10_000 + clockShiftMs;
    assert.deepEqual(browser.snapshot().stream_metrics, observedMetrics.at(-1), "静默不推进观察窗口");
  });
}

for (const observations of [[], [{ observedAtMonotonicMs: 1000, jpegPayloadBytes: 100 }]]) {
  test(`${observations.length}帧样本的速率为零，缺省首帧耗时保持null`, () => {
    assert.deepEqual(summarizeBrowserStreamMetrics({
      observations, deliveryClients: [], profile: null, lastAttachFirstFrameMs: undefined,
    }), {
      profile: null, fps: 0, bitrate_bps: 0, sample_frames: observations.length,
      last_attach_first_frame_ms: null,
      delivery: { client_count: 0, frames_sent: 0, frames_superseded: 0, ack_timeouts: 0, max_ack_rtt_ms: 0, clients: [] },
    });
  });
}

test("帧率用相邻样本间隔，正文速率包含窗口内全部JPEG字节，不改变输入", () => {
  const observations = Object.freeze([
    Object.freeze({ observedAtMonotonicMs: 1000, jpegPayloadBytes: 100 }),
    Object.freeze({ observedAtMonotonicMs: 2000, jpegPayloadBytes: 200 }),
    Object.freeze({ observedAtMonotonicMs: 3000, jpegPayloadBytes: 300 }),
  ]);
  const result = summarizeBrowserStreamMetrics({
    observations, deliveryClients: Object.freeze([]), profile: "interactive", lastAttachFirstFrameMs: 80,
  });
  assert.equal(result.fps, 1);
  assert.equal(result.bitrate_bps, 2400);
  assert.equal(result.sample_frames, 3);
  assert.equal(result.profile, "interactive");
  assert.equal(result.last_attach_first_frame_ms, 80);
  assert.equal(observations.length, 3);
});

test("同一毫秒的多帧仍按原有1ms最小跨度计算", () => {
  const result = summarizeBrowserStreamMetrics({
    observations: [
      { observedAtMonotonicMs: 1000, jpegPayloadBytes: 1 },
      { observedAtMonotonicMs: 1000, jpegPayloadBytes: 2 },
      { observedAtMonotonicMs: 1000, jpegPayloadBytes: 3 },
    ],
    deliveryClients: [], profile: "relaxed", lastAttachFirstFrameMs: 0,
  });
  assert.equal(result.fps, 2000);
  assert.equal(result.bitrate_bps, 48000);
  assert.equal(result.last_attach_first_frame_ms, 0);
});

test("Session采样/快照接线保持五秒窗口、静默结果、首帧耗时及连接计数分层", (t) => {
  const origin = 1_000;
  let nowMonotonicMs = origin;
  t.mock.method(performance, "now", () => nowMonotonicMs);
  const browser = new BrowserSession({
    manager: { persist: async () => undefined },
    record: { browser_id: "browser_metrics_test", status: "running", created_at: "2026-10-20T00:00:00.000Z" },
  });
  const first = Object.freeze({ frames_sent: 10, frames_superseded: 2, ack_timeouts: 1, max_ack_rtt_ms: 20, last_client_draw_ms: 4 });
  const second = Object.freeze({ frames_sent: 4, frames_superseded: 1, ack_timeouts: 0, max_ack_rtt_ms: 5, last_client_decode_ms: 2 });
  browser.clients.add({ frameFlowSnapshot: () => first });
  browser.clients.add({ frameFlowSnapshot: () => second });
  browser.clients.add({});
  browser.streamProfile = "interactive";
  browser.attachRequestedAtMonotonicMs = origin - 80;
  browser.recordFrameObservation(100);
  assert.equal(browser.snapshot().stream_metrics.last_attach_first_frame_ms, 80);
  assert.equal(browser.attachRequestedAtMonotonicMs, null);

  nowMonotonicMs += 5000;
  browser.recordFrameObservation(200);
  const atBoundary = browser.snapshot();
  assert.equal(atBoundary.stream_metrics.sample_frames, 2);
  assert.equal(atBoundary.stream_metrics.fps, 0.2);
  assert.equal(atBoundary.stream_metrics.bitrate_bps, 480);
  assert.equal(atBoundary.client_count, 3);
  assert.deepEqual(atBoundary.stream_metrics.delivery, {
    client_count: 2, frames_sent: 14, frames_superseded: 3, ack_timeouts: 1, max_ack_rtt_ms: 20,
    clients: [first, second],
  });

  nowMonotonicMs += 1;
  browser.recordFrameObservation(300);
  const afterBoundary = browser.snapshot().stream_metrics;
  assert.equal(afterBoundary.sample_frames, 2);
  assert.equal(afterBoundary.fps, 1000);
  assert.equal(afterBoundary.bitrate_bps, 4000000);
  assert.equal(afterBoundary.last_attach_first_frame_ms, 80);
  nowMonotonicMs += 10000;
  assert.deepEqual(browser.snapshot().stream_metrics, afterBoundary);
});
