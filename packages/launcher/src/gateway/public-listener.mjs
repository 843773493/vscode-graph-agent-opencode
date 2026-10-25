import {
  Agent as HttpAgent,
  request as httpRequest,
  createServer as createHttpServer,
} from "node:http";
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

function retireTargetAgent(agent) {
  if (agent === null) return;
  agent.keepAlive = false;
  for (const sockets of Object.values(agent.freeSockets)) {
    for (const socket of sockets) socket.destroy();
  }
}

function proxyHttpRequest(request, response, targetState) {
  if (targetState === null) {
    writeUnavailableResponse(response);
    return;
  }
  const { target, agent } = targetState;
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
      agent,
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
  // close也会在正常结束后触发；只中止未完成的响应，完整响应留给Agent复用。
  response.once("close", () => {
    if (!response.writableFinished) upstream.destroy();
  });
  upstream.once("error", failResponse);
  request.pipe(upstream);
}

function proxyGatewayUpgrade(request, clientSocket, clientHead, targetState) {
  if (targetState === null) {
    clientSocket.end(
      "HTTP/1.1 503 Service Unavailable\r\nConnection: close\r\n\r\n",
    );
    return;
  }
  const { target, agent } = targetState;
  const upstream = httpRequest({
    hostname: target.host,
    port: target.port,
    method: request.method,
    path: request.url,
    headers: request.headers,
    agent,
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
  let targetState = null;
  const server = createHttpServer((request, response) => {
    proxyHttpRequest(request, response, targetState);
  });
  server.on("upgrade", (request, clientSocket, clientHead) => {
    proxyGatewayUpgrade(request, clientSocket, clientHead, targetState);
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
      const previousAgent = targetState?.agent ?? null;
      // 每个generation使用独立连接池；旧在途请求结束后关闭，不跨generation复用。
      targetState = nextTarget === null
        ? null
        : { target: nextTarget, agent: new HttpAgent({ keepAlive: true }) };
      retireTargetAgent(previousAgent);
    },
    address() {
      return server.address();
    },
    targetUrl() {
      return targetState === null ? null : gatewayTargetUrl(targetState.target);
    },
    async close() {
      retireTargetAgent(targetState?.agent ?? null);
      targetState = null;
      await closeServer(server);
    },
  };
}
