import http from "node:http";
import { WebSocket, WebSocketServer } from "ws";
import { BrowserManager, resolveRequiredWorkspaceRoot } from "./browserManager.js";
import { encodeServerMessage, parseClientMessage } from "./protocol.js";

const DEFAULT_HOST = "127.0.0.1";
const DEFAULT_PORT = 8015;

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

function sendJson(response, status, data) {
  const body = JSON.stringify(data);
  response.writeHead(status, {
    "content-type": "application/json; charset=utf-8",
    "content-length": Buffer.byteLength(body),
    "access-control-allow-origin": "*",
    "access-control-allow-methods": "GET,POST,DELETE,OPTIONS",
    "access-control-allow-headers": "content-type",
  });
  response.end(body);
}

async function readJson(request) {
  const chunks = [];
  for await (const chunk of request) {
    chunks.push(chunk);
  }
  const raw = Buffer.concat(chunks).toString("utf8");
  if (!raw.trim()) {
    return {};
  }
  return JSON.parse(raw);
}

function normalizePathname(request) {
  const url = new URL(request.url || "/", "http://127.0.0.1");
  return { url, pathname: decodeURIComponent(url.pathname) };
}

function notFound(response) {
  sendJson(response, 404, { error: "not_found" });
}

function missingBrowserSnapshot(manager, browserId) {
  const timestamp = new Date().toISOString();
  return {
    browser_id: browserId,
    page_id: browserId,
    session_id: "",
    title: "Deleted Browser",
    url: "",
    viewport: { width: 1280, height: 800 },
    status: "deleted",
    created_at: timestamp,
    updated_at: timestamp,
    started_at: null,
    ended_at: timestamp,
    client_count: 0,
    sequence: 0,
    attach_url: manager.attachUrl(browserId),
  };
}

function parsePositiveInt(value, fieldName) {
  const numberValue = Number(value);
  if (!Number.isInteger(numberValue) || numberValue <= 0) {
    throw new Error(`${fieldName} 必须是正整数`);
  }
  return numberValue;
}

function wsClient(socket) {
  return {
    sendJson(message) {
      if (socket.readyState === WebSocket.OPEN) {
        socket.send(encodeServerMessage(message));
      }
    },
  };
}

