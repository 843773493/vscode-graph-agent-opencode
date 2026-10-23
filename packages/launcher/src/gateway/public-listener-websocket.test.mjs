import { describe, test } from "bun:test";
import { execFileSync } from "node:child_process";

const listenerUrl = new URL("./public-listener.mjs", import.meta.url).href;
const lifetimeUrl = new URL("./server-lifetime.mjs", import.meta.url).href;

function runWebSocketProbe(scenario) {
  // 正式Launcher使用Node；ws负责真实握手和帧，fixture不另造WebSocket协议。
  const source = `
    import assert from "node:assert/strict";
    import { once } from "node:events";
    import { createServer } from "node:http";
    import { createConnection } from "node:net";
    import { WebSocket, WebSocketServer, Sender } from "ws";
    const [listenerUrl, lifetimeUrl, scenario] = JSON.parse(process.argv[1]);
    const { createGatewayPublicListener } = await import(listenerUrl);
    const { listenServer, closeServer } = await import(lifetimeUrl);
    async function within(promise, phase) {
      let timer;
      try {
        return await Promise.race([promise, new Promise((_resolve, reject) => {
          timer = setTimeout(() => reject(new Error("WebSocket fixture超时: " + phase)), 1000);
        })]);
      } finally { clearTimeout(timer); }
    }
    let notifyUpgrade, notifyClosed, notifyMessage;
    const upgraded = new Promise((resolve) => { notifyUpgrade = resolve; });
    const backendClosed = new Promise((resolve) => { notifyClosed = resolve; });
    const firstMessage = new Promise((resolve) => { notifyMessage = resolve; });
    let backendSocket, backendWebSocket, backendRequest;
    let rawClient, client;
    const clientErrors = [];
    const server = createServer();
    const sockets = new WebSocketServer({ noServer: true });
    server.on("upgrade", (request, socket, head) => {
      backendSocket = socket;
      backendRequest = request;
      socket.once("close", notifyClosed);
      socket.on("error", (error) => {
        if (error.code !== "ECONNRESET") throw error;
      });
      // 手写的等待/拒绝端也须响应TCP半关闭，不能让fixture自身保持写半边。
      if (scenario === "client-before-upgrade" || scenario === "rejected-upgrade") {
        socket.once("end", () => socket.end());
      }
      notifyUpgrade();
      if (scenario === "client-before-upgrade") return;
      if (scenario === "rejected-upgrade") {
        socket.write("HTTP/1.1 403 Forbidden\\r\\nTransfer-Encoding: chunked\\r\\n\\r\\n3\\r\\nno!\\r\\n");
        return;
      }
      sockets.handleUpgrade(request, socket, head, (webSocket) => {
        backendWebSocket = webSocket;
        webSocket.on("message", (data) => {
          notifyMessage(data.toString());
          webSocket.send(data);
        });
        webSocket.send("welcome");
      });
    });
    const listener = createGatewayPublicListener({ host: "127.0.0.1", port: 0 });
    try {
      await listenServer(server, { host: "127.0.0.1", port: 0 });
      await listener.listen();
      listener.setTarget({ host: "127.0.0.1", port: server.address().port });
      const port = listener.address().port;
      if (scenario === "first-frame" || scenario === "client-before-upgrade") {
        rawClient = createConnection({ host: "127.0.0.1", port });
        rawClient.resume();
        await within(once(rawClient, "connect"), "原始客户端连接");
        const handshake = Buffer.from([
          "GET /socket HTTP/1.1", "Host: 127.0.0.1", "Upgrade: websocket",
          "Connection: Upgrade", "Sec-WebSocket-Version: 13",
          "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==", "", "",
        ].join("\\r\\n"));
        const firstFrame = scenario === "first-frame"
          ? Buffer.concat(Sender.frame(Buffer.from("initial-frame"), {
            fin: true, opcode: 1, mask: true, readOnly: true,
          })) : Buffer.alloc(0);
        rawClient.write(Buffer.concat([handshake, firstFrame]));
        await within(upgraded, "目标收到握手");
        if (scenario === "client-before-upgrade") {
          rawClient.destroy();
          await within(backendClosed, "握手等待期间释放上游");
        } else {
          assert.equal(await within(firstMessage, "握手附带的首帧"), "initial-frame");
          assert.equal(backendRequest.headers["content-length"], undefined);
          assert.equal(backendRequest.headers["transfer-encoding"], undefined);
        }
      } else {
        client = new WebSocket("ws://127.0.0.1:" + port + "/socket");
        client.on("error", (error) => clientErrors.push(error.message));
        if (scenario === "rejected-upgrade") {
          await within(once(client, "error"), "拒绝握手的失败交付");
          await within(backendClosed, "拒绝握手后释放上游");
        } else {
          const greeting = once(client, "message");
          await within(once(client, "open"), "客户端握手");
          assert.equal((await within(greeting, "服务端首帧"))[0].toString(), "welcome");
          if (scenario === "graceful-close") {
            const closed = once(client, "close");
            client.close(1000, "finished");
            const [code, reason] = await within(closed, "正常关闭握手");
            assert.equal(code, 1000);
            assert.equal(reason.toString(), "finished");
            await within(backendClosed, "正常关闭后释放上游");
          } else if (scenario === "client-after-upgrade") {
            client.terminate();
            await within(backendClosed, "客户端断开后释放上游");
          } else if (scenario === "upstream-after-upgrade") {
            const closed = once(client, "close");
            backendWebSocket.terminate();
            await within(closed, "上游断开后释放客户端");
          } else {
            const echo = once(client, "message");
            client.send("payload");
            assert.equal((await within(echo, "双向消息"))[0].toString(), "payload");
          }
          assert.deepEqual(clientErrors, []);
        }
      }
      console.log(JSON.stringify({ scenario, clientErrors }));
    } finally {
      rawClient?.destroy();
      client?.terminate();
      backendWebSocket?.terminate();
      backendSocket?.destroy();
      await listener.close();
      sockets.close();
      await closeServer(server);
    }
  `;
  execFileSync("node", ["--input-type=module", "--eval", source,
    JSON.stringify([listenerUrl, lifetimeUrl, scenario])], { encoding: "utf8", timeout: 5000 });
}

describe("Gateway WebSocket转发与释放", () => {
  for (const [scenario, title] of [
    ["echo", "正常握手转发服务端首帧与双向消息"],
    ["graceful-close", "正常关闭保留关闭码与原因并释放连接"],
    ["first-frame", "握手附带首帧按升级数据转发而不是HTTP正文"],
    ["client-before-upgrade", "客户端在等待握手期间退出会释放上游连接"],
    ["client-after-upgrade", "升级后客户端断开会释放上游连接"],
    ["upstream-after-upgrade", "升级后上游断开会释放客户端连接"],
    ["rejected-upgrade", "目标拒绝握手后不继续读取无界响应正文"],
  ]) {
    test(title, () => runWebSocketProbe(scenario));
  }
});
