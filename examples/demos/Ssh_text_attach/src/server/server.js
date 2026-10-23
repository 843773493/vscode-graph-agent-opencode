import { createServer } from "node:http";
import { fileURLToPath } from "node:url";
import { loadTargetsConfig, resolveRuntimeConfig } from "./config.js";
import { TextFileStore } from "./fileStore.js";
import { HttpError, readJsonRequest, requestUrl, requireMethod, writeJson } from "./http.js";
import { createStaticFileHandler } from "./staticFiles.js";
import { TargetFileClient } from "./targetFileClient.js";
import { SshTunnelManager } from "./tunnel.js";
import { proxyBackendRequest } from "./upstreamProxy.js";

const clientRoot = fileURLToPath(new URL("../client/", import.meta.url));
const PROXY_PREFIX = "/api/proxy/";

function parseBackendProxyRoute(url) {
  if (!url.pathname.startsWith(PROXY_PREFIX)) {
    return null;
  }

  const rest = url.pathname.slice(PROXY_PREFIX.length);
  const slashIndex = rest.indexOf("/");
  if (slashIndex <= 0) {
    throw new HttpError(400, "proxy 路径必须是 /api/proxy/:targetId/:backendPath");
  }

  const targetId = decodeURIComponent(rest.slice(0, slashIndex));
  const backendPath = rest.slice(slashIndex);
  if (!targetId) {
    throw new HttpError(400, "proxy targetId 不能为空");
  }

  return {
    targetId,
    backendPath,
  };
}

async function handleGatewayRequest(targetClient, request, response) {
  try {
    if (request.method === "OPTIONS") {
      writeJson(response, 204, {});
      return;
    }

    const url = requestUrl(request);
    const backendProxyRoute = parseBackendProxyRoute(url);
    if (backendProxyRoute) {
      await proxyBackendRequest(targetClient, backendProxyRoute, request, response, url);
      return;
    }

    if (url.pathname === "/health") {
      requireMethod(request, "GET");
      writeJson(response, 200, { ok: true, role: "gateway" });
      return;
    }

    if (url.pathname === "/api/targets") {
      requireMethod(request, "GET");
      writeJson(response, 200, targetClient.listTargets());
      return;
    }

    throw new HttpError(404, `未知 gateway 接口: ${url.pathname}`);
  } catch (error) {
    const statusCode = error instanceof HttpError ? error.statusCode : 500;
    const message = error instanceof Error ? error.message : String(error);
    console.error(`[gateway] ${request.method} ${request.url} ${statusCode}: ${message}`);
    if (response.destroyed) {
      return;
    }
    if (response.headersSent) {
      response.destroy();
      return;
    }
    writeJson(response, statusCode, { error: message });
  }
}

async function handleBackendRequest(localStore, request, response) {
  try {
    if (request.method === "OPTIONS") {
      writeJson(response, 204, {});
      return;
    }

    const url = requestUrl(request);
    if (url.pathname === "/health") {
      requireMethod(request, "GET");
      writeJson(response, 200, { ok: true, role: "backend" });
      return;
    }

    if (url.pathname === "/api/managed-file") {
      if (request.method === "GET") {
        writeJson(response, 200, { file: await localStore.snapshot() });
        return;
      }
      if (request.method === "PUT") {
        const body = await readJsonRequest(request);
        if (!body || typeof body !== "object" || typeof body.content !== "string") {
          throw new HttpError(400, "请求体必须包含字符串字段 content");
        }
        writeJson(response, 200, { file: await localStore.save(body.content) });
        return;
      }
      throw new HttpError(405, `只支持 GET/PUT /api/managed-file，当前请求方法: ${request.method}`);
    }

    throw new HttpError(404, `未知 backend 接口: ${url.pathname}`);
  } catch (error) {
    const statusCode = error instanceof HttpError ? error.statusCode : 500;
    const message = error instanceof Error ? error.message : String(error);
    console.error(`[backend] ${request.method} ${request.url} ${statusCode}: ${message}`);
    if (response.destroyed) {
      return;
    }
    if (response.headersSent) {
      response.destroy();
      return;
    }
    writeJson(response, statusCode, { error: message });
  }
}

function listen(server, host, port, label) {
  return new Promise((resolve, reject) => {
    const onError = (error) => {
      server.off("error", onError);
      server.off("listening", onListening);
      reject(new Error(
        `[${label}] 无法监听 ${host}:${port}: ${error.message}`,
        { cause: error },
      ));
    };
    const onListening = () => {
      server.off("error", onError);
      console.log(`[${label}] http://${host}:${port}`);
      resolve();
    };

    server.once("error", onError);
    server.once("listening", onListening);
    try {
      server.listen(port, host);
    } catch (error) {
      onError(error);
    }
  });
}

