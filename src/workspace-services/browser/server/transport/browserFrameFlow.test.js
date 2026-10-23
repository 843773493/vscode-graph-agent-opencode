import { describe, expect, spyOn, test } from "bun:test";
import { BrowserFrameFlow } from "./browserFrameFlow.js";

function harness(overrides = {}) {
  const sent = [];
  const timers = new Map();
  let timerId = 0;
  let nowMs = 1_000;
  const socket = {
    readyState: 1,
    bufferedAmount: 0,
    send: (frame) => sent.push(frame),
  };
  const flow = new BrowserFrameFlow({
    socket,
    encodeFrame: (frame) => frame.frameId,
    now: () => nowMs,
    schedule: (callback) => {
      timerId += 1;
      timers.set(timerId, callback);
      return timerId;
    },
    cancel: (id) => timers.delete(id),
    ...overrides,
  });
  return {
    flow,
    sent,
    timers,
    advance: (milliseconds) => { nowMs += milliseconds; },
    runTimer: (id) => {
      const callback = timers.get(id);
      timers.delete(id);
      callback?.();
    },
  };
}

describe("浏览器最新帧流控", () => {
  test.each([-60_000, 60_000])("系统校时 %i ms 不改变默认时钟的 ACK 耗时", (clockShiftMs) => {
    let elapsedClockMs = 1_000;
    const wallClock = spyOn(Date, "now").mockReturnValue(1_800_000_000_000);
    const elapsedClock = spyOn(performance, "now").mockImplementation(() => elapsedClockMs);
    const state = harness({ now: undefined });
    try {
      state.flow.offer({ frameId: 50 });
      state.flow.offer({ frameId: 51 });
      wallClock.mockReturnValue(1_800_000_000_000 + clockShiftMs);
      elapsedClockMs += 24;
      expect(state.flow.acknowledge(50)).toBe(true);
      expect(state.sent).toEqual([50, 51]);
      expect(state.flow.snapshot()).toMatchObject({
        last_ack_rtt_ms: 24,
        max_ack_rtt_ms: 24,
        awaiting_frame_id: 51,
        ack_timeouts: 0,
      });
      elapsedClockMs += 6;
      expect(state.flow.acknowledge(51)).toBe(true);
      expect(state.flow.snapshot()).toMatchObject({
        last_ack_rtt_ms: 6,
        max_ack_rtt_ms: 24,
        awaiting_frame_id: null,
      });
      expect(state.timers.size).toBe(0);
    } finally {
      state.flow.close();
      elapsedClock.mockRestore();
      wallClock.mockRestore();
    }
  });

  test("未确认期间只保留最新帧", () => {
    const state = harness();
    state.flow.offer({ frameId: 1 });
    state.flow.offer({ frameId: 2 });
    state.flow.offer({ frameId: 3 });

    expect(state.sent).toEqual([1]);
    expect(state.flow.snapshot().frames_superseded).toBe(1);

    state.advance(24);
    expect(state.flow.acknowledge(1, { decodeMs: 4, drawMs: 2 })).toBe(true);
    expect(state.sent).toEqual([1, 3]);
    expect(state.flow.snapshot()).toMatchObject({
      awaiting_frame_id: 3,
      last_ack_rtt_ms: 24,
      last_client_decode_ms: 4,
      last_client_draw_ms: 2,
    });
  });

  test("确认超时后发送当时最新帧", () => {
    const state = harness();
    state.flow.offer({ frameId: 10 });
    state.flow.offer({ frameId: 11 });
    const timeoutId = [...state.timers.keys()][0];

    state.runTimer(timeoutId);

    expect(state.sent).toEqual([10, 11]);
    expect(state.flow.snapshot().ack_timeouts).toBe(1);
  });

  test("WebSocket积压时不继续写入旧帧", () => {
    const state = harness();
    state.flow.socket.bufferedAmount = 256 * 1024;
    state.flow.offer({ frameId: 20 });
    state.flow.offer({ frameId: 21 });
    expect(state.sent).toEqual([]);

    state.flow.socket.bufferedAmount = 0;
    const retryId = [...state.timers.keys()][0];
    state.runTimer(retryId);

    expect(state.sent).toEqual([21]);
    expect(state.flow.snapshot().frames_superseded).toBe(1);
  });

  test("积压解除后新帧先于重试到达，确认后不再发送旧待发帧", () => {
    const state = harness();
    state.flow.socket.bufferedAmount = 256 * 1024;
    state.flow.offer({ frameId: 20 });
    const retryId = [...state.timers.keys()][0];
    expect(state.timers.size).toBe(1);

    state.flow.socket.bufferedAmount = 0;
    state.flow.offer({ frameId: 21 });
    expect(state.sent).toEqual([21]);
    expect(state.flow.acknowledge(21)).toBe(true);
    state.runTimer(retryId);

    expect(state.sent).toEqual([21]);
    expect(state.flow.snapshot()).toMatchObject({
      has_pending_frame: false,
      awaiting_frame_id: null,
      frames_superseded: 1,
    });
    expect(state.timers.size).toBe(0);
  });

  test("ACK超时遇到socket积压，新帧取代待发帧且迟到ACK不改变发送顺序", () => {
    const state = harness();
    state.flow.offer({ frameId: 30 });
    state.flow.offer({ frameId: 31 });
    state.flow.socket.bufferedAmount = 256 * 1024;
    state.runTimer([...state.timers.keys()][0]);
    const retryId = [...state.timers.keys()][0];
    expect(state.timers.size).toBe(1);

    state.flow.socket.bufferedAmount = 0;
    state.flow.offer({ frameId: 32 });
    expect(state.flow.acknowledge(30)).toBe(false);
    expect(state.flow.acknowledge(32)).toBe(true);
    state.runTimer(retryId);

    expect(state.sent).toEqual([30, 32]);
    expect(state.flow.snapshot()).toMatchObject({
      has_pending_frame: false,
      awaiting_frame_id: null,
      frames_superseded: 1,
      ack_timeouts: 1,
    });
    expect(state.timers.size).toBe(0);
  });

  test("重置丢弃旧待发帧和定时器，关闭后不再接收帧或ACK", () => {
    const state = harness();
    state.flow.socket.bufferedAmount = 256 * 1024;
    state.flow.offer({ frameId: 40 });
    expect(state.timers.size).toBe(1);

    state.flow.reset();
    expect(state.timers.size).toBe(0);
    state.flow.socket.bufferedAmount = 0;
    expect(state.flow.offer({ frameId: 41 })).toBe(true);
    state.flow.close();

    expect(state.flow.offer({ frameId: 42 })).toBe(false);
    expect(state.flow.acknowledge(41)).toBe(false);
    expect(state.sent).toEqual([41]);
    expect(state.timers.size).toBe(0);
  });
});
