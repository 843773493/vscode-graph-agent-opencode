import assert from "node:assert/strict";
import test from "node:test";
import { setTimeout as delay } from "node:timers/promises";

import { TerminalOutputMultiplexer } from "./terminalOutputMultiplexer.js";

class FakeClient {
  constructor() {
    this.sent = [];
    this.writable = true;
    this.closed = null;
  }

  sendJson(message) {
    if (!this.writable) {
      return false;
    }
    this.sent.push(message);
    return true;
  }

  sendRaw(message) {
    return this.sendJson(JSON.parse(message));
  }

  close(code, reason) {
    this.closed = { code, reason };
  }
}

async function createHarness(options = {}) {
  let sequence = 0;
  const client = new FakeClient();
  const multiplexer = new TerminalOutputMultiplexer({
    terminalId: "term_test",
    getSequence: () => sequence,
    getSnapshot: () => ({
      terminal_id: "term_test",
      sequence,
      buffer: "",
      display_buffer: "",
    }),
    ...options,
  });
  await multiplexer.attach(client, { afterSequence: 0 });
  client.sent.length = 0;
  return {
    client,
    multiplexer,
    emit(data) {
      sequence += 1;
      multiplexer.recordAndBroadcast({
        type: "output",
        terminalId: "term_test",
        sequence,
        data,
      });
      return sequence;
    },
  };
}

test("未 ACK 字节达到窗口后停止发送，ACK 后继续", async () => {
  const harness = await createHarness({ maxClientUnacknowledgedBytes: 10 });
  const firstSequence = harness.emit("123456");
  harness.emit("abcdef");

  assert.deepEqual(
    harness.client.sent.map((message) => message.data),
    ["123456"],
  );

  harness.multiplexer.acknowledge(harness.client, firstSequence);
  assert.deepEqual(
    harness.client.sent.map((message) => message.data),
    ["123456", "abcdef"],
  );
});

test("socket 暂时不可写时会定时重试，不依赖新的 ACK", async () => {
  const harness = await createHarness({ socketRetryDelayMs: 10 });
  harness.client.writable = false;
  harness.emit("retry-output");
  assert.equal(harness.client.sent.length, 0);

  harness.client.writable = true;
  await delay(30);
  assert.equal(harness.client.sent[0].data, "retry-output");
  harness.multiplexer.dispose();
});

test("重放环出现缺口时发送完整 resync 快照", async () => {
  let sequence = 0;
  const multiplexer = new TerminalOutputMultiplexer({
    terminalId: "term_gap",
    getSequence: () => sequence,
    getSnapshot: () => ({
      terminal_id: "term_gap",
      sequence,
      buffer: "latest-buffer",
      display_buffer: "latest-buffer",
    }),
    maxClientUnacknowledgedBytes: 400 * 1024,
  });
  for (const data of ["a".repeat(300 * 1024), "b".repeat(300 * 1024)]) {
    sequence += 1;
    multiplexer.recordAndBroadcast({
      type: "output",
      terminalId: "term_gap",
      sequence,
      data,
    });
  }

  const client = new FakeClient();
  await multiplexer.attach(client, { afterSequence: 0 });
  assert.equal(client.sent[0].type, "attached");
  assert.equal(client.sent[0].replayMode, "snapshot");
  assert.equal(client.sent[0].snapshot.buffer, "latest-buffer");
  multiplexer.dispose();
});

test("拒绝确认尚未发送的未来 sequence", async () => {
  const harness = await createHarness();
  const sequence = harness.emit("output");

  assert.throws(
    () => harness.multiplexer.acknowledge(harness.client, sequence + 1),
    /ACK sequence 无效/,
  );
  harness.multiplexer.acknowledge(harness.client, sequence);
  harness.multiplexer.dispose();
});

test("拒绝绕过客户端窗口的单个超大输出事件", async () => {
  const harness = await createHarness({ maxClientUnacknowledgedBytes: 8 });

  assert.throws(() => harness.emit("123456789"), /单个终端输出事件超过客户端窗口/);
  harness.multiplexer.dispose();
});

test("socket 重试期间保持控制事件与输出的产生顺序", async () => {
  const harness = await createHarness({ socketRetryDelayMs: 10 });
  harness.client.writable = false;
  harness.multiplexer.broadcast({ type: "status", value: "before-output" });
  harness.emit("result");
  harness.multiplexer.broadcast({ type: "exit" });

  harness.client.writable = true;
  await delay(30);
  assert.deepEqual(
    harness.client.sent.map((message) => message.type),
    ["status", "output", "exit"],
  );
  harness.multiplexer.dispose();
});

test("慢客户端控制事件超过硬上限时被主动断开", async () => {
  const harness = await createHarness({
    maxClientUnacknowledgedBytes: 4,
    maxControlQueueBytes: 32,
  });
  harness.emit("1234");
  harness.emit("5678");

  harness.multiplexer.broadcast({ type: "input", data: "a".repeat(64) });

  assert.equal(harness.multiplexer.clientCount, 0);
  assert.equal(harness.client.closed.code, 1013);
  assert.match(harness.client.closed.reason, /积压超过上限/);
  harness.multiplexer.dispose();
});
