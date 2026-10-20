import assert from "node:assert/strict";
import test from "node:test";
import { setTimeout as delay } from "node:timers/promises";
import { TerminalOutputMultiplexer } from "./terminalOutputMultiplexer.js";
import { encodeTerminalServerMessage } from "../protocol/messages.js";

function createEncodedClient() {
  return {
    writable: true,
    received: [],
    sendJson(message) { return this.sendRaw(encodeTerminalServerMessage(message)); },
    sendRaw(encoded) {
      if (!this.writable) return false;
      this.received.push(JSON.parse(encoded));
      return true;
    },
  };
}

test("编码只在同次广播内复用，独立重放重新编码且不污染业务事件", async (t) => {
  const terminalId = "term_shared_encoding";
  let sequence = 1;
  let serializations = 0;
  const stringify = JSON.stringify;
  t.mock.method(JSON, "stringify", (value, ...args) => {
    if (value?.type === "output" && value.terminalId === terminalId) serializations += 1;
    return stringify(value, ...args);
  });
  const first = Object.freeze({ type: "output", terminalId, sequence: 1, data: "中😀\\\"\u0000", timestamp: "2026-10-19T22:00:00.000Z" });
  encodeTerminalServerMessage(first);
  const serializationsPerEncoding = serializations;
  assert(serializationsPerEncoding > 0);
  serializations = 0;
  const multiplexer = new TerminalOutputMultiplexer({ terminalId, getSequence: () => sequence, getSnapshot: () => ({ buffer: "" }) });
  try {
    multiplexer.recordAndBroadcast(first);
    assert.equal(serializations, 0);
    const clients = [createEncodedClient(), createEncodedClient()];
    for (const client of clients) await multiplexer.attach(client, { afterSequence: 0 });
    assert.equal(serializations, serializationsPerEncoding * 2);
    serializations = 0;

    sequence = 2;
    const second = Object.freeze({ ...first, sequence, data: "下一条正文" });
    multiplexer.recordAndBroadcast(second);

    assert.equal(serializations, serializationsPerEncoding);
    for (const client of clients) assert.deepEqual(client.received.slice(1), [first, second]);
    const reconnecting = createEncodedClient();
    await multiplexer.attach(reconnecting, { afterSequence: 1 });
    assert.deepEqual(reconnecting.received.slice(1), [second]);
    assert.equal(serializations, serializationsPerEncoding * 2);
    assert.equal(multiplexer.readAfter(0).output, first.data + second.data);
    assert.equal(Object.hasOwn(first, "byteCount"), false);
    assert.equal(Object.hasOwn(second, "encodedMessage"), false);
  } finally {
    multiplexer.dispose();
  }
});

test("广播中socket拒收不视为已发送，后续重试仍交付原正文和序号", async () => {
  let sequence = 0;
  const multiplexer = new TerminalOutputMultiplexer({ terminalId: "term_encoding_retry", getSequence: () => sequence,
    getSnapshot: () => ({ buffer: "" }), socketRetryDelayMs: 5 });
  const fast = createEncodedClient();
  const slow = createEncodedClient();
  try {
    for (const client of [fast, slow]) await multiplexer.attach(client, { afterSequence: 0 });
    slow.writable = false;
    sequence = 1;
    const event = { type: "output", terminalId: "term_encoding_retry", sequence, data: "原正文 中😀" };
    multiplexer.recordAndBroadcast(event);

    assert.deepEqual(fast.received.slice(1), [event]);
    assert.deepEqual(slow.received.slice(1), []);
    assert.throws(() => multiplexer.acknowledge(slow, 1), /ACK sequence 无效/);
    slow.writable = true;
    await delay(25);
    assert.deepEqual(slow.received.slice(1), [event]);
    multiplexer.acknowledge(slow, 1);
    assert.equal(multiplexer.clientCount, 2);
  } finally {
    multiplexer.dispose();
  }
});
