const DEFAULT_ACK_TIMEOUT_MS = 1_500;
const DEFAULT_BUFFER_RETRY_MS = 8;
const DEFAULT_MAX_BUFFERED_BYTES = 128 * 1024;

/**
 * 单连接画面发送与确认。now 是同一时钟的毫秒读数，默认 Date.now；
 * 只计算发送至 ACK 的差值，不与 CDP 的帧时间戳比较。
 */
export class BrowserFrameFlow {
  constructor({
    socket,
    encodeFrame,
    isOpen = () => socket.readyState === 1,
    ackTimeoutMs = DEFAULT_ACK_TIMEOUT_MS,
    bufferRetryMs = DEFAULT_BUFFER_RETRY_MS,
    maxBufferedBytes = DEFAULT_MAX_BUFFERED_BYTES,
    now = () => Date.now(),
    schedule = (callback, delay) => setTimeout(callback, delay),
    cancel = (timer) => clearTimeout(timer),
  }) {
    this.socket = socket;
    this.encodeFrame = encodeFrame;
    this.isOpen = isOpen;
    this.ackTimeoutMs = ackTimeoutMs;
    this.bufferRetryMs = bufferRetryMs;
    this.maxBufferedBytes = maxBufferedBytes;
    this.now = now;
    this.schedule = schedule;
    this.cancel = cancel;
    this.awaitingFrameId = null;
    this.awaitingFrameSentAtClockMs = null;
    this.pendingFrame = null;
    this.ackTimer = null;
    this.bufferRetryTimer = null;
    this.closed = false;
    this.framesSent = 0;
    this.framesSuperseded = 0;
    this.ackTimeouts = 0;
    this.lastAckRttMs = null;
    this.maxAckRttMs = 0;
    this.lastClientDecodeMs = null;
    this.lastClientDrawMs = null;
  }

  offer(frame) {
    if (this.closed || !this.isOpen()) {
      return false;
    }
    if (this.pendingFrame !== null) {
      this.framesSuperseded += 1;
    }
    // 先替换唯一待发帧，再统一发送，避免积压解除时绕过旧待发帧。
    this.pendingFrame = frame;
    this.flushLatest();
    return true;
  }

  acknowledge(frameId, metrics = {}) {
    if (this.closed || frameId !== this.awaitingFrameId) {
      return false;
    }
    this.clearAckTimer();
    const frameSentAtClockMs = this.awaitingFrameSentAtClockMs;
    this.awaitingFrameId = null;
    this.awaitingFrameSentAtClockMs = null;
    if (frameSentAtClockMs !== null) {
      this.lastAckRttMs = Math.max(0, this.now() - frameSentAtClockMs);
      this.maxAckRttMs = Math.max(this.maxAckRttMs, this.lastAckRttMs);
    }
    this.lastClientDecodeMs = finiteNonNegative(metrics.decodeMs);
    this.lastClientDrawMs = finiteNonNegative(metrics.drawMs);
    this.flushLatest();
    return true;
  }

  /**
   * ACK RTT 从 WebSocket.send 返回后计到 ACK 到达，包括传输、客户端处理与排队，
   * 不能当作纯网络 RTT；decode/draw 是客户端 performance.now 差值上报，
   * draw 只计 drawImage 调用，不证明像素已显示，丢弃的帧也可能被确认。
   */
  snapshot() {
    return {
      awaiting_frame_id: this.awaitingFrameId,
      has_pending_frame: this.pendingFrame !== null,
      frames_sent: this.framesSent,
      frames_superseded: this.framesSuperseded,
      ack_timeouts: this.ackTimeouts,
      last_ack_rtt_ms: this.lastAckRttMs,
      max_ack_rtt_ms: this.maxAckRttMs,
      last_client_decode_ms: this.lastClientDecodeMs,
      last_client_draw_ms: this.lastClientDrawMs,
    };
  }

  reset() {
    this.pendingFrame = null;
    this.awaitingFrameId = null;
    this.awaitingFrameSentAtClockMs = null;
    this.clearAckTimer();
    if (this.bufferRetryTimer !== null) {
      this.cancel(this.bufferRetryTimer);
      this.bufferRetryTimer = null;
    }
    this.framesSent = 0;
    this.framesSuperseded = 0;
    this.ackTimeouts = 0;
    this.lastAckRttMs = null;
    this.maxAckRttMs = 0;
    this.lastClientDecodeMs = null;
    this.lastClientDrawMs = null;
  }

  close() {
    this.reset();
    this.closed = true;
  }

  transmit(frame) {
    const encoded = this.encodeFrame(frame);
    this.socket.send(encoded, { binary: true });
    this.framesSent += 1;
    this.awaitingFrameId = frame.frameId;
    this.awaitingFrameSentAtClockMs = this.now();
    this.ackTimer = this.schedule(() => {
      this.ackTimer = null;
      if (this.awaitingFrameId === null || this.closed) {
        return;
      }
      this.ackTimeouts += 1;
      this.awaitingFrameId = null;
      this.awaitingFrameSentAtClockMs = null;
      this.flushLatest();
    }, this.ackTimeoutMs);
    this.ackTimer?.unref?.();
  }

  flushLatest() {
    if (this.closed || this.awaitingFrameId !== null || this.pendingFrame === null) {
      return;
    }
    if (!this.isOpen()) {
      this.pendingFrame = null;
      return;
    }
    if (this.socket.bufferedAmount > this.maxBufferedBytes) {
      this.scheduleBufferRetry();
      return;
    }
    const frame = this.pendingFrame;
    this.pendingFrame = null;
    this.transmit(frame);
  }

  scheduleBufferRetry() {
    if (this.bufferRetryTimer !== null || this.closed) {
      return;
    }
    this.bufferRetryTimer = this.schedule(() => {
      this.bufferRetryTimer = null;
      this.flushLatest();
    }, this.bufferRetryMs);
    this.bufferRetryTimer?.unref?.();
  }

  clearAckTimer() {
    if (this.ackTimer !== null) {
      this.cancel(this.ackTimer);
      this.ackTimer = null;
    }
  }
}

function finiteNonNegative(value) {
  return typeof value === "number" && Number.isFinite(value) && value >= 0
    ? value
    : null;
}
