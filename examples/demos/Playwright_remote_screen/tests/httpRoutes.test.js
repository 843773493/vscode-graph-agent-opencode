import { createServer, request as createHttpRequest } from "node:http";
import { describe, expect, test } from "bun:test";
import { createHttpRequestHandler } from "../src/server/httpRoutes.js";
import { createBrowserSessionStub } from "./browserSessionStub.js";

const httpSocketsByServer = new WeakMap();

function listen(server) {
  const sockets = new Set();
  httpSocketsByServer.set(server, sockets);
  server.on("connection", (socket) => {
    sockets.add(socket);
    socket.once("close", () => sockets.delete(socket));
  });

  return new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => {
      server.off("error", reject);
      resolve(server.address().port);
    });
  });
}

async function closeServer(server) {
  if (!server.listening) {
    return;
  }
  const sockets = httpSocketsByServer.get(server);
  if (!sockets) {
    throw new Error("HTTP 测试服务未通过 listen fixture 初始化");
  }
  await new Promise((resolve, reject) => {
    server.close((error) => {
      if (error) {
        reject(error);
        return;
      }
      resolve();
    });
    for (const socket of sockets) {
      socket.destroy();
    }
  });
}

function wait(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

async function postCommand(port, body) {
  return await fetch(`http://127.0.0.1:${port}/api/command`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body,
  });
}

describe("命令 API 请求边界", () => {
  test("无效输入返回 400，超限请求返回 413", async () => {
    const pageState = {
      browserId: "uuid:12",
      closed: false,
      url: "https://example.test/",
      title: "Fixture page",
      viewport: { width: 1280, height: 800 },
      attachedClients: 0,
      streaming: false,
    };
    const server = createServer(createHttpRequestHandler({
      currentState: async () => pageState,
      goto: async () => { throw new Error("browser navigation failed"); },
    }));
    try {
      const port = await listen(server);
      const invalidBodies = [
        "{",
        JSON.stringify({ line: 42 }),
        JSON.stringify({ line: "dance" }),
        JSON.stringify({ line: "exit" }),
      ];
      for (const body of invalidBodies) {
        const response = await postCommand(port, body);
        expect(response.status).toBe(400);
        expect(await response.json()).toHaveProperty("error");
      }

      const bodyPrefix = '{"line":"';
      const bodySuffix = '"}';
      const maxBodyBytes = 65536;
      const exactLimitBody = bodyPrefix
        + " ".repeat(maxBodyBytes - Buffer.byteLength(bodyPrefix) - Buffer.byteLength(bodySuffix))
        + bodySuffix;
      expect(Buffer.byteLength(exactLimitBody)).toBe(maxBodyBytes);
      const exactLimitResponse = await postCommand(port, exactLimitBody);
      expect(exactLimitResponse.status).toBe(200);

      const oversizedResponse = await postCommand(port, "x".repeat(65537));
      expect(oversizedResponse.status).toBe(413);
      expect(await oversizedResponse.json()).toEqual({ error: "请求体过大" });

      const executionFailure = await postCommand(port, JSON.stringify({ line: "goto example.com" }));
      expect(executionFailure.status).toBe(500);
      expect(await executionFailure.json()).toEqual({ error: "browser navigation failed" });

      const successfulCommand = await postCommand(port, JSON.stringify({ line: "title" }));
      expect(successfulCommand.status).toBe(200);
      expect(await successfulCommand.json()).toEqual({
        ok: true,
        output: "Fixture page",
        state: pageState,
      });
    } finally {
      await closeServer(server);
    }
  });

  test("导航响应复用导航生成的状态快照", async () => {
    const session = createBrowserSessionStub({ send: async () => undefined });
    let titleReads = 0;
    const readPageTitle = session.page.title;
    session.page.title = async () => {
      titleReads += 1;
      return await readPageTitle();
    };
    const server = createServer(createHttpRequestHandler(session));
    try {
      const port = await listen(server);
      const response = await postCommand(port, JSON.stringify({ line: "goto example.test/next" }));

      expect(response.status).toBe(200);
      expect(await response.json()).toEqual({
        ok: true,
        output: "已进入 https://example.test/next",
        state: {
          browserId: "uuid:12",
          closed: false,
          url: "https://example.test/next",
          title: "fixture page",
          viewport: { width: 1280, height: 800 },
          attachedClients: 0,
          streaming: false,
        },
      });
      expect(titleReads).toBe(1);

      const clickResponse = await postCommand(port, JSON.stringify({ line: "click 10 20" }));
      expect(clickResponse.status).toBe(200);
      expect(await clickResponse.json()).toEqual({
        ok: true,
        output: "已点击 10,20",
        state: {
          browserId: "uuid:12",
          closed: false,
          url: "https://example.test/next",
          title: "fixture page",
          viewport: { width: 1280, height: 800 },
          attachedClients: 0,
          streaming: false,
        },
      });
      expect(titleReads).toBe(2);
    } finally {
      await closeServer(server);
    }
  });
});

