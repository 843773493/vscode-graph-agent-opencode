import { describe, expect, test } from "bun:test";
import { execFileSync } from "node:child_process";
import { once } from "node:events";
import { createServer as createHttpServer } from "node:http";
import { createConnection } from "node:net";
import { createGatewayPublicListener } from "./public-listener.mjs";
import { closeServer, listenServer } from "./server-lifetime.mjs";

async function within(promise, phase) {
  let timer;
  try {
    return await Promise.race([
      promise,
      new Promise((_resolve, reject) => {
        timer = setTimeout(() => reject(new Error(`连接fixture超时: ${phase}`)), 1000);
      }),
    ]);
  } finally {
    clearTimeout(timer);
  }
}

async function observeHttpDisconnect(streamResponse) {
  let receivedRequest;
  let closedUpstream;
  const received = new Promise((resolve) => { receivedRequest = resolve; });
  const closed = new Promise((resolve) => { closedUpstream = resolve; });
  const upstream = createHttpServer((request, response) => {
    request.socket.once("close", closedUpstream);
    if (streamResponse) {
      response.writeHead(200, { "content-type": "text/event-stream" });
      response.write("stream-prefix\n");
    }
    receivedRequest();
  });
  const listener = createGatewayPublicListener({ host: "127.0.0.1", port: 0 });
  let client;
  try {
    await listenServer(upstream, { host: "127.0.0.1", port: 0 });
    await listener.listen();
    listener.setTarget({ host: "127.0.0.1", port: upstream.address().port });
    client = createConnection({ host: "127.0.0.1", port: listener.address().port });
    let receivedPrefix;
    const prefix = new Promise((resolve) => { receivedPrefix = resolve; });
    let responseText = "";
    client.on("data", (chunk) => {
      responseText += chunk.toString();
      if (responseText.includes("stream-prefix")) receivedPrefix();
    });
    await within(once(client, "connect"), "客户端连接");
    client.write("GET /events HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n");
    await within(received, "目标收到请求");
    if (streamResponse) await within(prefix, "客户端收到流正文");
    const clientClosed = once(client, "close");
    client.destroy();
    await within(clientClosed, "客户端关闭");
    await within(closed, "上游连接释放");
  } finally {
    client?.destroy();
    upstream.closeAllConnections();
    await listener.close();
    await closeServer(upstream);
  }
}

function runNodeHttpDisconnect(streamResponse) {
  // Launcher由Node执行；Bun的HTTP shim未转发响应关闭事件，不能代替真实运行时。
  const source = `
    import { once } from "node:events";
    import { createServer as createHttpServer } from "node:http";
    import { createConnection } from "node:net";
    import { createGatewayPublicListener } from ${JSON.stringify(new URL("./public-listener.mjs", import.meta.url).href)};
    import { closeServer, listenServer } from ${JSON.stringify(new URL("./server-lifetime.mjs", import.meta.url).href)};
    ${within.toString()}
    ${observeHttpDisconnect.toString()}
    await observeHttpDisconnect(${JSON.stringify(streamResponse)});
  `;
  execFileSync("node", ["--input-type=module", "--eval", source], {
    encoding: "utf8",
    timeout: 5000,
  });
}

