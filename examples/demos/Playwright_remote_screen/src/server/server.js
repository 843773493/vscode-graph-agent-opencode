import { createServer } from "node:http";
import { createInterface } from "node:readline";
import { fileURLToPath } from "node:url";
import { WebSocketServer } from "ws";
import { BrowserSession } from "./browserSession.js";
import { FIXED_BROWSER_ID } from "./protocol.js";
import {
  BROWSER_CONTROL_COMMAND_HELP,
  executeBrowserCommand,
  parseBrowserControlCommand,
} from "./browserControlCommands.js";
import { normalizeHttpUrl } from "./url.js";
import { createHttpRequestHandler } from "./httpRoutes.js";
import {
  createViewerSocketHandler,
  proxyCdpWebSocket,
  webSocketUpgradePath,
} from "./webSocketRoutes.js";

const demoRoot = fileURLToPath(new URL("../../", import.meta.url));
const HTTP_SHUTDOWN_GRACE_MS = 1000;
const WEB_SOCKET_SHUTDOWN_GRACE_MS = 1000;

function readOptionValue(name) {
  const index = process.argv.indexOf(name);
  if (index === -1) {
    return null;
  }
  const value = process.argv[index + 1];
  if (!value) {
    throw new Error(`缺少命令行参数 ${name} 的值`);
  }
  return value;
}

function resolvePort() {
  const rawPort = process.env.REMOTE_SCREEN_PORT || readOptionValue("--port") || "8121";
  const port = Number(rawPort);
  if (!Number.isInteger(port) || port <= 0 || port > 65535) {
    throw new Error(`非法端口: ${rawPort}`);
  }
  return port;
}

function resolveHeadless() {
  const raw = process.env.REMOTE_SCREEN_HEADLESS || readOptionValue("--headless");
  if (process.argv.includes("--headed")) {
    return false;
  }
  if (raw === null) {
    return true;
  }
  if (raw === "1" || raw === "true") {
    return true;
  }
  if (raw === "0" || raw === "false") {
    return false;
  }
  throw new Error(`REMOTE_SCREEN_HEADLESS/--headless 必须是 true/false: ${raw}`);
}

function resolveViewport() {
  const rawViewport = process.env.REMOTE_SCREEN_VIEWPORT || readOptionValue("--viewport") || "1920x800";
  const match = rawViewport.match(/^(\d+)x(\d+)$/i);
  if (!match) {
    throw new Error(`viewport 格式必须是 <width>x<height>: ${rawViewport}`);
  }
  const width = Number(match[1]);
  const height = Number(match[2]);
  if (!Number.isInteger(width) || !Number.isInteger(height) || width <= 0 || height <= 0) {
    throw new Error(`非法 viewport: ${rawViewport}`);
  }
  return { width, height };
}

function resolveCdpPort() {
  const rawPort = process.env.REMOTE_SCREEN_CDP_PORT || readOptionValue("--cdp-port") || "9333";
  const port = Number(rawPort);
  if (!Number.isInteger(port) || port <= 0 || port > 65535) {
    throw new Error(`非法 CDP 端口: ${rawPort}`);
  }
  return port;
}

function startBrowserControlConsole(session, shutdown) {
  if (!process.stdin.isTTY) {
    console.log("[remote-screen] stdin 不是 TTY，跳过浏览器控制台");
    return;
  }

  console.log("[remote-screen] 浏览器控制命令可用:");
  for (const browserControlCommand of BROWSER_CONTROL_COMMAND_HELP) {
    console.log(`  ${browserControlCommand}`);
  }

  const readline = createInterface({
    input: process.stdin,
    output: process.stdout,
    terminal: true,
    prompt: "browser> ",
  });
  let closed = false;
  let browserControlCommandChain = Promise.resolve();

  readline.on("line", (line) => {
    browserControlCommandChain = browserControlCommandChain
      .then(async () => {
        const browserControlCommand = parseBrowserControlCommand(line);
        if (browserControlCommand.name === "exit") {
          closed = true;
          readline.close();
          await shutdown();
          return;
        }
        const commandResult = await executeBrowserCommand(session, browserControlCommand);
        if (commandResult.output) {
          console.log(commandResult.output);
        }
      })
      .catch((error) => {
          console.error(`[browser-console] ${error instanceof Error ? error.message : String(error)}`);
      })
      .finally(() => {
        if (!closed) {
          readline.prompt();
        }
      });
  });

  readline.on("close", () => {
    closed = true;
  });
  readline.prompt();
}

async function closeServer(server, httpSockets) {
  if (!server.listening) {
    return;
  }
  const closeOperation = new Promise((resolve, reject) => {
    server.close((error) => {
      if (error) {
        reject(error);
        return;
      }
      resolve();
    });
  });
  const forceCloseTimer = setTimeout(
    () => {
      for (const socket of httpSockets) {
        socket.destroy();
      }
    },
    HTTP_SHUTDOWN_GRACE_MS,
  );
  try {
    await closeOperation;
  } finally {
    clearTimeout(forceCloseTimer);
  }
}