describe("CDP HTTP 代理生命周期", () => {
  test("剔除 Connection 点名的逐跳响应头并保留端到端头", async () => {
    const upstreamServer = createServer((_request, response) => {
      response.writeHead(200, {
        connection: "x-connection-private, close",
        "proxy-connection": "x-proxy-private",
        "x-connection-private": "connection-only",
        "x-proxy-private": "proxy-only",
        "x-end-to-end": "preserved",
      });
      response.end("inspector resource");
    });
    let upstreamPort;
    const proxyServer = createServer(createHttpRequestHandler({
      cdpHttpOrigin: () => `http://127.0.0.1:${upstreamPort}`,
    }));

    try {
      upstreamPort = await listen(upstreamServer);
      const proxyPort = await listen(proxyServer);
      const response = await fetch(`http://127.0.0.1:${proxyPort}/devtools/inspector.html`);

      expect(response.status).toBe(200);
      expect(await response.text()).toBe("inspector resource");
      expect(response.headers.get("x-connection-private")).toBeNull();
      expect(response.headers.get("x-proxy-private")).toBeNull();
      expect(response.headers.get("x-end-to-end")).toBe("preserved");
    } finally {
      await Promise.all([closeServer(proxyServer), closeServer(upstreamServer)]);
    }
  });

  test("查看器断开时取消仍在等待响应头的 CDP 请求", async () => {
    let resolveUpstreamRequest;
    let resolveUpstreamSocketClose;
    const upstreamRequestReceived = new Promise((resolve) => {
      resolveUpstreamRequest = resolve;
    });
    const upstreamSocketClosed = new Promise((resolve) => {
      resolveUpstreamSocketClose = resolve;
    });
    const upstreamServer = createServer((request) => {
      const responseTimeout = setTimeout(() => request.socket.destroy(), 3000);
      request.socket.once("close", () => {
        clearTimeout(responseTimeout);
        resolveUpstreamSocketClose();
      });
      resolveUpstreamRequest();
    });
    let upstreamPort;
    const proxyServer = createServer(createHttpRequestHandler({
      cdpHttpOrigin: () => `http://127.0.0.1:${upstreamPort}`,
    }));
    try {
      upstreamPort = await listen(upstreamServer);
      const proxyPort = await listen(proxyServer);
      const request = createHttpRequest({
        host: "127.0.0.1",
        port: proxyPort,
        path: "/devtools/inspector.html",
        method: "GET",
      });
      const clientRequestClosed = new Promise((resolve) => {
        request.once("close", resolve);
      });
      request.end();

      await Promise.race([
        upstreamRequestReceived,
        wait(1000).then(() => { throw new Error("上游未收到代理请求"); }),
      ]);
      request.destroy();
      await clientRequestClosed;

      const upstreamClosed = await Promise.race([
        upstreamSocketClosed.then(() => true),
        wait(1000).then(() => false),
      ]);
      expect(upstreamClosed).toBe(true);
    } finally {
      await Promise.all([closeServer(proxyServer), closeServer(upstreamServer)]);
    }
  });
});
