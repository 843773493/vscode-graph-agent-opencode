import { request as httpRequest, createServer as createHttpServer } from "node:http";
import { closeServer, listenServer } from "./server-lifetime.mjs";

function gatewayTargetUrl(target) {
  return `http://${target.host}:${target.port}`;
}

function writeUnavailableResponse(response) {
  response.writeHead(503, {
    "cache-control": "no-store",
    "content-type": "text/plain; charset=utf-8",
  });
  response.end("Gateway supervisor 尚未绑定 active generation\n");
}

function proxyHttpRequest(request, response, target) {
  if (target === null) {
    writeUnavailableResponse(response);
    return;
  }
  const failResponse = (error) => {
    if (response.destroyed) return;
    if (response.headersSent) {
      // 正文已开始，必须保留不完整响应，不能拼入错误文本或伪造正常结束。
      response.destroy(error);
      return;
    }
    response.writeHead(502, {
      "content-type": "text/plain; charset=utf-8",
    });
    response.end(`Gateway generation 代理失败: ${error.message}\n`);
  };
  const upstream = httpRequest(
    {
      hostname: target.host,
      port: target.port,
      method: request.method,
      path: request.url,
      headers: request.headers,
      agent: false,
    },
    (upstreamResponse) => {
      upstreamResponse.once("error", failResponse);
      response.writeHead(
        upstreamResponse.statusCode ?? 502,
        upstreamResponse.headers,
      );
      upstreamResponse.pipe(response);
    },
  );
  // 响应关闭包含客户端取消，不能让上游流继续占用连接。
  response.once("close", () => upstream.destroy());
  upstream.once("error", failResponse);
  request.pipe(upstream);
}

function proxyGatewayUpgrade(request, clientSocket, clientHead, target) {
  if (target === null) {
    clientSocket.end(
      "HTTP/1.1 503 Service Unavailable\r\nConnection: close\r\n\r\n",
    );
    return;
  }
  const upstream = httpRequest({
    hostname: target.host,
    port: target.port,
    method: request.method,
    path: request.url,
    headers: request.headers,
    agent: false,
  });
  let upgradedUpstreamSocket = null;
  const closeSockets = () => {
    clientSocket.destroy();
    upgradedUpstreamSocket?.destroy();
    upstream.destroy();
  };
  // 等待101期间也必须接收取消；升级后由同一入口释放连接。
  clientSocket.once("end", closeSockets);
  clientSocket.once("close", closeSockets);
  clientSocket.once("error", closeSockets);
  upstream.once("upgrade", (upstreamResponse, upstreamSocket, upstreamHead) => {
    upgradedUpstreamSocket = upstreamSocket;
    upstreamSocket.once("error", closeSockets);
    if (clientSocket.destroyed) {
      closeSockets();
      return;
    }
    const responseLines = [
      `HTTP/1.1 ${upstreamResponse.statusCode ?? 101} ${upstreamResponse.statusMessage ?? "Switching Protocols"}`,
    ];
    for (
      let index = 0;
      index < upstreamResponse.rawHeaders.length;
      index += 2
    ) {
      responseLines.push(
        `${upstreamResponse.rawHeaders[index]}: ${upstreamResponse.rawHeaders[index + 1]}`,
      );
    }
    clientSocket.write(`${responseLines.join("\r\n")}\r\n\r\n`);
    if (upstreamHead.length > 0) clientSocket.write(upstreamHead);
    if (clientHead.length > 0) upstreamSocket.write(clientHead);
    clientSocket.pipe(upstreamSocket);
    upstreamSocket.pipe(clientSocket);
  });
  upstream.once("response", closeSockets);
  upstream.once("error", closeSockets);
  upstream.end();
}

export function createGatewayPublicListener({ host, port }) {
  let target = null;
  const server = createHttpServer((request, response) => {
    proxyHttpRequest(request, response, target);
  });
  server.on("upgrade", (request, clientSocket, clientHead) => {
    proxyGatewayUpgrade(request, clientSocket, clientHead, target);
  });
  return {
    async listen() {
      await listenServer(server, { host, port });
    },
    setTarget(nextTarget) {
      if (
        nextTarget !== null &&
        (typeof nextTarget.host !== "string" ||
          !Number.isSafeInteger(nextTarget.port) ||
          nextTarget.port < 1 ||
          nextTarget.port > 65535)
      ) {
        throw new TypeError("Gateway supervisor target 无效");
      }
      target = nextTarget;
    },
    address() {
      return server.address();
    },
    targetUrl() {
      return target === null ? null : gatewayTargetUrl(target);
    },
    async close() {
      await closeServer(server);
    },
  };
}
