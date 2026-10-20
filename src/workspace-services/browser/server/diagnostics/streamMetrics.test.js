import assert from "node:assert/strict";
import test from "node:test";
import { BrowserSession } from "../browserSession.js";
import { summarizeBrowserStreamMetrics } from "./streamMetrics.js";

for (const observations of [[], [{ observedAtUnixMs: 1000, jpegPayloadBytes: 100 }]]) {
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
    Object.freeze({ observedAtUnixMs: 1000, jpegPayloadBytes: 100 }),
    Object.freeze({ observedAtUnixMs: 2000, jpegPayloadBytes: 200 }),
    Object.freeze({ observedAtUnixMs: 3000, jpegPayloadBytes: 300 }),
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
      { observedAtUnixMs: 1000, jpegPayloadBytes: 1 },
      { observedAtUnixMs: 1000, jpegPayloadBytes: 2 },
      { observedAtUnixMs: 1000, jpegPayloadBytes: 3 },
    ],
    deliveryClients: [], profile: "relaxed", lastAttachFirstFrameMs: 0,
  });
  assert.equal(result.fps, 2000);
  assert.equal(result.bitrate_bps, 48000);
  assert.equal(result.last_attach_first_frame_ms, 0);
});

test("Session采样/快照接线保持五秒窗口、静默结果、首帧耗时及连接计数分层", (t) => {
  const origin = Date.parse("2026-10-20T00:00:00.000Z");
  let nowUnixMs = origin;
  t.mock.method(Date, "now", () => nowUnixMs);
  const browser = new BrowserSession({
    manager: { persist: async () => undefined },
    record: { browser_id: "browser_metrics_test", status: "running", created_at: new Date(origin).toISOString() },
  });
  const first = Object.freeze({ frames_sent: 10, frames_superseded: 2, ack_timeouts: 1, max_ack_rtt_ms: 20, last_client_draw_ms: 4 });
  const second = Object.freeze({ frames_sent: 4, frames_superseded: 1, ack_timeouts: 0, max_ack_rtt_ms: 5, last_client_decode_ms: 2 });
  browser.clients.add({ frameFlowSnapshot: () => first });
  browser.clients.add({ frameFlowSnapshot: () => second });
  browser.clients.add({});
  browser.streamProfile = "interactive";
  browser.attachRequestedAtUnixMs = origin - 80;
  browser.recordFrameObservation(100);
  assert.equal(browser.snapshot().stream_metrics.last_attach_first_frame_ms, 80);
  assert.equal(browser.attachRequestedAtUnixMs, null);

  nowUnixMs += 5000;
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

  nowUnixMs += 1;
  browser.recordFrameObservation(300);
  const afterBoundary = browser.snapshot().stream_metrics;
  assert.equal(afterBoundary.sample_frames, 2);
  assert.equal(afterBoundary.fps, 1000);
  assert.equal(afterBoundary.bitrate_bps, 4000000);
  assert.equal(afterBoundary.last_attach_first_frame_ms, 80);
  nowUnixMs += 10000;
  assert.deepEqual(browser.snapshot().stream_metrics, afterBoundary);
});
