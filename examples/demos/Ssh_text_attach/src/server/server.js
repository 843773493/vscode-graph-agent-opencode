import { createServer } from "node:http";
import { fileURLToPath } from "node:url";
import { loadTargetsConfig, resolveRuntimeConfig } from "./config.js";
import { TextFileStore } from "./fileStore.js";
import { HttpError, readJsonRequest, readRawRequest, requestUrl, requireMethod, writeJson } from "./http.js";
import { createStaticFileHandler } from "./staticFiles.js";
import { TargetFileClient } from "./targetFileClient.js";
import { SshTunnelManager } from "./tunnel.js";

const clientRoot = fileURLToPath(new URL("../client/", import.meta.url));
const PROXY_PREFIX = "/api/proxy/";
const HOP_BY_HOP_HEADERS = new Set([
  "connection",
  "content-length",
  "expect",
  "host",
  "keep-alive",
  "proxy-authenticate",
  "proxy-authorization",
  "proxy-connection",
  "te",
  "trailer",
  "transfer-encoding",
  "upgrade",
]);

function parseProxyPath(url) {
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

function proxyRequestHeaders(request) {
  const headers = {};
  for (const [name, value] of Object.entries(request.headers)) {
    const lowerName = name.toLowerCase();
    if (HOP_BY_HOP_HEADERS.has(lowerName)) {
      continue;
    }
    if (value === undefined) {
      continue;
    }
    headers[name] = Array.isArray(value) ? value.join(", ") : value;
  }
  return headers;
}

function proxyResponseHeaders(upstreamResponse) {
  const headers = {
    "access-control-allow-origin": "*",
    "access-control-allow-methods": "GET, POST, PUT, PATCH, DELETE, OPTIONS",
    "access-control-allow-headers": "content-type",
    "cache-control": "no-store",
  };
  for (const [name, value] of upstreamResponse.headers) {
    const lowerName = name.toLowerCase();
    if (HOP_BY_HOP_HEADERS.has(lowerName)) {
      continue;
    }
    headers[name] = value;
  }
  return headers;
}

async function proxyTargetRequest(targetClient, proxyInfo, request, response, originalUrl) {
  const origin = await targetClient.backendOriginFor(proxyInfo.targetId);
  const upstreamUrl = new URL(`${proxyInfo.backendPath}${originalUrl.search}`, origin);
  const hasBody = request.method !== "GET" && request.method !== "HEAD";
  const body = hasBody ? await readRawRequest(request) : undefined;
  const upstreamResponse = await fetch(upstreamUrl, {
    method: request.method,
    headers: proxyRequestHeaders(request),
    body,
  });
  const responseBody = Buffer.from(await upstreamResponse.arrayBuffer());
  response.writeHead(upstreamResponse.status, proxyResponseHeaders(upstreamResponse));
  response.end(responseBody);
}

async function handleGatewayRequest(targetClient, request, response) {
  try {
    if (request.method === "OPTIONS") {
      writeJson(response, 204, {});
      return;
    }

    const url = requestUrl(request);
    const proxyInfo = parseProxyPath(url);
    if (proxyInfo) {
      await proxyTargetRequest(targetClient, proxyInfo, request, response, url);
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
    writeJson(response, statusCode, { error: message });
  }
}

function listen(server, host, port, label) {
  return new Promise((resolve, reject) => {
    const onError = (error) => {
      reject(error);
    };
    server.once("error", onError);
    server.listen(port, host, () => {
      server.off("error", onError);
      console.log(`[${label}] http://${host}:${port}`);
      resolve();
    });
  });
}

function closeServer(server) {
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

const runtimeConfig = resolveRuntimeConfig();
const servers = [];
const tunnelManager = new SshTunnelManager();

if (runtimeConfig.roles.includes("backend")) {
  const localStore = new TextFileStore({
    filePath: runtimeConfig.dataFilePath,
    label: runtimeConfig.dataLabel,
  });
  await localStore.ensureReady();
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
  const targetsConfig = await loadTargetsConfig(runtimeConfig.targetsConfigPath, runtimeConfig.projectRoot);
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
  const frontendServer = createServer(createStaticFileHandler(clientRoot));
  servers.push({
    label: "frontend",
    server: frontendServer,
    host: runtimeConfig.frontendHost,
    port: runtimeConfig.frontendPort,
  });
}

process.once("SIGINT", async () => {
  console.log("[shutdown] 收到 SIGINT");
  tunnelManager.closeAll();
  await Promise.all(servers.map((entry) => closeServer(entry.server)));
  process.exit(0);
});

process.once("SIGTERM", async () => {
  console.log("[shutdown] 收到 SIGTERM");
  tunnelManager.closeAll();
  await Promise.all(servers.map((entry) => closeServer(entry.server)));
  process.exit(0);
});

await Promise.all(servers.map((entry) => listen(entry.server, entry.host, entry.port, entry.label)));
