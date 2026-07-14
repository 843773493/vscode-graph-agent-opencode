import http from "node:http";
import path from "node:path";
import { existsSync, statSync } from "node:fs";
import { readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";

const DEFAULT_HOST = "0.0.0.0";
const DEFAULT_PORT = 8013;
const DEFAULT_BACKEND_PORT = 8012;
const DEFAULT_BACKEND_URL = "http://127.0.0.1:8012";
const currentFile = fileURLToPath(import.meta.url);
const currentDir = path.dirname(currentFile);

function parseArgs(argv) {
  const args = new Map();
  for (let index = 0; index < argv.length; index += 1) {
    const arg = argv[index];
    if (!arg.startsWith("--")) {
      continue;
    }
    const key = arg.slice(2);
    const value = argv[index + 1]?.startsWith("--") ? "true" : argv[index + 1];
    args.set(key, value ?? "true");
    if (value && value !== "true") {
      index += 1;
    }
  }
  return args;
}

function contentType(filePath) {
  if (filePath.endsWith(".html")) return "text/html; charset=utf-8";
  if (filePath.endsWith(".js")) return "application/javascript; charset=utf-8";
  if (filePath.endsWith(".css")) return "text/css; charset=utf-8";
  return "application/octet-stream";
}

function send(response, status, body, type = "text/plain; charset=utf-8") {
  response.writeHead(status, {
    "content-type": type,
    "content-length": Buffer.byteLength(body),
  });
  response.end(body);
}

function hostnameFromHostHeader(hostHeader) {
  if (!hostHeader) {
    return "127.0.0.1";
  }
  if (hostHeader.startsWith("[")) {
    return hostHeader.slice(1, hostHeader.indexOf("]"));
  }
  return hostHeader.split(":")[0] || "127.0.0.1";
}

function formatUrlHost(hostname) {
  return hostname.includes(":") && !hostname.startsWith("[")
    ? `[${hostname}]`
    : hostname;
}

function backendUrlForRequest(request, configuredBackendUrl) {
  if (configuredBackendUrl !== "auto") {
    return configuredBackendUrl;
  }
  const hostname = hostnameFromHostHeader(request.headers.host);
  return `http://${formatUrlHost(hostname)}:${DEFAULT_BACKEND_PORT}`;
}

function resolveWorkspaceRoot(args) {
  const raw = args.get("workspace-root") || process.env.BOXTEAM_TERMINAL_WORKSPACE_ROOT || process.env.WORKSPACE_ROOT;
  if (typeof raw !== "string" || raw.trim() === "" || raw === "true") {
    throw new Error(
      "terminal frontend 启动必须显式提供 workspace root："
        + "请传入 --workspace-root、BOXTEAM_TERMINAL_WORKSPACE_ROOT 或 WORKSPACE_ROOT。",
    );
  }
  const resolved = path.resolve(raw);
  if (!existsSync(resolved) || !statSync(resolved).isDirectory()) {
    throw new Error(`--workspace-root 必须指向已存在的目录: ${resolved}`);
  }
  return resolved;
}

function resolveAssetRoot(args) {
  const raw = args.get("asset-root") || process.env.BOXTEAM_PROJECT_ROOT || process.cwd();
  if (typeof raw !== "string" || raw.trim() === "" || raw === "true") {
    throw new Error(
      "terminal frontend 启动必须显式提供 asset root："
        + "请传入 --asset-root、BOXTEAM_PROJECT_ROOT，或从项目根目录启动。",
    );
  }
  const resolved = path.resolve(raw);
  if (!existsSync(resolved) || !statSync(resolved).isDirectory()) {
    throw new Error(`asset root 必须指向已存在的目录: ${resolved}`);
  }
  return resolved;
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  const host = args.get("host") || process.env.BOXTEAM_TERMINAL_FRONTEND_HOST || DEFAULT_HOST;
  const port = Number(args.get("port") || process.env.BOXTEAM_TERMINAL_FRONTEND_PORT || DEFAULT_PORT);
  const backendUrl =
    args.get("backend-url") ||
    process.env.BOXTEAM_TERMINAL_BACKEND_URL ||
    DEFAULT_BACKEND_URL;
  const clientRoot = currentDir;
  const workspaceRoot = resolveWorkspaceRoot(args);
  const assetRoot = resolveAssetRoot(args);

  const server = http.createServer(async (request, response) => {
    try {
      const url = new URL(request.url || "/", `http://${host}:${port}`);
      if (url.pathname === "/health") {
        send(
          response,
          200,
          JSON.stringify({ ok: true, backend_url: backendUrl, workspace_root: workspaceRoot, asset_root: assetRoot }),
          "application/json; charset=utf-8",
        );
        return;
      }

      if (url.pathname === "/config.js") {
        const browserBackendUrl = backendUrlForRequest(request, backendUrl);
        send(
          response,
          200,
          `window.BOXTEAM_TERMINAL_BACKEND_URL = ${JSON.stringify(browserBackendUrl)};\n`,
          "application/javascript; charset=utf-8",
        );
        return;
      }

      const vendorMap = new Map([
        ["/vendor/xterm/xterm.css", path.join(assetRoot, "node_modules", "@xterm", "xterm", "css", "xterm.css")],
        ["/vendor/xterm/xterm.js", path.join(assetRoot, "node_modules", "@xterm", "xterm", "lib", "xterm.js")],
        ["/vendor/xterm/addon-fit.js", path.join(assetRoot, "node_modules", "@xterm", "addon-fit", "lib", "addon-fit.js")],
        ["/vendor/codicon/codicon.css", path.join(assetRoot, "src", "web", "node_modules", "@vscode", "codicons", "dist", "codicon.css")],
        ["/vendor/codicon/codicon.ttf", path.join(assetRoot, "src", "web", "node_modules", "@vscode", "codicons", "dist", "codicon.ttf")],
      ]);
      const vendorPath = vendorMap.get(url.pathname);
      if (vendorPath) {
        const body = await readFile(vendorPath);
        response.writeHead(200, { "content-type": contentType(vendorPath) });
        response.end(body);
        return;
      }

      const fileName = url.pathname === "/" ? "index.html" : path.basename(url.pathname);
      const filePath = path.join(clientRoot, fileName);
      const body = await readFile(filePath);
      response.writeHead(200, { "content-type": contentType(filePath) });
      response.end(body);
    } catch (error) {
      send(
        response,
        404,
        error instanceof Error ? error.message : String(error),
      );
    }
  });

  server.listen(port, host, () => {
    console.log(`[terminal-frontend] listening on http://${host}:${port}`);
    console.log(`[terminal-frontend] backend ${backendUrl}`);
  });
}

await main();
