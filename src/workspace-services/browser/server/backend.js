import http from "node:http";
import { randomUUID } from "node:crypto";
import { createReadStream } from "node:fs";
import { stat } from "node:fs/promises";
import { stripVTControlCharacters } from "node:util";
import { WebSocket, WebSocketServer } from "ws";
import { BrowserFrameFlow } from "./browserFrameFlow.js";
import { BrowserManager, resolveRequiredWorkspaceRoot } from "./browserManager.js";
import { LatestPointerMoveDispatcher } from "./latestPointerMoveDispatcher.js";
import { encodeServerMessage, parseClientMessage } from "./protocol.js";

const DEFAULT_HOST = "127.0.0.1";
const DEFAULT_PORT = 8015;
const encodedFrames = new WeakMap();

function encodeBinaryFrame(frame) {
  const metadata = Buffer.from(JSON.stringify({
    type: "frame",
    frameId: frame.frameId,
    browserId: frame.browserId,
    pageId: frame.pageId,
    width: frame.width,
    height: frame.height,
    pixelWidth: frame.pixelWidth || frame.width,
    pixelHeight: frame.pixelHeight || frame.height,
    pageScaleFactor: frame.pageScaleFactor,
    timestamp: frame.timestamp,
  }), "utf8");
  const header = Buffer.allocUnsafe(4);
  header.writeUInt32BE(metadata.byteLength, 0);
  return Buffer.concat([header, metadata, frame.jpeg]);
}

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
    "access-control-allow-methods": "GET,POST,PATCH,DELETE,OPTIONS",
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