function runNodeUpstreamFailure(scenario) {
  const source = `
    import assert from "node:assert/strict";
    import { createServer, request as httpRequest } from "node:http";
    const [listenerUrl, lifetimeUrl, scenario] = JSON.parse(process.argv[1]);
    const { createGatewayPublicListener } = await import(listenerUrl);
    const { listenServer, closeServer } = await import(lifetimeUrl);
    let upstreamSocket, downstreamRequest;
    const server = createServer((request, response) => {
      if (request.url === "/healthy") { response.end("healthy"); return; }
      upstreamSocket = request.socket;
      if (scenario === "before-headers") { request.socket.destroy(); return; }
      response.writeHead(200, scenario === "fixed-length"
        ? { "content-length": "100" } : { "content-type": "text/plain" });
      response.write("prefix");
    });
    const listener = createGatewayPublicListener({ host: "127.0.0.1", port: 0 });
    async function readPublic(url, abortUpstream = false) {
      let timer;
      try {
        return await Promise.race([
          new Promise((resolve, reject) => {
            downstreamRequest = httpRequest({ host: "127.0.0.1", port: listener.address().port,
              path: url, agent: false }, (response) => {
              let body = "";
              response.setEncoding("utf8");
              response.on("data", (chunk) => {
                body += chunk;
                if (abortUpstream) upstreamSocket.destroy();
              });
              response.once("end", () => resolve({ status: response.statusCode, body,
                ended: true, complete: response.complete }));
              response.once("error", (error) => resolve({ status: response.statusCode, body,
                ended: false, complete: response.complete, error: error.message, code: error.code }));
            });
            downstreamRequest.once("error", reject);
            downstreamRequest.end();
          }),
          new Promise((_resolve, reject) => {
            timer = setTimeout(() => reject(new Error("HTTP失败fixture超时: " + url)), 1000);
          }),
        ]);
      } finally { clearTimeout(timer); }
    }
    try {
      await listenServer(server, { host: "127.0.0.1", port: 0 });
      await listener.listen();
      listener.setTarget({ host: "127.0.0.1", port: server.address().port });
      const broken = await readPublic("/broken", scenario !== "before-headers");
      if (scenario === "before-headers") {
        assert.equal(broken.status, 502);
        assert.equal(broken.ended, true);
        assert.match(broken.body, /Gateway generation 代理失败:/);
      } else {
        assert.equal(broken.status, 200);
        assert.equal(broken.ended, false);
        assert.equal(broken.complete, false);
        assert.equal(broken.body, "prefix");
        assert.equal(typeof broken.error, "string");
      }
      const healthy = await readPublic("/healthy");
      assert.deepEqual(healthy, { status: 200, body: "healthy", ended: true, complete: true });
      console.log(JSON.stringify({ scenario, broken, healthy }));
    } finally {
      downstreamRequest?.destroy();
      server.closeAllConnections();
      await listener.close();
      await closeServer(server);
    }
  `;
  execFileSync("node", ["--input-type=module", "--eval", source,
    JSON.stringify([new URL("./public-listener.mjs", import.meta.url).href,
      new URL("./server-lifetime.mjs", import.meta.url).href, scenario])],
  { encoding: "utf8", timeout: 5000 });
}

describe("Gateway公开监听", () => {
  test("稳定 public listener 代理 HTTP 流并可切换 target", async () => {
    const upstream = createHttpServer((request, response) => {
      response.writeHead(200, { "content-type": "text/event-stream" });
      response.write(`method=${request.method}\n`);
      request.on("data", (chunk) => response.write(chunk));
      request.on("end", () => response.end("done\n"));
    });
    await new Promise((resolve) => upstream.listen(0, "127.0.0.1", resolve));
    const upstreamAddress = upstream.address();
    const listener = createGatewayPublicListener({
      host: "127.0.0.1",
      port: 0,
    });
    await listener.listen();
    const publicAddress = listener.address();
    expect(typeof publicAddress).toBe("object");
    expect(typeof upstreamAddress).toBe("object");
    listener.setTarget({
      host: "127.0.0.1",
      port: upstreamAddress.port,
    });

    try {
      const response = await fetch(
        `http://127.0.0.1:${publicAddress.port}/events`,
        {
          method: "POST",
          body: "payload",
        },
      );
      expect(response.status).toBe(200);
      expect(await response.text()).toBe("method=POST\npayloaddone\n");
    } finally {
      await listener.close();
      await new Promise((resolve) => upstream.close(resolve));
    }
  });

  test("响应头到达前客户端断开会释放上游请求", async () => {
    runNodeHttpDisconnect(false);
  });

  test("流正文传输期间客户端断开会释放上游连接", async () => {
    runNodeHttpDisconnect(true);
  });
});

describe("HTTP上游失败交付", () => {
  for (const [scenario, title] of [
    ["before-headers", "响应头前失败返回502并能处理后续请求"],
    ["chunked", "分块正文中断明确失败且代理继续服务"],
    ["fixed-length", "定长正文未完成时不伪报成功或拼入错误文本"],
  ]) {
    test(title, () => runNodeUpstreamFailure(scenario));
  }
});
