import { readFile } from "node:fs/promises";
import path from "node:path";

function mimeType(filePath) {
  const ext = path.extname(filePath);
  if (ext === ".html") return "text/html; charset=utf-8";
  if (ext === ".js") return "text/javascript; charset=utf-8";
  if (ext === ".css") return "text/css; charset=utf-8";
  if (ext === ".json") return "application/json; charset=utf-8";
  if (ext === ".svg") return "image/svg+xml";
  return "application/octet-stream";
}

function resolveStaticPath(clientRoot, requestUrl) {
  const url = new URL(requestUrl || "/", "http://127.0.0.1");
  const relativePath = url.pathname === "/" ? "index.html" : decodeURIComponent(url.pathname.slice(1));
  if (relativePath.includes("\0")) {
    throw new Error("静态资源路径包含非法空字符");
  }
  const filePath = path.resolve(clientRoot, relativePath);
  const relativeToClientRoot = path.relative(clientRoot, filePath);
  if (
    relativeToClientRoot === ".."
    || relativeToClientRoot.startsWith(`..${path.sep}`)
    || path.isAbsolute(relativeToClientRoot)
  ) {
    throw new Error(`静态资源路径越界: ${url.pathname}`);
  }
  return filePath;
}

export function createStaticFileHandler(clientRoot, gatewayPort) {
  const normalizedClientRoot = path.resolve(clientRoot);
  return async (request, response) => {
    try {
      const requestUrl = new URL(request.url || "/", "http://127.0.0.1");
      if (requestUrl.pathname === "/health") {
        response.writeHead(200, { "content-type": "application/json; charset=utf-8", "cache-control": "no-store" });
        response.end(JSON.stringify({ ok: true }));
        return;
      }

      if (requestUrl.pathname === "/runtime-config") {
        response.writeHead(200, { "content-type": "application/json; charset=utf-8", "cache-control": "no-store" });
        response.end(JSON.stringify({ gatewayPort }));
        return;
      }

      const filePath = resolveStaticPath(normalizedClientRoot, request.url);
      const content = await readFile(filePath);
      response.writeHead(200, { "content-type": mimeType(filePath), "cache-control": "no-store" });
      response.end(content);
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      const statusCode = error?.code === "ENOENT" ? 404 : 500;
      console.error(`[frontend] ${request.url} ${statusCode}: ${message}`);
      if (response.destroyed) {
        return;
      }
      if (response.headersSent) {
        response.destroy();
        return;
      }
      response.writeHead(statusCode, { "content-type": "text/plain; charset=utf-8" });
      response.end(message);
    }
  };
}