async function closeHttpServer(server) {
  await new Promise((resolve, reject) => {
    server.close((error) => {
      if (error) {
        reject(error);
        return;
      }
      resolve();
    });
  });
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  const host = args.get("host") || process.env.BOXTEAM_BROWSER_HOST || DEFAULT_HOST;
  const port = Number(args.get("port") || process.env.BOXTEAM_BROWSER_BACKEND_PORT || DEFAULT_PORT);
  const workspaceRoot = resolveRequiredWorkspaceRoot(args);
  const browserFrontendBaseUrl =
    args.get("frontend-url") ||
    process.env.BOXTEAM_BROWSER_FRONTEND_URL ||
    "http://127.0.0.1:8016";

  const manager = new BrowserManager({
    workspaceRoot,
    browserFrontendBaseUrl,
  });
  await manager.init();

  const server = http.createServer(async (request, response) => {
    response.setHeader("access-control-allow-origin", "*");
    response.setHeader("access-control-allow-methods", "GET,POST,DELETE,OPTIONS");
    response.setHeader("access-control-allow-headers", "content-type");
    if (request.method === "OPTIONS") {
      response.writeHead(204);
      response.end();
      return;
    }

    try {
      const { url, pathname } = normalizePathname(request);
      if (request.method === "GET" && pathname === "/health") {
        sendJson(response, 200, {
          ok: true,
          workspace_root: workspaceRoot,
          browser_count: manager.list().length,
        });
        return;
      }

      if (request.method === "GET" && pathname === "/api/browsers") {
        sendJson(response, 200, {
          data: manager.list({ sessionId: url.searchParams.get("session_id") }),
        });
        return;
      }

      if (request.method === "POST" && pathname === "/api/browsers") {
        const payload = await readJson(request);
        const browser = await manager.create({
          sessionId: payload.session_id,
          title: payload.title,
          url: payload.url,
          viewport: payload.viewport,
        });
        sendJson(response, 200, { data: browser });
        return;
      }

      const browserMatch = pathname.match(/^\/api\/browsers\/([^/]+)(?:\/([^/]+))?$/);
      if (browserMatch) {
        const browserId = browserMatch[1];
        const action = browserMatch[2] || "";

        if (request.method === "GET" && !action) {
          try {
            sendJson(response, 200, { data: manager.get(browserId).snapshot() });
          } catch (error) {
            const message = error instanceof Error ? error.message : String(error);
            if (
              message.startsWith("浏览器页面不存在") &&
              url.searchParams.get("missing_as_deleted") === "1"
            ) {
              sendJson(response, 200, { data: missingBrowserSnapshot(manager, browserId) });
              return;
            }
            throw error;
          }
          return;
        }

        if (request.method === "GET" && action === "read") {
          sendJson(response, 200, { data: await manager.get(browserId).readSummary() });
          return;
        }

        if (request.method === "POST" && action === "navigate") {
          const payload = await readJson(request);
          sendJson(response, 200, {
            data: await manager.get(browserId).navigate(payload.type || "url", payload.url),
          });
          return;
        }

        if (request.method === "POST" && action === "click") {
          sendJson(response, 200, { data: await manager.get(browserId).click(await readJson(request)) });
          return;
        }

        if (request.method === "POST" && action === "hover") {
          sendJson(response, 200, { data: await manager.get(browserId).hover(await readJson(request)) });
          return;
        }

        if (request.method === "POST" && action === "type") {
          sendJson(response, 200, { data: await manager.get(browserId).typeInPage(await readJson(request)) });
          return;
        }

        if (request.method === "POST" && action === "drag") {
          sendJson(response, 200, { data: await manager.get(browserId).drag(await readJson(request)) });
          return;
        }

        if (request.method === "POST" && action === "dialog") {
          sendJson(response, 200, { data: await manager.get(browserId).handleDialog(await readJson(request)) });
          return;
        }

        if (request.method === "POST" && action === "screenshot") {
          sendJson(response, 200, { data: await manager.get(browserId).screenshot(await readJson(request)) });
          return;
        }

        if (request.method === "POST" && action === "run") {
          sendJson(response, 200, { data: await manager.get(browserId).runPlaywrightCode(await readJson(request)) });
          return;
        }

        if (request.method === "POST" && action === "close") {
          sendJson(response, 200, { data: await manager.close(browserId) });
          return;
        }

        if (request.method === "DELETE" && !action) {
          sendJson(response, 200, { data: await manager.delete(browserId) });
          return;
        }
      }

      notFound(response);
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      const status = message.startsWith("浏览器页面不存在") ? 404 : 500;
      sendJson(response, status, { error: message });
    }
  });

  const wss = new WebSocketServer({ server, path: "/browser" });
  wss.on("connection", (socket) => {
    const client = wsClient(socket);
    let attachedBrowser = null;
    let queued = Promise.resolve();

    const onFrame = (frame) => client.sendJson({ type: "frame", ...frame });
    const onState = (state) => client.sendJson({ type: "state", browserId: state.browser_id, state });

    async function detach(sendDetached) {
      if (!attachedBrowser) {
        return;
      }
      const browser = attachedBrowser;
      attachedBrowser = null;
      browser.off("frame", onFrame);
      browser.off("state", onState);
      const state = await browser.detachClient(client);
      if (sendDetached) {
        client.sendJson({ type: "detached", browserId: browser.id, state });
      }
    }

    async function handleMessage(raw) {
      const message = parseClientMessage(raw);
      if (message.type === "attach") {
        await detach(false);
        attachedBrowser = manager.get(message.browserId);
        attachedBrowser.on("frame", onFrame);
        attachedBrowser.on("state", onState);
        const state = await attachedBrowser.attachClient(client);
        client.sendJson({ type: "attached", browserId: message.browserId, state });
        return;
      }

      if (message.type === "detach") {
        await detach(true);
        return;
      }

      if (!attachedBrowser) {
        throw new Error("尚未 attach 到浏览器页面");
      }

      if (message.type === "pointer") {
        await attachedBrowser.dispatchPointer(message);
        return;
      }
      if (message.type === "key") {
        await attachedBrowser.dispatchKey(message);
        return;
      }
      if (message.type === "paste") {
        await attachedBrowser.insertText(message.text);
        return;
      }
      if (message.type === "viewport") {
        await attachedBrowser.setViewport(
          parsePositiveInt(message.width, "width"),
          parsePositiveInt(message.height, "height"),
        );
        return;
      }
      if (message.type === "command") {
        const state = message.name === "stop"
          ? await attachedBrowser.stopLoading()
          : await attachedBrowser.navigate(message.name === "goto" ? "url" : message.name, message.url);
        client.sendJson({ type: "commandResult", browserId: attachedBrowser.id, output: "命令已完成", state });
        return;
      }
      throw new Error(`未知 WebSocket 消息类型: ${message.type}`);
    }

    socket.on("message", (raw) => {
      queued = queued
        .then(() => handleMessage(raw))
        .catch((error) => {
          client.sendJson({
            type: "error",
            message: error instanceof Error ? error.message : String(error),
          });
        });
    });

    socket.on("close", () => {
      void detach(false).catch((error) => {
        console.error(
          "[browser-backend] WebSocket 关闭时 detach 失败:",
          error instanceof Error ? (error.stack ?? error.message) : String(error),
        );
      });
    });
  });

  server.listen(port, host, () => {
    console.log(`[browser-backend] listening on http://${host}:${port}`);
    console.log(`[browser-backend] workspace ${workspaceRoot}`);
  });

  let shuttingDown = false;
  const shutdown = async (reason, exitCode, error = null) => {
    if (shuttingDown) {
      return;
    }
    shuttingDown = true;
    if (error) {
      console.error(error);
    }
    for (const client of wss.clients) {
      client.close(1001, "browser manager shutting down");
    }
    await manager.shutdown(reason);
    wss.close();
    await closeHttpServer(server);
    process.exit(exitCode);
  };

  process.once("SIGINT", () => void shutdown("browser_manager_sigint", 130));
  process.once("SIGTERM", () => void shutdown("browser_manager_sigterm", 143));
  process.once("SIGHUP", () => void shutdown("browser_manager_sighup", 129));
  process.once("uncaughtException", (error) => {
    void shutdown("browser_manager_uncaught_exception", 1, error);
  });
  process.once("unhandledRejection", (reason) => {
    const error = reason instanceof Error ? reason : new Error(String(reason));
    void shutdown("browser_manager_unhandled_rejection", 1, error);
  });
}

await main();