async function sendDownload(response, download) {
  const fileStat = await stat(download.path);
  response.writeHead(200, {
    "content-type": "application/octet-stream",
    "content-length": fileStat.size,
    "content-disposition": `attachment; filename*=UTF-8''${encodeURIComponent(download.filename)}`,
    "access-control-allow-origin": "*",
  });
  createReadStream(download.path).pipe(response);
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

function isAgentRequest(request) {
  return String(request.headers["x-boxteam-actor"] || "").startsWith("agent");
}

function assertAgentAccessAllowed(request, browser) {
  if (isAgentRequest(request)) {
    browser.assertAgentAccessAllowed();
  }
}

function requestActor(request) {
  const actor = String(request.headers["x-boxteam-actor"] || "").trim();
  return actor || "user:http";
}

async function runHttpOperation(request, browser, action, callback, { visible = true } = {}) {
  assertAgentAccessAllowed(request, browser);
  return await browser.enqueueOperation(
    { actor: requestActor(request), action, visible },
    callback,
  );
}

function wsClient(socket) {
  const frameFlow = new BrowserFrameFlow({
    socket,
    encodeFrame(frame) {
      let encoded = encodedFrames.get(frame);
      if (!encoded) {
        encoded = encodeBinaryFrame(frame);
        encodedFrames.set(frame, encoded);
      }
      return encoded;
    },
  });
  return {
    participantId: `user_${randomUUID().replaceAll("-", "")}`,
    kind: "user",
    connectedAt: new Date().toISOString(),
    sendJson(message) {
      if (socket.readyState === WebSocket.OPEN) {
        socket.send(encodeServerMessage(message));
      }
    },
    sendFrame(frame) {
      return frameFlow.offer(frame);
    },
    acknowledgeFrame(frameId, metrics) {
      return frameFlow.acknowledge(frameId, metrics);
    },
    resetFrameFlow() {
      frameFlow.reset();
    },
    closeFrameFlow() {
      frameFlow.close();
    },
    frameFlowSnapshot() {
      return frameFlow.snapshot();
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
  manager.startResourceGovernor({
    policy: {
      ...(process.env.BOXTEAM_BROWSER_IDLE_FREEZE_MS
        ? { idleFreezeMs: parsePositiveInt(process.env.BOXTEAM_BROWSER_IDLE_FREEZE_MS, "BOXTEAM_BROWSER_IDLE_FREEZE_MS") }
        : {}),
    },
  });

  const server = http.createServer(async (request, response) => {
    response.setHeader("access-control-allow-origin", "*");
    response.setHeader("access-control-allow-methods", "GET,POST,PATCH,DELETE,OPTIONS");
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
          process_id: process.pid,
          workspace_root: workspaceRoot,
          browser_count: manager.list().length,
          browser_runtime: manager.runtimePool.snapshot(),
          resource_governor: manager.resourceGovernorSnapshot(),
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
          deviceProfile: payload.device_profile,
          deviceOrientation: payload.device_orientation,
        });
        sendJson(response, 200, { data: browser });
        return;
      }

      const downloadMatch = pathname.match(/^\/api\/browsers\/([^/]+)\/downloads\/([^/]+)$/);
      if (request.method === "GET" && downloadMatch) {
        await sendDownload(response, manager.download(downloadMatch[1], downloadMatch[2]));
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
          const browser = manager.get(browserId);
          sendJson(response, 200, {
            data: await runHttpOperation(
              request,
              browser,
              "read",
              () => browser.readSummary(),
              { visible: false },
            ),
          });
          return;
        }

        if (request.method === "PATCH" && action === "agent-lock") {
          const payload = await readJson(request);
          sendJson(response, 200, {
            data: await manager.setAgentAccessLocked(browserId, payload.locked, payload.owner_id),
          });
          return;
        }

        if (request.method === "PATCH" && action === "resource-policy") {
          const payload = await readJson(request);
          const browser = manager.get(browserId);
          sendJson(response, 200, {
            data: await browser.setResourcePolicy(payload.policy),
          });
          return;
        }

        if (request.method === "PATCH" && action === "device-profile") {
          const payload = await readJson(request);
          const browser = manager.get(browserId);
          sendJson(response, 200, {
            data: await runHttpOperation(
              request,
              browser,
              "device-profile",
              () => browser.setDeviceProfile(
                payload.profile_id || payload.device_profile,
                payload.orientation || payload.device_orientation,
              ),
            ),
          });
          return;
        }

        if (request.method === "POST" && action === "freeze") {
          const browser = manager.get(browserId);
          sendJson(response, 200, { data: await browser.freeze({ reason: "manual" }) });
          return;
        }

        if (request.method === "POST" && action === "wake") {
          const browser = manager.get(browserId);
          sendJson(response, 200, { data: await browser.wake({ reason: "manual" }) });
          return;
        }

        if (request.method === "POST" && action === "discard") {
          const browser = manager.get(browserId);
          sendJson(response, 200, { data: await browser.discard({ reason: "manual" }) });
          return;
        }

        if (request.method === "POST" && action === "navigate") {
          const payload = await readJson(request);
          const browser = manager.get(browserId);
          sendJson(response, 200, {
            data: await runHttpOperation(
              request,
              browser,
              `navigate:${payload.type || "url"}`,
              () => {
                const type = payload.type || "url";
                if (type === "new_tab") {
                  return browser.createPage(payload.url);
                }
                if (type === "activate_tab") {
                  return browser.activatePage(payload.tab_id);
                }
                if (type === "close_tab") {
                  return browser.closePage(payload.tab_id);
                }
                return browser.navigate(type, payload.url);
              },
            ),
          });
          return;
        }

        if (request.method === "POST" && action === "click") {
          const browser = manager.get(browserId);
          const payload = await readJson(request);
          sendJson(response, 200, {
            data: await runHttpOperation(request, browser, "click", () => browser.click(payload)),
          });
          return;
        }

        if (request.method === "POST" && action === "hover") {
          const browser = manager.get(browserId);
          const payload = await readJson(request);
          sendJson(response, 200, {
            data: await runHttpOperation(request, browser, "hover", () => browser.hover(payload)),
          });
          return;
        }

        if (request.method === "POST" && action === "type") {
          const browser = manager.get(browserId);
          const payload = await readJson(request);
          sendJson(response, 200, {
            data: await runHttpOperation(request, browser, "type", () => browser.typeInPage(payload)),
          });
          return;
        }

        if (request.method === "POST" && action === "drag") {
          const browser = manager.get(browserId);
          const payload = await readJson(request);
          sendJson(response, 200, {
            data: await runHttpOperation(request, browser, "drag", () => browser.drag(payload)),
          });
          return;
        }

        if (request.method === "POST" && action === "dialog") {
          const browser = manager.get(browserId);
          const payload = await readJson(request);
          sendJson(response, 200, {
            data: await runHttpOperation(request, browser, "dialog", () => browser.handleDialog(payload)),
          });
          return;
        }

        if (request.method === "POST" && action === "screenshot") {
          const browser = manager.get(browserId);
          const payload = await readJson(request);
          sendJson(response, 200, {
            data: await runHttpOperation(request, browser, "screenshot", () => browser.screenshot(payload)),
          });
          return;
        }

        if (request.method === "POST" && action === "run") {
          const browser = manager.get(browserId);
          const payload = await readJson(request);
          sendJson(response, 200, {
            data: await runHttpOperation(request, browser, "run", () => browser.runPlaywrightCode(payload)),
          });
          return;
        }

        if (request.method === "POST" && action === "close") {
          const browser = manager.get(browserId);
          sendJson(response, 200, {
            data: await runHttpOperation(request, browser, "close", () => manager.close(browserId)),
          });
          return;
        }

        if (request.method === "DELETE" && !action) {
          const browser = manager.get(browserId);
          assertAgentAccessAllowed(request, browser);
          const canDeleteWithoutPageOperation = browser.status !== "running"
            || ["frozen", "discarded"].includes(browser.record.resource_state);
          sendJson(response, 200, {
            data: canDeleteWithoutPageOperation
              ? await manager.delete(browserId)
              : await runHttpOperation(request, browser, "delete", () => manager.delete(browserId)),
          });
          return;
        }
      }

      notFound(response);
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      const status = message.startsWith("浏览器页面不存在")
        ? 404
        : message.startsWith("浏览器下载不存在")
          ? 404
        : error?.code === "browser_stale_element_ref"
          ? 409
        : [
            "browser_resource_protected",
            "browser_resource_protected_during_transition",
            "browser_must_be_frozen_before_discard",
            "browser_session_logical_limit_exceeded",
            "browser_closing",
            "browser_checkpoint_index_changed_externally",
          ].includes(error?.code)
          ? 409
        : error?.code === "browser_checkpoint_too_large"
          ? 413
        : error?.code === "browser_checkpoint_workspace_quota_exceeded"
          ? 507
        : error?.code === "browser_creation_paused_memory_pressure"
          ? 503
        : error?.code === "browser_agent_access_locked"
          ? 423
          : error?.code === "browser_agent_lock_owned_by_another_user"
            ? 423
          : 500;
      sendJson(response, status, {
        error: message,
        ...(error?.code ? { code: error.code } : {}),
      });
    }
  });

  const wss = new WebSocketServer({ server, path: "/browser" });
  wss.on("connection", (socket) => {
    const client = wsClient(socket);
    let attachedBrowser = null;
    let queued = Promise.resolve();

    const onFrame = (frame) => client.sendFrame(frame);
    const onState = (state) => client.sendJson({ type: "state", browserId: state.browser_id, state });
    const onParticipantPointer = (pointer) => {
      if (pointer.participantId !== client.participantId) {
        client.sendJson({
          type: "participantPointer",
          browserId: attachedBrowser?.id,
          pointer,
        });
      }
    };

    async function detach(sendDetached) {
      if (!attachedBrowser) {
        client.resetFrameFlow();
        return;
      }
      const browser = attachedBrowser;
      attachedBrowser = null;
      browser.off("frame", onFrame);
      browser.off("state", onState);
      browser.off("participant-pointer", onParticipantPointer);
      client.resetFrameFlow();
      const state = await browser.detachClient(client);
      if (sendDetached) {
        client.sendJson({ type: "detached", browserId: browser.id, state });
      }
    }

    async function runUserOperation(
      message,
      action,
      callback,
      { visible = true, interactive = false } = {},
    ) {
      const operation = { actor: `user:${client.participantId}`, action, visible };
      const result = interactive
        ? await attachedBrowser.runInteractiveOperation(operation, callback)
        : await attachedBrowser.enqueueOperation(operation, callback);
      if (message.clientOperationId) {
        client.sendJson({
          type: "operationAck",
          browserId: attachedBrowser.id,
          clientOperationId: message.clientOperationId,
          operation: result.operation,
        });
      }
      return result;
    }

    async function handleMessage(message) {
      if (message.type === "attach") {
        await detach(false);
        if (message.participantId) {
          client.participantId = message.participantId;
        }
        attachedBrowser = manager.get(message.browserId);
        attachedBrowser.on("frame", onFrame);
        attachedBrowser.on("state", onState);
        attachedBrowser.on("participant-pointer", onParticipantPointer);
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
        await runUserOperation(
          message,
          `pointer:${message.action}`,
          () => attachedBrowser.dispatchPointer(message),
          { visible: false, interactive: true },
        );
        attachedBrowser.emit("participant-pointer", {
          participantId: client.participantId,
          x: message.x,
          y: message.y,
          action: message.action,
        });
        return;
      }
      if (message.type === "key") {
        await runUserOperation(
          message,
          `key:${message.action}`,
          () => attachedBrowser.dispatchKey(message),
          { visible: false, interactive: true },
        );
        return;
      }
      if (message.type === "paste") {
        await runUserOperation(
          message,
          "insertText",
          () => attachedBrowser.insertText(message.text),
          { visible: false, interactive: true },
        );
        return;
      }
      if (message.type === "handleDialog") {
        await runUserOperation(message, "dialog", () => attachedBrowser.handleDialog({
            acceptModal: message.accept,
            promptText: message.promptText,
          }));
        return;
      }
      if (message.type === "selectFiles") {
        await runUserOperation(
          message,
          "selectFiles",
          () => attachedBrowser.handleDialog({ filePayloads: message.files }),
        );
        return;
      }
      if (message.type === "readClipboard") {
        const output = await runUserOperation(
          message,
          "readClipboard",
          () => attachedBrowser.readClipboardText(),
          { visible: false, interactive: true },
        );
        client.sendJson({
          type: "clipboardText",
          browserId: attachedBrowser.id,
          text: output.result,
        });
        return;
      }
      if (message.type === "inspectElement" || message.type === "selectElement") {
        const output = await runUserOperation(
          message,
          message.type,
          () => attachedBrowser.inspectElement({
            x: Number(message.x),
            y: Number(message.y),
          }),
          { visible: false, interactive: true },
        );
        const element = Object.hasOwn(output, "result")
          ? output.result
          : Object.fromEntries(
              Object.entries(output).filter(([key]) => !["operation", "operation_revision"].includes(key)),
            );
        client.sendJson({
          type: message.type === "selectElement" ? "elementSelected" : "elementHovered",
          browserId: attachedBrowser.id,
          mode: message.mode === "rich" ? "rich" : "basic",
          element,
        });
        return;
      }
      if (message.type === "viewport") {
        await runUserOperation(message, "viewport", () => attachedBrowser.setViewport(
            parsePositiveInt(message.width, "width"),
            parsePositiveInt(message.height, "height"),
          ));
        return;
      }
      if (message.type === "deviceProfile") {
        await runUserOperation(
          message,
          "device-profile",
          () => attachedBrowser.setDeviceProfile(message.profileId, message.orientation),
        );
        return;
      }
      if (message.type === "deviceSettings") {
        await runUserOperation(
          message,
          "device-settings",
          () => attachedBrowser.setDeviceSettings(message.settings || {}),
        );
        return;
      }
      if (message.type === "saveDevicePreset") {
        await runUserOperation(
          message,
          "save-device-preset",
          () => attachedBrowser.saveDevicePreset(message.name),
          { visible: false, interactive: true },
        );
        return;
      }
      if (message.type === "debugSnapshot") {
        const output = await runUserOperation(
          message,
          "debug-snapshot",
          () => attachedBrowser.debugSnapshot(),
          { visible: false, interactive: true },
        );
        const data = Object.hasOwn(output, "result")
          ? output.result
          : Object.fromEntries(
              Object.entries(output).filter(([key]) => !["operation", "operation_revision"].includes(key)),
            );
        client.sendJson({
          type: "debugSnapshot",
          browserId: attachedBrowser.id,
          data,
        });
        return;
      }
      if (message.type === "captureScreenshot") {
        const output = await runUserOperation(
          message,
          "capture-screenshot",
          async () => ({ result: await attachedBrowser.captureClientScreenshot() }),
          { visible: false, interactive: true },
        );
        client.sendJson({
          type: "screenshotResult",
          browserId: attachedBrowser.id,
          mimeType: "image/png",
          data: output.result.toString("base64"),
        });
        return;
      }
      if (message.type === "command") {
        const state = await runUserOperation(message, `command:${message.name}`, () => {
          if (message.name === "stop") {
            return attachedBrowser.stopLoading();
          }
          if (message.name === "newPage") {
            return attachedBrowser.createPage(message.url);
          }
          if (message.name === "activatePage") {
            return attachedBrowser.activatePage(message.pageId);
          }
          if (message.name === "closePage") {
            return attachedBrowser.closePage(message.pageId);
          }
          if (message.name === "find") {
            return attachedBrowser.findText(message.query, message.backwards === true);
          }
          if (message.name === "clearNetwork") {
            return attachedBrowser.clearNetworkRequests();
          }
          return attachedBrowser.navigate(message.name === "goto" ? "url" : message.name, message.url);
        });
        client.sendJson({ type: "commandResult", browserId: attachedBrowser.id, output: "命令已完成", state });
        return;
      }
      throw new Error(`未知 WebSocket 消息类型: ${message.type}`);
    }

    function reportMessageError(message, error) {
      const detail = stripVTControlCharacters(
        error instanceof Error ? (error.stack || error.message) : String(error),
      );
      console.error(
        `[browser-backend] WebSocket 操作失败: browser_id=${attachedBrowser?.id || message.browserId || "unknown"} participant_id=${client.participantId} type=${message.type} command=${message.name || "-"}\n${detail}`,
      );
      const visibleMessage = attachedBrowser?.snapshot().navigation_error?.message
        || (error instanceof Error ? error.message : String(error));
      client.sendJson({
        type: "error",
        message: visibleMessage,
        ...(error?.code ? { code: error.code } : {}),
      });
    }

    const pointerMoves = new LatestPointerMoveDispatcher(async (message) => {
      try {
        await handleMessage(message);
      } catch (error) {
        reportMessageError(message, error);
      }
    });

    function enqueueMessage(message) {
      queued = queued
        .then(() => pointerMoves.flush())
        .then(() => handleMessage(message))
        .catch((error) => reportMessageError(message, error));
    }

    socket.on("message", (raw) => {
      let message;
      try {
        message = parseClientMessage(raw);
      } catch (error) {
        client.sendJson({
          type: "error",
          message: error instanceof Error ? error.message : String(error),
        });
        return;
      }
      if (message.type === "frameAck") {
        if (!attachedBrowser || attachedBrowser.id !== message.browserId) {
          return;
        }
        client.acknowledgeFrame(message.frameId, {
          decodeMs: message.decodeMs,
          drawMs: message.drawMs,
        });
        return;
      }
      if (message.type === "pointer"
        && message.action === "move"
        && !message.clientOperationId) {
        pointerMoves.offer(message);
        return;
      }
      enqueueMessage(message);
    });

    socket.on("close", () => {
      pointerMoves.close();
      client.closeFrameFlow();
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
    let resolvedExitCode = exitCode;
    try {
      await manager.shutdown(reason);
    } catch (shutdownError) {
      resolvedExitCode = 1;
      console.error(
        "[browser-backend] 关闭检查点失败:",
        shutdownError instanceof Error ? (shutdownError.stack ?? shutdownError.message) : String(shutdownError),
      );
    }
    wss.close();
    await closeHttpServer(server);
    process.exit(resolvedExitCode);
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
