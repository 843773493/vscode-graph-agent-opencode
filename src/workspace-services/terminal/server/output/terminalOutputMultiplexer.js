import { encodeTerminalServerMessage } from "../../protocol/messages.js";

const MAX_REPLAY_BYTES = 512 * 1024;
const MAX_CLIENT_UNACKNOWLEDGED_BYTES = 256 * 1024;
const MAX_CONTROL_QUEUE_BYTES = 256 * 1024;
const SOCKET_RETRY_DELAY_MS = 50;

/**
 * 单个attach连接的输出交付状态，不是Terminal生命周期或模型读取游标。
 * @typedef {object} TerminalClientFlowState
 * @property {number} acknowledgedSequence 连接的累计确认游标，接入时以重放起点或当前快照序号初始化；不表示屏幕已绘制或模型已读取。
 * @property {number} sentSequence 已交给连接的输出序号，含attached/resync快照代表的消费基线。
 * @property {Array<{sequence: number, byteCount: number}|null>} inFlightEvents 未确认计费记录只含序号和UTF-8字节数，已确认前缀置null，不保存正文。
 * @property {number} inFlightEventStartIndex 未确认记录在数组中的逻辑起点。
 * @property {number} unacknowledgedBytes 未确认正文的UTF-8字节总数，快照同样计费。
 * @property {boolean} attachedMessageSent attached消息是否已交给连接，不代表客户端已确认。
 * @property {"ack"|"socket"|null} blockedOn 当前停止发送的原因；等待ACK与等待socket可写分别恢复。
 * @property {ReturnType<typeof setTimeout>|null} socketRetryTimer socket拒收后的重试句柄，不是ACK超时。
 * @property {Array<{afterSequence: number, message: object, byteCount: number}|null>} controlQueue 按输出序号屏障排队的控制消息，已发送前缀置null。
 * @property {number} controlQueueStartIndex 未发送控制消息在数组中的逻辑起点。
 * @property {number} controlQueueBytes 控制队列按JSON正文计费的UTF-8字节数。
 */

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
    /** @type {Map<object, TerminalClientFlowState>} */
    this.clientFlowStates = new Map();
    // Session逐一递增sequence；淘汰只推进头游标，避免逐事件搬移整个数组。
    this.outputEvents = [];
    this.outputEventStartIndex = 0;
    this.outputEventBytes = 0;
    this.maxClientUnacknowledgedBytes = maxClientUnacknowledgedBytes;
    this.maxControlQueueBytes = maxControlQueueBytes;
    this.socketRetryDelayMs = socketRetryDelayMs;
  }

  get clientCount() {
    return this.clientFlowStates.size;
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
    /** @type {TerminalClientFlowState} */
    const clientFlow = {
      acknowledgedSequence: 0,
      sentSequence: 0,
      inFlightEvents: [],
      inFlightEventStartIndex: 0,
      unacknowledgedBytes: 0,
      attachedMessageSent: false,
      blockedOn: null,
      socketRetryTimer: null,
      controlQueue: [],
      controlQueueStartIndex: 0,
      controlQueueBytes: 0,
    };
    this.clientFlowStates.set(client, clientFlow);
    try {
      if (beforeNotify) {
        await beforeNotify();
      }
    } catch (error) {
      this.clientFlowStates.delete(client);
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
    clientFlow.acknowledgedSequence = canReplay ? requestedSequence : currentSequence;
    clientFlow.sentSequence = canReplay ? requestedSequence : currentSequence;
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
      this.clientFlowStates.delete(client);
      throw new Error(`发送终端 attached 消息失败: terminal_id=${this.terminalId}`);
    }
    clientFlow.attachedMessageSent = true;
    if (!canReplay && requestedSequence !== null) {
      clientFlow.inFlightEvents.push({
        sequence: currentSequence,
        byteCount: Buffer.byteLength(snapshot.buffer || "", "utf8"),
      });
      clientFlow.unacknowledgedBytes = clientFlow.inFlightEvents[0].byteCount;
      clientFlow.blockedOn = "ack";
    }
    if (clientFlow.blockedOn === null) {
      this.flushClientReplay(client, clientFlow);
    }
  }

  async detach(client, { beforeNotify = null } = {}) {
    const clientFlow = this.clientFlowStates.get(client);
    if (clientFlow && clientFlow.socketRetryTimer !== null) {
      clearTimeout(clientFlow.socketRetryTimer);
    }
    this.clientFlowStates.delete(client);
    if (beforeNotify) {
      await beforeNotify();
    }
    client.sendJson({
      type: "detached",
      terminalId: this.terminalId,
    });
  }

  acknowledge(client, sequence) {
    const clientFlow = this.clientFlowStates.get(client);
    if (!clientFlow) {
      throw new Error(`客户端尚未 attach 到终端: terminal_id=${this.terminalId}`);
    }
    if (!Number.isInteger(sequence) || sequence < 0 || sequence > clientFlow.sentSequence) {
      throw new Error(
        `终端 ACK sequence 无效: terminal_id=${this.terminalId}, sequence=${sequence}, sent_sequence=${clientFlow.sentSequence}`,
      );
    }
    clientFlow.acknowledgedSequence = Math.max(clientFlow.acknowledgedSequence, sequence);
    while (
      clientFlow.inFlightEventStartIndex < clientFlow.inFlightEvents.length
      && clientFlow.inFlightEvents[clientFlow.inFlightEventStartIndex].sequence <= clientFlow.acknowledgedSequence
    ) {
      const acknowledged = clientFlow.inFlightEvents[clientFlow.inFlightEventStartIndex];
      clientFlow.inFlightEvents[clientFlow.inFlightEventStartIndex] = null;
      clientFlow.inFlightEventStartIndex += 1;
      clientFlow.unacknowledgedBytes = Math.max(
        clientFlow.unacknowledgedBytes - acknowledged.byteCount,
        0,
      );
    }
    // ACK可能逐条到达；和重放缓冲一样，集中压缩以避免反复搬移未确认事件。
    if (
      clientFlow.inFlightEventStartIndex > 0
      && clientFlow.inFlightEventStartIndex * 2 >= clientFlow.inFlightEvents.length
    ) {
      clientFlow.inFlightEvents = clientFlow.inFlightEvents.slice(clientFlow.inFlightEventStartIndex);
      clientFlow.inFlightEventStartIndex = 0;
    }
    clientFlow.blockedOn = null;
    this.flushClientReplay(client, clientFlow);
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
    const encodedOutputs = this.clientFlowStates.size > 1 ? new Map() : null;
    for (const [client, clientFlow] of this.clientFlowStates) {
      if (!clientFlow.attachedMessageSent || clientFlow.blockedOn !== null) {
        continue;
      }
      this.flushClientReplay(client, clientFlow, encodedOutputs);
    }
  }

  broadcast(message) {
    if (this.clientFlowStates.size === 0) {
      return;
    }
    const afterSequence = this.getSequence();
    const byteCount = Buffer.byteLength(JSON.stringify(message), "utf8");
    for (const [client, clientFlow] of this.clientFlowStates) {
      if (clientFlow.controlQueueBytes + byteCount > this.maxControlQueueBytes) {
        this.disconnectSlowClient(client, clientFlow);
        continue;
      }
      clientFlow.controlQueue.push({ afterSequence, message, byteCount });
      clientFlow.controlQueueBytes += byteCount;
      if (clientFlow.attachedMessageSent && clientFlow.blockedOn === null) {
        this.flushClientReplay(client, clientFlow);
      }
    }
  }

  flushClientReplay(client, clientFlow, encodedOutputs = null) {
    const currentSequence = this.getSequence();
    const earliestSequence = this.outputEvents[this.outputEventStartIndex]?.sequence
      ?? currentSequence + 1;
    if (clientFlow.sentSequence < earliestSequence - 1) {
      const snapshot = this.getSnapshot();
      const sent = client.sendJson({
        type: "resync",
        terminalId: this.terminalId,
        snapshot,
      });
      if (!sent) {
        this.scheduleSocketRetry(client, clientFlow);
        return;
      }
      clientFlow.sentSequence = currentSequence;
      clientFlow.inFlightEvents = [{
        sequence: currentSequence,
        byteCount: Buffer.byteLength(snapshot.buffer || "", "utf8"),
      }];
      clientFlow.inFlightEventStartIndex = 0;
      clientFlow.unacknowledgedBytes = clientFlow.inFlightEvents[0].byteCount;
      clientFlow.blockedOn = "ack";
      return;
    }
    for (
      let index = this.outputEventStartIndex + clientFlow.sentSequence - earliestSequence + 1;
      index < this.outputEvents.length;
      index += 1
    ) {
      const event = this.outputEvents[index];
      if (!this.flushControlsThrough(client, clientFlow, event.sequence - 1)) {
        return;
      }
      if (
        clientFlow.inFlightEventStartIndex < clientFlow.inFlightEvents.length
        && clientFlow.unacknowledgedBytes + event.byteCount
          > this.maxClientUnacknowledgedBytes
      ) {
        clientFlow.blockedOn = "ack";
        return;
      }
      let encoded = encodedOutputs?.get(event);
      if (encoded === undefined) {
        const { byteCount: _byteCount, ...message } = event;
        encoded = encodeTerminalServerMessage(message);
        encodedOutputs?.set(event, encoded);
      }
      if (!client.sendRaw(encoded)) {
        this.scheduleSocketRetry(client, clientFlow);
        return;
      }
      clientFlow.sentSequence = event.sequence;
      clientFlow.inFlightEvents.push({
        sequence: event.sequence,
        byteCount: event.byteCount,
      });
      clientFlow.unacknowledgedBytes += event.byteCount;
    }
    if (!this.flushControlsThrough(client, clientFlow, currentSequence)) {
      return;
    }
    clientFlow.blockedOn = null;
  }

  flushControlsThrough(client, clientFlow, sequence) {
    while (
      clientFlow.controlQueueStartIndex < clientFlow.controlQueue.length
      && clientFlow.controlQueue[clientFlow.controlQueueStartIndex].afterSequence <= sequence
    ) {
      const controlMessage = clientFlow.controlQueue[clientFlow.controlQueueStartIndex];
      if (!client.sendJson(controlMessage.message)) {
        this.scheduleSocketRetry(client, clientFlow);
        return false;
      }
      clientFlow.controlQueue[clientFlow.controlQueueStartIndex] = null;
      clientFlow.controlQueueStartIndex += 1;
      clientFlow.controlQueueBytes = Math.max(
        clientFlow.controlQueueBytes - controlMessage.byteCount,
        0,
      );
    }
    if (
      clientFlow.controlQueueStartIndex > 0
      && clientFlow.controlQueueStartIndex * 2 >= clientFlow.controlQueue.length
    ) {
      clientFlow.controlQueue = clientFlow.controlQueue.slice(clientFlow.controlQueueStartIndex);
      clientFlow.controlQueueStartIndex = 0;
    }
    return true;
  }

  scheduleSocketRetry(client, clientFlow) {
    clientFlow.blockedOn = "socket";
    if (clientFlow.socketRetryTimer !== null) {
      return;
    }
    clientFlow.socketRetryTimer = setTimeout(() => {
      clientFlow.socketRetryTimer = null;
      if (this.clientFlowStates.get(client) !== clientFlow) {
        return;
      }
      clientFlow.blockedOn = null;
      this.flushClientReplay(client, clientFlow);
    }, this.socketRetryDelayMs);
  }

  disconnectSlowClient(client, clientFlow) {
    if (clientFlow.socketRetryTimer !== null) {
      clearTimeout(clientFlow.socketRetryTimer);
    }
    this.clientFlowStates.delete(client);
    client.close(
      1013,
      `终端控制事件积压超过上限: terminal_id=${this.terminalId}, bytes=${clientFlow.controlQueueBytes}`,
    );
  }

  dispose() {
    for (const clientFlow of this.clientFlowStates.values()) {
      if (clientFlow.socketRetryTimer !== null) {
        clearTimeout(clientFlow.socketRetryTimer);
      }
    }
    this.clientFlowStates.clear();
  }
}
