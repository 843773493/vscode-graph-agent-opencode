import assert from "node:assert/strict";
import test from "node:test";
import { setTimeout as delay } from "node:timers/promises";

import { TerminalOutputMultiplexer } from "./terminalOutputMultiplexer.js";

test("模型输出游标按 sequence 增量读取且不受 UI ACK 影响", () => {
  let sequence = 0;
  const multiplexer = new TerminalOutputMultiplexer({
    terminalId: "term_model_cursor",
    getSequence: () => sequence,
    getSnapshot: () => ({ buffer: "alpha beta" }),
  });
  sequence = 1;
  multiplexer.recordAndBroadcast({ sequence, data: "alpha " });
  sequence = 2;
  multiplexer.recordAndBroadcast({ sequence, data: "beta" });

  const first = multiplexer.readAfter(0);
  const second = multiplexer.readAfter(first.sequence);

  assert.equal(first.output, "alpha beta");
  assert.equal(first.sequence, 2);
  assert.equal(first.replayMode, "incremental");
  assert.equal(second.output, "");
});

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

test("恢复后的连续输出按原sequence定位，读取与attach不重放已消费前缀", async () => {
  let sequence = 9000;
  const multiplexer = new TerminalOutputMultiplexer({
    terminalId: "term_restored_cursor",
    getSequence: () => sequence,
    getSnapshot: () => ({ buffer: "before-restart" }),
  });
  for (const data of ["A", "B", "C", "D"]) {
    sequence += 1;
    multiplexer.recordAndBroadcast({ type: "output", sequence, data });
  }

  assert.equal(multiplexer.readAfter(9000).output, "ABCD");
  assert.equal(multiplexer.readAfter(9002).output, "CD");
  assert.equal(multiplexer.readAfter(9004).output, "");
  assert.deepEqual(multiplexer.readAfter(8999), {
    output: "before-restart", sequence: 9004,
    replayMode: "snapshot", omittedBeforeSequence: 9001,
  });
  const client = new FakeClient();
  await multiplexer.attach(client, { afterSequence: 9002 });
  assert.equal(client.sent[0].replayMode, "incremental");
  assert.deepEqual(client.sent.slice(1).map((message) => [message.sequence, message.data]), [
    [9003, "C"], [9004, "D"],
  ]);
  multiplexer.dispose();
});

test("淘汰重放环前缀后仍保留最早可重放边界和ACK窗口", async () => {
  let sequence = 0;
  const multiplexer = new TerminalOutputMultiplexer({
    terminalId: "term_retained_suffix",
    getSequence: () => sequence,
    getSnapshot: () => ({ buffer: "latest-buffer" }),
  });
  const chunks = [];
  for (let index = 0; index < 10; index += 1) {
    const data = String.fromCharCode(65 + index).repeat(64 * 1024);
    chunks.push(data);
    sequence += 1;
    multiplexer.recordAndBroadcast({ type: "output", sequence, data });
  }

  assert.deepEqual(multiplexer.readAfter(1), {
    output: "latest-buffer", sequence: 10,
    replayMode: "snapshot", omittedBeforeSequence: 3,
  });
  assert.equal(multiplexer.readAfter(2).output, chunks.slice(2).join(""));
  assert.equal(multiplexer.readAfter(9).output, chunks[9]);
  const client = new FakeClient();
  await multiplexer.attach(client, { afterSequence: 2 });
  assert.equal(client.sent[0].replayMode, "incremental");
  assert.deepEqual(client.sent.slice(1).map((message) => message.sequence), [3, 4, 5, 6]);
  multiplexer.acknowledge(client, 6);
  assert.deepEqual(client.sent.slice(1).map((message) => message.sequence), [3, 4, 5, 6, 7, 8, 9, 10]);
  assert.equal(multiplexer.readAfter(9).output, chunks[9]);
  multiplexer.dispose();
});

test("慢客户端错过重放窗后先resync，确认快照后仅发送新增输出", async () => {
  const harness = await createHarness({
    maxClientUnacknowledgedBytes: 128 * 1024,
    getSnapshot: () => ({ buffer: "最新快照" }),
  });
  const chunk = "a".repeat(64 * 1024);
  for (let index = 0; index < 40; index += 1) harness.emit(chunk);
  assert.deepEqual(harness.client.sent.map((message) => message.sequence), [1, 2]);

  harness.multiplexer.acknowledge(harness.client, 2);
  const resync = harness.client.sent.at(-1);
  assert.equal(resync.type, "resync");
  assert.equal(resync.snapshot.buffer, "最新快照");
  harness.emit("resumed-output");
  assert.equal(harness.client.sent.at(-1), resync);
  harness.multiplexer.acknowledge(harness.client, 40);
  assert.deepEqual(harness.client.sent.map((message) => message.type), [
    "output", "output", "resync", "output",
  ]);
  assert.equal(harness.client.sent.at(-1).sequence, 41);
  assert.equal(harness.client.sent.at(-1).data, "resumed-output");
  harness.multiplexer.dispose();
});

