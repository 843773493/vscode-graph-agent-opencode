import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

// 浏览器的 protocol 挂载由 Gateway 提供；Node 检查沿同一源码加载真实编码器。
const source = await readFile(new URL("./terminalConnection.js", import.meta.url), "utf8");
const moduleSource = source.replace('"./protocol/messages.js"',
  JSON.stringify(new URL("../protocol/messages.js", import.meta.url).href));
const { TerminalConnection } = await import(`data:text/javascript;base64,${Buffer.from(moduleSource).toString("base64")}`);

function setup(t, options = {}) {
  const sockets = [];
  const timers = new Map();
  const statuses = [];
  const sequences = new Map();
  let timerId = 0;
  let grants = 0;
  let storageReads = 0;
  let visible = true;
  let revoked = false;
  class FakeSocket extends EventTarget {
    static OPEN = 1;
    static CLOSED = 3;
    readyState = 0;
    sent = [];
    constructor(url) { super(); this.url = url; sockets.push(this); }
    emit(type, fields = {}) { this.dispatchEvent(Object.assign(new Event(type), fields)); }
    open() { this.readyState = 1; this.emit("open"); }
    message(message) { this.emit("message", { data: JSON.stringify(message) }); }
    send(value) { this.sent.push(JSON.parse(value)); }
    close(code = 1000) { this.readyState = 3; this.emit("close", { code }); }
  }
  const originalWindow = Object.getOwnPropertyDescriptor(globalThis, "window");
  const originalSocket = Object.getOwnPropertyDescriptor(globalThis, "WebSocket");
  Object.defineProperty(globalThis, "window", { configurable: true, value: {
    sessionStorage: { getItem(key) { storageReads += 1; return sequences.get(key); },
      setItem: (key, value) => sequences.set(key, value) },
    setTimeout(callback, delay) { timers.set(++timerId, { callback, delay }); return timerId; },
    clearTimeout(id) { timers.delete(id); },
  } });
  Object.defineProperty(globalThis, "WebSocket", { configurable: true, value: FakeSocket });
  t.after(() => {
    if (originalWindow) Object.defineProperty(globalThis, "window", originalWindow);
    else delete globalThis.window;
    Object.defineProperty(globalThis, "WebSocket", originalSocket);
  });
  const connection = new TerminalConnection({
    terminalId: "term_test",
    requestGrant: async () => ({ grant_token: `grant-${++grants}`, service: "terminal" }),
    socketUrl: () => "ws://127.0.0.1/terminal",
    isVisible: () => visible,
    canReconnect: () => true,
    getSize: () => ({ cols: 80, rows: 24 }),
    onMessage() {}, onMode() {},
    onStatus: (...status) => statuses.push(status),
    onRevoked() { revoked = true; },
    ...options,
  });
  return { connection, sockets, timers, statuses, sequences,
    get storageReads() { return storageReads; },
    get revoked() { return revoked; },
    setVisible(value) { visible = value; },
    async reconnect() {
      assert.equal(timers.size, 1);
      const [id, timer] = timers.entries().next().value;
      timers.delete(id);
      timer.callback();
      await Promise.resolve();
    },
  };
}

test("授权等待合并；隐藏并断开后不建立迟到连接，恢复取得新授权", async t => {
  let deliver;
  let grants = 0;
  const state = setup(t, { requestGrant: () => {
    grants += 1;
    return new Promise(resolve => { deliver = resolve; });
  } });
  const pending = state.connection.attach();
  await state.connection.attach();
  assert.equal(grants, 1);
  state.setVisible(false);
  state.connection.detach({ preserveIntent: true });
  deliver({ grant_token: "old", service: "terminal" });
  await pending;
  assert.equal(state.sockets.length, 0);
  assert.equal(state.connection.desiredAttached, true);
  state.setVisible(true);
  const resumed = state.connection.attach();
  assert.equal(grants, 2);
  deliver({ grant_token: "new", service: "terminal" });
  await resumed;
  state.sockets[0].open();
  assert.equal(state.sockets[0].sent[0].grantToken, "new");
});

test("连接恢复沿原ACK游标取得新授权，相同尺寸只发送一次，旧socket事件无效", async t => {
  const state = setup(t);
  const { connection, sockets, timers } = state;
  await connection.attach();
  const first = sockets[0];
  first.open();
  first.message({ type: "attached" });
  connection.acknowledge(12);
  connection.resize(80, 24);
  connection.resize(90, 30);
  connection.resize(90, 30);
  assert.deepEqual(first.sent.map(value => value.type), ["attach", "ack", "resize"]);
  assert.equal(connection.lastSequence, 12);
  first.close(1006);
  assert.equal([...timers.values()][0].delay, 500);
  await state.reconnect();
  const second = sockets[1];
  second.open();
  assert.equal(second.sent[0].afterSequence, 12);
  assert.equal(second.sent[0].grantToken, "grant-2");
  second.message({ type: "attached" });
  const previousStatusCount = state.statuses.length;
  first.message({ type: "exit" });
  first.emit("error");
  first.close(4009);
  assert.equal(connection.socket, second);
  assert.equal(connection.attached, true);
  assert.equal(connection.desiredAttached, true);
  assert.equal(state.statuses.length, previousStatusCount);
  assert.equal(state.revoked, false);
});

