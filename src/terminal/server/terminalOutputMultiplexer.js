const MAX_REPLAY_BYTES = 512 * 1024;
const MAX_CLIENT_UNACKNOWLEDGED_BYTES = 256 * 1024;
const MAX_CONTROL_QUEUE_BYTES = 256 * 1024;
const SOCKET_RETRY_DELAY_MS = 50;

export class TerminalOutputMultiplexer {
  constructor({
    terminalId,
    getSequence,
    getSnapshot,
    maxClientUnacknowledgedBytes = MAX_CLIENT_UNACKNOWLEDGED_BYTES,
    maxControlQueueBytes = MAX_CONTROL_QUEUE_BYTES,
    socketRetryDelayMs = SOCKET_RETRY_DELAY_MS,
  }) {
    this.terminalId = terminalId;
    this.getSequence = getSequence;
    this.getSnapshot = getSnapshot;
    this.clients = new Map();
    this.outputEvents = [];
    this.outputEventBytes = 0;
    this.maxClientUnacknowledgedBytes = maxClientUnacknowledgedBytes;
    this.maxControlQueueBytes = maxControlQueueBytes;
    this.socketRetryDelayMs = socketRetryDelayMs;
  }

  get clientCount() {
    return this.clients.size;
  }

  async attach(client, { afterSequence = null, beforeNotify = null } = {}) {
    const state = {
      acknowledgedSequence: 0,
      sentSequence: 0,
      inFlightEvents: [],
      unacknowledgedBytes: 0,
      ready: false,
      blocked: null,
      retryTimer: null,
      controlQueue: [],
      controlQueueBytes: 0,
    };
    this.clients.set(client, state);
    try {
      if (beforeNotify) {
        await beforeNotify();
      }
    } catch (error) {
      this.clients.delete(client);
      throw error;
    }
    const currentSequence = this.getSequence();
    const requestedSequence = Number.isInteger(afterSequence) && afterSequence >= 0
      ? afterSequence
      : null;
    const earliestSequence = this.outputEvents[0]?.sequence ?? currentSequence + 1;
    const canReplay = requestedSequence !== null
      && requestedSequence <= currentSequence
      && requestedSequence >= earliestSequence - 1;
    state.acknowledgedSequence = canReplay ? requestedSequence : currentSequence;
    state.sentSequence = canReplay ? requestedSequence : currentSequence;
    const snapshot = this.getSnapshot();
    const attachedSnapshot = canReplay
      ? { ...snapshot, buffer: "", display_buffer: "" }
      : snapshot;
    if (!client.sendJson({
      type: "attached",
      terminalId: this.terminalId,
      snapshot: attachedSnapshot,
      replayMode: canReplay ? "incremental" : "snapshot",
      resumeFromSequence: canReplay ? requestedSequence : currentSequence,
    })) {
      this.clients.delete(client);
      throw new Error(`发送终端 attached 消息失败: terminal_id=${this.terminalId}`);
    }
    state.ready = true;
    if (!canReplay && requestedSequence !== null) {
      state.inFlightEvents.push({
        sequence: currentSequence,
        byteCount: Buffer.byteLength(snapshot.buffer || "", "utf8"),
      });
      state.unacknowledgedBytes = state.inFlightEvents[0].byteCount;
      state.blocked = "ack";
    }
    if (state.blocked === null) {
      this.flushClientReplay(client, state);
    }
  }

  async detach(client, { beforeNotify = null } = {}) {
    const state = this.clients.get(client);
    if (state?.retryTimer !== null) {
      clearTimeout(state.retryTimer);
    }
    this.clients.delete(client);
    if (beforeNotify) {
      await beforeNotify();
    }
    client.sendJson({
      type: "detached",
      terminalId: this.terminalId,
    });
  }

  acknowledge(client, sequence) {
    const state = this.clients.get(client);
    if (!state) {
      throw new Error(`客户端尚未 attach 到终端: terminal_id=${this.terminalId}`);
    }
    if (!Number.isInteger(sequence) || sequence < 0 || sequence > state.sentSequence) {
      throw new Error(
        `终端 ACK sequence 无效: terminal_id=${this.terminalId}, sequence=${sequence}, sent_sequence=${state.sentSequence}`,
      );
    }
    state.acknowledgedSequence = Math.max(state.acknowledgedSequence, sequence);
    while (
      state.inFlightEvents.length > 0
      && state.inFlightEvents[0].sequence <= state.acknowledgedSequence
    ) {
      const acknowledged = state.inFlightEvents.shift();
      state.unacknowledgedBytes = Math.max(
        state.unacknowledgedBytes - acknowledged.byteCount,
        0,
      );
    }
    state.blocked = null;
    this.flushClientReplay(client, state);
  }