test("连续多轮淘汰前后，读取与重连保留完整UTF-8正文、原序号和ACK边界", async () => {
  const restoredSequence = 9000;
  let sequence = restoredSequence;
  const multiplexer = new TerminalOutputMultiplexer({
    terminalId: "term_many_evictions",
    getSequence: () => sequence,
    getSnapshot: () => ({ buffer: "最新快照" }),
  });
  const timestamp = "2026-10-19T18:00:00.000Z";
  const dataFor = (value) => String(value).padStart(8, "0") + "中".repeat(338) + "ab";
  const checkpoints = new Set([513, 1023, 1024, 1025, 1535, 1536, 1537, 2048]);
  try {
    for (let count = 1; count <= 2048; count += 1) {
      sequence += 1;
      multiplexer.recordAndBroadcast({
        type: "output", terminalId: "term_many_evictions", sequence,
        data: dataFor(sequence), timestamp,
      });
      if (!checkpoints.has(count)) continue;

      // 每条1024字节，512KiB窗口保留最后512条完整事件。
      const beforeRetained = sequence - 512;
      const expectedSequences = Array.from({ length: 512 }, (_, index) => beforeRetained + index + 1);
      const expectedData = expectedSequences.map(dataFor);
      assert.deepEqual(multiplexer.readAfter(beforeRetained - 1), {
        output: "最新快照", sequence, replayMode: "snapshot",
        omittedBeforeSequence: beforeRetained + 1,
      });
      assert.equal(multiplexer.readAfter(beforeRetained).output, expectedData.join(""));
      assert.equal(multiplexer.readAfter(sequence - 2).output, expectedData.slice(-2).join(""));

      const client = new FakeClient();
      await multiplexer.attach(client, { afterSequence: beforeRetained });
      assert.equal(client.sent[0].replayMode, "incremental");
      assert.deepEqual(client.sent.slice(1).map((message) => message.sequence), expectedSequences.slice(0, 256));
      multiplexer.acknowledge(client, beforeRetained + 256);
      assert.deepEqual(client.sent.slice(1).map((message) => message.sequence), expectedSequences);
      assert.deepEqual(client.sent.slice(1).map((message) => message.data), expectedData);
      assert.ok(client.sent.slice(1).every((message) => message.timestamp === timestamp));
      await multiplexer.detach(client);
    }
  } finally {
    multiplexer.dispose();
  }
});

test("多轮淘汰后慢客户端确认resync，再按产生顺序接收控制事件和新增正文", async () => {
  const harness = await createHarness({
    maxClientUnacknowledgedBytes: 128 * 1024,
    getSnapshot: () => ({ buffer: "最新快照" }),
  });
  try {
    for (let index = 0; index < 2048; index += 1) harness.emit("x".repeat(1024));
    assert.deepEqual(
      harness.client.sent.map((message) => message.sequence),
      Array.from({ length: 128 }, (_, index) => index + 1),
    );
    harness.multiplexer.broadcast({ type: "status", value: "before-resumed-output" });
    harness.multiplexer.acknowledge(harness.client, 128);
    const resync = harness.client.sent.at(-1);
    assert.equal(resync.type, "resync");
    assert.equal(resync.snapshot.buffer, "最新快照");
    harness.emit("resumed-output");
    assert.equal(harness.client.sent.at(-1), resync);

    harness.multiplexer.acknowledge(harness.client, 2048);
    assert.deepEqual(harness.client.sent.slice(128), [
      resync,
      { type: "status", value: "before-resumed-output" },
      { type: "output", terminalId: "term_test", sequence: 2049, data: "resumed-output" },
    ]);
  } finally {
    harness.multiplexer.dispose();
  }
});

for (const mode of ["逐条", "累计"]) {
  test(`${mode}ACK跨多轮窗口后释放准确字节，重复及旧确认不重复发送`, async () => {
    const harness = await createHarness({ maxClientUnacknowledgedBytes: 32 });
    try {
      for (let index = 1; index <= 12; index += 1) harness.emit(String(index).padStart(8, "0"));
      assert.equal(harness.client.sent.length, 4);
      const increment = mode === "逐条" ? 1 : 4;
      for (let sequence = increment; sequence <= 12; sequence += increment) {
        harness.multiplexer.acknowledge(harness.client, sequence);
        const count = Math.min(12, sequence + 4);
        assert.deepEqual(harness.client.sent.map((message) => message.sequence),
          Array.from({ length: count }, (_, index) => index + 1));
        assert.deepEqual(harness.client.sent.map((message) => message.data),
          Array.from({ length: count }, (_, index) => String(index + 1).padStart(8, "0")));
        harness.multiplexer.acknowledge(harness.client, sequence);
        harness.multiplexer.acknowledge(harness.client, 0);
        assert.equal(harness.client.sent.length, count);
      }
      harness.emit("next-one");
      assert.equal(harness.client.sent.at(-1).sequence, 13);
      assert.equal(harness.client.sent.at(-1).data, "next-one");
    } finally {
      harness.multiplexer.dispose();
    }
  });
}

test("部分ACK后发生重同步，确认快照能完整释放新窗口而不沿用旧队列位置", async () => {
  const snapshotData = "中".repeat(20000);
  const harness = await createHarness({
    maxClientUnacknowledgedBytes: 128 * 1024,
    getSnapshot: () => ({ buffer: snapshotData }),
  });
  try {
    for (let index = 0; index < 2048; index += 1) harness.emit("x".repeat(1024));
    assert.equal(harness.client.sent.length, 128);
    // 只确认第一条，队列尚有127条；重放缺口随即用当前快照替换它们。
    harness.multiplexer.acknowledge(harness.client, 1);
    const resync = harness.client.sent.at(-1);
    assert.equal(resync.type, "resync");
    assert.equal(resync.snapshot.buffer, snapshotData);
    harness.emit("a".repeat(64 * 1024));
    harness.emit("b".repeat(64 * 1024));
    assert.equal(harness.client.sent.at(-1), resync);
    harness.multiplexer.acknowledge(harness.client, 2048);
    assert.deepEqual(harness.client.sent.slice(129).map((message) => [message.sequence, message.data]), [
      [2049, "a".repeat(64 * 1024)], [2050, "b".repeat(64 * 1024)],
    ]);
  } finally {
    harness.multiplexer.dispose();
  }
});