test("显式断开撤销重连，撤权不自动重连，普通断线逐次退避", async t => {
  const state = setup(t);
  const { connection, sockets, timers } = state;
  await connection.attach();
  sockets[0].close(1006);
  await state.reconnect();
  sockets[1].close(1006);
  assert.equal([...timers.values()][0].delay, 1000);
  connection.desiredAttached = false;
  connection.disconnect();
  assert.equal(timers.size, 0);
  await connection.attach();
  sockets[2].open();
  sockets[2].message({ type: "attached" });
  sockets[2].close(4009);
  assert.equal(connection.desiredAttached, false);
  assert.equal(state.revoked, true);
  assert.equal(timers.size, 0);
});

test("显式detach发送原协议并清除意图；授权失败保留错误且不建立socket", async t => {
  const state = setup(t);
  await state.connection.attach();
  state.sockets[0].open();
  state.sockets[0].message({ type: "attached" });
  state.connection.detach();
  assert.deepEqual(state.sockets[0].sent.at(-1), { type: "detach", terminalId: "term_test" });
  assert.equal(state.connection.attached, false);
  assert.equal(state.connection.desiredAttached, false);
  state.connection.requestGrant = async () => {
    throw new Error("owner denied");
  };
  await state.connection.attach();
  assert.deepEqual(state.statuses.at(-1), ["owner denied", true]);
  assert.equal(state.sockets.length, 1);
  assert.equal(state.timers.size, 0);
});

test("临时授权失败与握手失败沿同一退避重取票据，成功后清除重试次数", async t => {
  let attempts = 0;
  const state = setup(t, { requestGrant: async () => {
    if (++attempts === 1) throw Object.assign(new Error("owner 503"), { retryable: true });
    return { grant_token: `grant-${attempts}`, service: "terminal" };
  } });
  await state.connection.attach();
  assert.equal(state.connection.desiredAttached, true);
  assert.equal(state.sockets.length, 0);
  await state.reconnect();
  state.sockets[0].open();
  assert.equal(state.sockets[0].sent[0].grantToken, "grant-2");
  state.sockets[0].message({ type: "error", message: "grant expired" });
  state.sockets[0].close(4008);
  assert.equal([...state.timers.values()][0].delay, 1000);
  await state.reconnect();
  state.sockets[1].open();
  assert.equal(state.sockets[1].sent[0].grantToken, "grant-3");
  state.sockets[1].message({ type: "attached" });
  assert.equal(state.connection.reconnectAttempts, 0);
});

test("反复握手失败有界停止，用户可以重新连接；等待重试时detach取消意图", async t => {
  const state = setup(t);
  await state.connection.attach();
  for (let index = 0; index < 6; index++) {
    state.sockets[index].close(4008);
    await state.reconnect();
  }
  state.sockets[6].close(4008);
  assert.equal(state.timers.size, 0);
  assert.equal(state.connection.desiredAttached, false);
  assert.match(state.statuses.at(-1)[0], /请点击连接重试/);
  await state.connection.attach();
  state.sockets[7].close(1006);
  assert.equal([...state.timers.values()][0].delay, 500);
  state.connection.detach();
  assert.equal(state.timers.size, 0);
  assert.equal(state.connection.desiredAttached, false);
});

test("等待授权时断开再重新连接，以最新连接意图完成原在途授权", async t => {
  let deliver;
  let grants = 0;
  const state = setup(t, { requestGrant: () => {
    grants += 1;
    return new Promise(resolve => { deliver = resolve; });
  } });
  const pending = state.connection.attach();
  state.connection.detach();
  await state.connection.attach();
  deliver({ grant_token: "pending-grant", service: "terminal" });
  await pending;
  assert.equal(grants, 1);
  assert.equal(state.sockets.length, 1);
  state.sockets[0].open();
  state.sockets[0].message({ type: "attached" });
  assert.equal(state.connection.attached, true);
  assert.equal(state.connection.desiredAttached, true);
});

test("游标由当前页面快照与输出推进，不读写浏览器持久存储", async t => {
  const state = setup(t);
  state.connection.rememberSequence(12);
  await state.connection.attach();
  state.sockets[0].open();
  state.sockets[0].message({ type: "attached" });
  state.connection.acknowledge(13);
  state.connection.acknowledge(12);
  assert.equal(state.connection.lastSequence, 13);
  assert.equal(state.sockets[0].sent[0].afterSequence, 12);
  assert.deepEqual(state.sockets[0].sent.slice(1), [{ type: "ack", sequence: 13 }, { type: "ack", sequence: 12 }]);
  assert.equal(state.storageReads, 0);
  assert.equal(state.sequences.size, 0);
});
