import {
  request as httpRequest,
  createServer as createHttpServer,
} from "node:http";
import { closeServer, listenServer } from "./server-lifecycle.mjs";

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
      response.writeHead(
        upstreamResponse.statusCode ?? 502,
        upstreamResponse.headers,
      );
      upstreamResponse.pipe(response);
    },
  );
  upstream.once("error", (error) => {
    if (!response.headersSent) {
      response.writeHead(502, {
        "content-type": "text/plain; charset=utf-8",
      });
    }
    response.end(`Gateway generation 代理失败: ${error.message}\n`);
  });
  request.pipe(upstream);
}

function proxyGatewayUpgrade(request, clientSocket, head, target) {
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
  const closeSockets = () => {
    clientSocket.destroy();
    upstream.destroy();
  };
  upstream.once("upgrade", (upstreamResponse, upstreamSocket, upstreamHead) => {
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
    clientSocket.pipe(upstreamSocket);
    upstreamSocket.pipe(clientSocket);
    clientSocket.once("error", closeSockets);
    upstreamSocket.once("error", closeSockets);
  });
  upstream.once("response", (upstreamResponse) => {
    upstreamResponse.resume();
    clientSocket.destroy();
  });
  upstream.once("error", closeSockets);
  upstream.end(head);
}

export function createGatewayPublicListener({ host, port }) {
  let target = null;
  const server = createHttpServer((request, response) => {
    proxyHttpRequest(request, response, target);
  });
  server.on("upgrade", (request, clientSocket, head) => {
    proxyGatewayUpgrade(request, clientSocket, head, target);
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