function closeServer(server) {
  if (!server.listening) {
    return Promise.resolve();
  }
  return new Promise((resolve, reject) => {
    server.close((error) => {
      if (error) {
        reject(error);
        return;
      }
      resolve();
    });
  });
}

async function closeServers(serverEntries) {
  const closeResults = await Promise.allSettled(
    serverEntries.map(({ server }) => closeServer(server)),
  );
  const closeErrors = closeResults
    .filter((result) => result.status === "rejected")
    .map((result) => result.reason);
  if (closeErrors.length > 0) {
    throw new AggregateError(closeErrors, "一个或多个 HTTP 服务关闭失败");
  }
}

async function startServers(serverEntries) {
  const listenResults = await Promise.allSettled(serverEntries.map(({ server, host, port, label }) => (
    listen(server, host, port, label)
  )));
  const listenErrors = listenResults
    .filter((result) => result.status === "rejected")
    .map((result) => result.reason);
  if (listenErrors.length === 0) {
    return;
  }

  const startupErrors = [...listenErrors];
  try {
    await closeServers(serverEntries);
  } catch (error) {
    startupErrors.push(error);
  }
  throw new AggregateError(startupErrors, "Ssh_text_attach 服务启动失败");
}

const runtimeConfig = resolveRuntimeConfig();
const servers = [];
const tunnelManager = new SshTunnelManager();

if (runtimeConfig.roles.includes("backend")) {
  const localStore = new TextFileStore({
    filePath: runtimeConfig.dataFilePath,
    label: runtimeConfig.dataLabel,
  });
  await localStore.ensureFileExists();
  const backendServer = createServer((request, response) => {
    void handleBackendRequest(localStore, request, response);
  });
  servers.push({
    label: "backend",
    server: backendServer,
    host: runtimeConfig.backendHost,
    port: runtimeConfig.backendPort,
  });
  console.log(`[data] ${runtimeConfig.dataFilePath}`);
}

if (runtimeConfig.roles.includes("gateway")) {
  const targetsConfig = await loadTargetsConfig(runtimeConfig.targetsConfigPath, runtimeConfig.demoRoot);
  const targetClient = new TargetFileClient({
    targetsConfig,
    tunnelManager,
  });
  const gatewayServer = createServer((request, response) => {
    void handleGatewayRequest(targetClient, request, response);
  });
  servers.push({
    label: "gateway",
    server: gatewayServer,
    host: runtimeConfig.gatewayHost,
    port: runtimeConfig.gatewayPort,
  });
  console.log(`[targets] ${runtimeConfig.targetsConfigPath}`);
}

if (runtimeConfig.roles.includes("frontend")) {
  const frontendServer = createServer(createStaticFileHandler(clientRoot, runtimeConfig.gatewayPort));
  servers.push({
    label: "frontend",
    server: frontendServer,
    host: runtimeConfig.frontendHost,
    port: runtimeConfig.frontendPort,
  });
}

let shutdownStarted = false;
let startupPromise;

async function shutdown(signal) {
  if (shutdownStarted) {
    return;
  }
  shutdownStarted = true;
  process.off("SIGINT", handleInterrupt);
  process.off("SIGTERM", handleTermination);
  console.log(`[shutdown] 收到 ${signal}`);
  const shutdownErrors = [];
  if (startupPromise) {
    try {
      await startupPromise;
    } catch (error) {
      shutdownErrors.push(error);
    }
  }
  try {
    await tunnelManager.shutdown();
  } catch (error) {
    shutdownErrors.push(error);
  }
  try {
    await closeServers(servers);
  } catch (error) {
    shutdownErrors.push(error);
  }
  if (shutdownErrors.length > 0) {
    throw new AggregateError(shutdownErrors, "服务关闭期间发生错误");
  }
  process.exitCode = 0;
}

function handleTermination() {
  handleShutdownSignal("SIGTERM");
}

function handleInterrupt() {
  handleShutdownSignal("SIGINT");
}

function handleShutdownSignal(signal) {
  void shutdown(signal).catch((error) => {
    const message = error instanceof Error ? error.message : String(error);
    console.error(`[shutdown] 关闭服务失败: ${message}`);
    process.exitCode = 1;
  });
}

process.once("SIGINT", handleInterrupt);
process.once("SIGTERM", handleTermination);

startupPromise = startServers(servers).catch(async (error) => {
  try {
    await tunnelManager.shutdown();
  } catch (shutdownError) {
    throw new AggregateError(
      [error, shutdownError],
      "Ssh_text_attach 服务启动失败且 SSH 隧道清理未完成",
    );
  }
  throw error;
});
await startupPromise;
