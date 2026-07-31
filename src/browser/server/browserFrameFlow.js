const DEFAULT_ACK_TIMEOUT_MS = 1_500;
const DEFAULT_BUFFER_RETRY_MS = 8;
const DEFAULT_MAX_BUFFERED_BYTES = 128 * 1024;

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
    this.awaitingSentAtMs = null;
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
    if (this.awaitingFrameId !== null || this.socket.bufferedAmount > this.maxBufferedBytes) {
      if (this.pendingFrame !== null) {
        this.framesSuperseded += 1;
      }
      this.pendingFrame = frame;
      if (this.awaitingFrameId === null) {
        this.scheduleBufferRetry();
      }
      return true;
    }
    this.transmit(frame);
    return true;
  }

  acknowledge(frameId, metrics = {}) {
    if (this.closed || frameId !== this.awaitingFrameId) {
      return false;
    }
    this.clearAckTimer();
    const sentAtMs = this.awaitingSentAtMs;
    this.awaitingFrameId = null;
    this.awaitingSentAtMs = null;
    if (sentAtMs !== null) {
      this.lastAckRttMs = Math.max(0, this.now() - sentAtMs);
      this.maxAckRttMs = Math.max(this.maxAckRttMs, this.lastAckRttMs);
    }
    this.lastClientDecodeMs = finiteNonNegative(metrics.decodeMs);
    this.lastClientDrawMs = finiteNonNegative(metrics.drawMs);
    this.flushLatest();
    return true;
  }

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
    this.awaitingSentAtMs = null;
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
    this.awaitingSentAtMs = this.now();
    this.ackTimer = this.schedule(() => {
      this.ackTimer = null;
      if (this.awaitingFrameId === null || this.closed) {
        return;
      }
      this.ackTimeouts += 1;
      this.awaitingFrameId = null;
      this.awaitingSentAtMs = null;
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