  recordAndBroadcast(event) {
    const byteCount = Buffer.byteLength(event.data, "utf8");
    if (byteCount > this.maxClientUnacknowledgedBytes) {
      throw new Error(
        `单个终端输出事件超过客户端窗口: terminal_id=${this.terminalId}, bytes=${byteCount}, window=${this.maxClientUnacknowledgedBytes}`,
      );
    }
    this.outputEvents.push({ ...event, byteCount });
    this.outputEventBytes += byteCount;
    while (this.outputEventBytes > MAX_REPLAY_BYTES && this.outputEvents.length > 1) {
      const removed = this.outputEvents.shift();
      this.outputEventBytes -= removed.byteCount;
    }
    for (const [client, state] of this.clients) {
      if (!state.ready || state.blocked !== null) {
        continue;
      }
      this.flushClientReplay(client, state);
    }
  }

  broadcast(message) {
    const afterSequence = this.getSequence();
    const byteCount = Buffer.byteLength(JSON.stringify(message), "utf8");
    for (const [client, state] of this.clients) {
      if (state.controlQueueBytes + byteCount > this.maxControlQueueBytes) {
        this.disconnectSlowClient(client, state);
        continue;
      }
      state.controlQueue.push({ afterSequence, message, byteCount });
      state.controlQueueBytes += byteCount;
      if (state.ready && state.blocked === null) {
        this.flushClientReplay(client, state);
      }
    }
  }

  flushClientReplay(client, state) {
    const currentSequence = this.getSequence();
    const earliestSequence = this.outputEvents[0]?.sequence ?? currentSequence + 1;
    if (state.sentSequence < earliestSequence - 1) {
      const snapshot = this.getSnapshot();
      const sent = client.sendJson({
        type: "resync",
        terminalId: this.terminalId,
        snapshot,
      });
      if (!sent) {
        this.scheduleSocketRetry(client, state);
        return;
      }
      state.sentSequence = currentSequence;
      state.inFlightEvents = [{
        sequence: currentSequence,
        byteCount: Buffer.byteLength(snapshot.buffer || "", "utf8"),
      }];
      state.unacknowledgedBytes = state.inFlightEvents[0].byteCount;
      state.blocked = "ack";
      return;
    }
    for (const event of this.outputEvents) {
      if (event.sequence <= state.sentSequence) {
        continue;
      }
      if (!this.flushControlsThrough(client, state, event.sequence - 1)) {
        return;
      }
      if (
        state.inFlightEvents.length > 0
        && state.unacknowledgedBytes + event.byteCount
          > this.maxClientUnacknowledgedBytes
      ) {
        state.blocked = "ack";
        return;
      }
      const { byteCount: _byteCount, ...message } = event;
      if (!client.sendJson(message)) {
        this.scheduleSocketRetry(client, state);
        return;
      }
      state.sentSequence = event.sequence;
      state.inFlightEvents.push({
        sequence: event.sequence,
        byteCount: event.byteCount,
      });
      state.unacknowledgedBytes += event.byteCount;
    }
    if (!this.flushControlsThrough(client, state, currentSequence)) {
      return;
    }
    state.blocked = null;
  }

  flushControlsThrough(client, state, sequence) {
    while (
      state.controlQueue.length > 0
      && state.controlQueue[0].afterSequence <= sequence
    ) {
      if (!client.sendJson(state.controlQueue[0].message)) {
        this.scheduleSocketRetry(client, state);
        return false;
      }
      const sent = state.controlQueue.shift();
      state.controlQueueBytes = Math.max(state.controlQueueBytes - sent.byteCount, 0);
    }
    return true;
  }

  scheduleSocketRetry(client, state) {
    state.blocked = "socket";
    if (state.retryTimer !== null) {
      return;
    }
    state.retryTimer = setTimeout(() => {
      state.retryTimer = null;
      if (this.clients.get(client) !== state) {
        return;
      }
      state.blocked = null;
      this.flushClientReplay(client, state);
    }, this.socketRetryDelayMs);
  }

  disconnectSlowClient(client, state) {
    if (state.retryTimer !== null) {
      clearTimeout(state.retryTimer);
    }
    this.clients.delete(client);
    client.close(
      1013,
      `终端控制事件积压超过上限: terminal_id=${this.terminalId}, bytes=${state.controlQueueBytes}`,
    );
  }

  dispose() {
    for (const state of this.clients.values()) {
      if (state.retryTimer !== null) {
        clearTimeout(state.retryTimer);
      }
    }
    this.clients.clear();
  }
}
