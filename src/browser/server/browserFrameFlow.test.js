import { describe, expect, test } from "bun:test";
import { BrowserFrameFlow } from "./browserFrameFlow.js";

function harness() {
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
});