function closeWebSocketServer(webSocketServer) {
  return new Promise((resolve, reject) => {
    const forceCloseTimer = setTimeout(() => {
      for (const socket of webSocketServer.clients) {
        socket.terminate();
      }
    }, WEB_SOCKET_SHUTDOWN_GRACE_MS);

    try {
      webSocketServer.close((error) => {
        clearTimeout(forceCloseTimer);
        if (error) {
          reject(error);
          return;
        }
        resolve();
      });
    } catch (error) {
      clearTimeout(forceCloseTimer);
      reject(error);
      return;
    }

    for (const socket of webSocketServer.clients) {
      socket.close(1001, "remote screen server shutting down");
    }
  });
}

function listen(server, host, port) {
  return new Promise((resolve, reject) => {
    const onError = (error) => {
      server.off("error", onError);
      server.off("listening", onListening);
      reject(error);
    };
    const onListening = () => {
      server.off("error", onError);
      server.off("listening", onListening);
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

async function main() {
  const host = process.env.REMOTE_SCREEN_HOST || readOptionValue("--host") || "0.0.0.0";
  const port = resolvePort();
  const initialUrl = normalizeHttpUrl(process.env.REMOTE_SCREEN_INITIAL_URL || readOptionValue("--url") || "https://www.bilibili.com/");
  const session = new BrowserSession({
    browserId: FIXED_BROWSER_ID,
    initialUrl,
    headless: resolveHeadless(),
    viewport: resolveViewport(),
    cdpHost: "127.0.0.1",
    cdpPort: resolveCdpPort(),
  });
  await session.start();

  const httpSockets = new Set();
  const server = createServer(createHttpRequestHandler(session));
  server.on("connection", (socket) => {
    httpSockets.add(socket);
    socket.once("close", () => httpSockets.delete(socket));
  });
  const webSocketServer = new WebSocketServer({ noServer: true });
  webSocketServer.on("connection", createViewerSocketHandler(session));

  server.on("upgrade", (request, socket, head) => {
    httpSockets.delete(socket);
    const upgradePath = webSocketUpgradePath(request.url);
    if (upgradePath !== "/remote-screen") {
      if (upgradePath.startsWith("/cdp/page/")) {
        const targetId = decodeURIComponent(upgradePath.slice("/cdp/page/".length));
        webSocketServer.handleUpgrade(request, socket, head, (ws) => {
          proxyCdpWebSocket(session, targetId, ws);
        });
        return;
      }
      socket.write("HTTP/1.1 404 Not Found\r\n\r\n");
      socket.destroy();
      return;
    }
    webSocketServer.handleUpgrade(request, socket, head, (ws) => {
      webSocketServer.emit("connection", ws, request);
    });
  });

  let shuttingDown = false;
  async function shutdown() {
    if (shuttingDown) {
      return;
    }
    shuttingDown = true;
    console.log("[remote-screen] 正在关闭...");

    const shutdownErrors = [];
    const [httpCloseResult, webSocketCloseResult] = await Promise.allSettled([
      closeServer(server, httpSockets),
      closeWebSocketServer(webSocketServer),
    ]);
    if (httpCloseResult.status === "rejected") {
      shutdownErrors.push(httpCloseResult.reason);
    }
    if (webSocketCloseResult.status === "rejected") {
      shutdownErrors.push(webSocketCloseResult.reason);
    }
    try {
      await session.close();
    } catch (error) {
      shutdownErrors.push(error);
    }

    if (shutdownErrors.length > 0) {
      console.error(new AggregateError(shutdownErrors, "远程屏幕服务关闭失败"));
      process.exit(1);
    }
    console.log("[remote-screen] 已关闭");
    process.exit(0);
  }

  process.on("SIGINT", () => {
    void shutdown();
  });
  process.on("SIGTERM", () => {
    void shutdown();
  });

  try {
    await listen(server, host, port);
  } catch (error) {
    try {
      await session.close();
    } catch (closeError) {
      throw new AggregateError(
        [error, closeError],
        "远程屏幕 HTTP 服务启动失败，且无法关闭浏览器会话",
      );
    }
    throw error;
  }

  console.log(`[remote-screen] http://${host}:${port}`);
  console.log(`[remote-screen] browserId=${FIXED_BROWSER_ID}`);
  console.log(`[remote-screen] demoRoot=${demoRoot}`);
  console.log(`[remote-screen] initialUrl=${initialUrl}`);
  startBrowserControlConsole(session, shutdown);
}

main().catch((error) => {
  console.error(`[remote-screen] 启动失败: ${error instanceof Error ? error.stack : String(error)}`);
  process.exit(1);
});
