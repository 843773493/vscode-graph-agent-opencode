import { encodeTerminalServerMessage } from "../protocol/messages.js";

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
    // Session逐一递增sequence；淘汰只推进头游标，避免逐事件搬移整个数组。
    this.outputEvents = [];
    this.outputEventStartIndex = 0;
    this.outputEventBytes = 0;
    this.maxClientUnacknowledgedBytes = maxClientUnacknowledgedBytes;
    this.maxControlQueueBytes = maxControlQueueBytes;
    this.socketRetryDelayMs = socketRetryDelayMs;
  }

  get clientCount() {
    return this.clients.size;
  }

  readAfter(afterSequence) {
    const currentSequence = this.getSequence();
    if (!Number.isInteger(afterSequence) || afterSequence < 0) {
      throw new Error(
        `终端输出游标无效: terminal_id=${this.terminalId}, sequence=${afterSequence}`,
      );
    }
    if (afterSequence > currentSequence) {
      throw new Error(
        `终端输出游标超过最新输出: terminal_id=${this.terminalId}, sequence=${afterSequence}, current_sequence=${currentSequence}`,
      );
    }
    if (afterSequence === currentSequence) {
      return {
        output: "",
        sequence: currentSequence,
        replayMode: "incremental",
        omittedBeforeSequence: null,
      };
    }

    const earliestSequence = this.outputEvents[this.outputEventStartIndex]?.sequence
      ?? currentSequence + 1;
    const canReplay = afterSequence >= earliestSequence - 1;
    if (canReplay) {
      // 从已定位的后缀直接拼接，不再复制事件数组并分配正文数组。
      let output = "";
      for (let index = this.outputEventStartIndex + afterSequence - earliestSequence + 1;
        index < this.outputEvents.length; index += 1) {
        output += this.outputEvents[index].data;
      }
      return {
        output,
        sequence: currentSequence,
        replayMode: "incremental",
        omittedBeforeSequence: null,
      };
    }

    const snapshot = this.getSnapshot();
    return {
      output: snapshot.buffer || "",
      sequence: currentSequence,
      replayMode: "snapshot",
      omittedBeforeSequence: earliestSequence,
    };
  }

  async attach(client, { afterSequence = null, beforeNotify = null } = {}) {
    const state = {
      acknowledgedSequence: 0,
      sentSequence: 0,
      inFlightEvents: [],
      inFlightEventStartIndex: 0,
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
    const earliestSequence = this.outputEvents[this.outputEventStartIndex]?.sequence
      ?? currentSequence + 1;
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
    if (state && state.retryTimer !== null) {
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
      state.inFlightEventStartIndex < state.inFlightEvents.length
      && state.inFlightEvents[state.inFlightEventStartIndex].sequence <= state.acknowledgedSequence
    ) {
      const acknowledged = state.inFlightEvents[state.inFlightEventStartIndex];
      state.inFlightEvents[state.inFlightEventStartIndex] = null;
      state.inFlightEventStartIndex += 1;
      state.unacknowledgedBytes = Math.max(
        state.unacknowledgedBytes - acknowledged.byteCount,
        0,
      );
    }
    // ACK可能逐条到达；和重放缓冲一样，集中压缩以避免反复搬移未确认事件。
    if (
      state.inFlightEventStartIndex > 0
      && state.inFlightEventStartIndex * 2 >= state.inFlightEvents.length
    ) {
      state.inFlightEvents = state.inFlightEvents.slice(state.inFlightEventStartIndex);
      state.inFlightEventStartIndex = 0;
    }
    state.blocked = null;
    this.flushClientReplay(client, state);
  }

  /**
   * timestamp 是 Session 收到本次 PTY 输出消息的 UTC ISO 时间，同次切块共享该值。
   * 重放保留原时间；输出顺序以 sequence 为准，不按墙上时钟排序。
   * @param {{type: "output", terminalId: string, sequence: number, data: string, timestamp: string}} event
   */
  recordAndBroadcast(event) {
    const byteCount = Buffer.byteLength(event.data, "utf8");
    if (byteCount > this.maxClientUnacknowledgedBytes) {
      throw new Error(
        `单个终端输出事件超过客户端窗口: terminal_id=${this.terminalId}, bytes=${byteCount}, window=${this.maxClientUnacknowledgedBytes}`,
      );
    }
    this.outputEvents.push({ ...event, byteCount });
    this.outputEventBytes += byteCount;
    while (
      this.outputEventBytes > MAX_REPLAY_BYTES
      && this.outputEvents.length - this.outputEventStartIndex > 1
    ) {
      const removed = this.outputEvents[this.outputEventStartIndex];
      this.outputEvents[this.outputEventStartIndex] = null;
      this.outputEventStartIndex += 1;
      this.outputEventBytes -= removed.byteCount;
    }
    // 立即释放淘汰正文；空前缀占到一半时再批量压缩，均摊搬移成本。
    if (this.outputEventStartIndex * 2 >= this.outputEvents.length) {
      this.outputEvents = this.outputEvents.slice(this.outputEventStartIndex);
      this.outputEventStartIndex = 0;
    }
    // 只在本次同步广播内共享封包，不把编码正文留在重放缓冲中。
    const encodedOutputs = this.clients.size > 1 ? new Map() : null;
    for (const [client, state] of this.clients) {
      if (!state.ready || state.blocked !== null) {
        continue;
      }
      this.flushClientReplay(client, state, encodedOutputs);
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

  flushClientReplay(client, state, encodedOutputs = null) {
    const currentSequence = this.getSequence();
    const earliestSequence = this.outputEvents[this.outputEventStartIndex]?.sequence
      ?? currentSequence + 1;
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
      state.inFlightEventStartIndex = 0;
      state.unacknowledgedBytes = state.inFlightEvents[0].byteCount;
      state.blocked = "ack";
      return;
    }
    for (
      let index = this.outputEventStartIndex + state.sentSequence - earliestSequence + 1;
      index < this.outputEvents.length;
      index += 1
    ) {
      const event = this.outputEvents[index];
      if (!this.flushControlsThrough(client, state, event.sequence - 1)) {
        return;
      }
      if (
        state.inFlightEventStartIndex < state.inFlightEvents.length
        && state.unacknowledgedBytes + event.byteCount
          > this.maxClientUnacknowledgedBytes
      ) {
        state.blocked = "ack";
        return;
      }
      let encoded = encodedOutputs?.get(event);
      if (encoded === undefined) {
        const { byteCount: _byteCount, ...message } = event;
        encoded = encodeTerminalServerMessage(message);
        encodedOutputs?.set(event, encoded);
      }
      if (!client.sendRaw(encoded)) {
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
