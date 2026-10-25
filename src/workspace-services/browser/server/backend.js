import http from "node:http";
import { randomUUID } from "node:crypto";
import { open } from "node:fs/promises";
import { pipeline } from "node:stream/promises";
import { stripVTControlCharacters } from "node:util";
import { WebSocket, WebSocketServer } from "ws";
import { BrowserFrameFlow } from "./transport/browserFrameFlow.js";
import {
  BrowserManager,
  BrowserTransportGrantRejected,
  resolveRequiredWorkspaceRoot,
} from "./browserManager.js";
import { validateWorkspaceId } from "./workspaceIdentity.js";
import { LatestPointerMoveDispatcher } from "./operations/latestPointerMoveDispatcher.js";
import { encodeBinaryFrame, encodeServerMessage, parseClientMessage } from "./transport/protocol.js";

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
    "access-control-allow-methods": "GET,POST,PATCH,DELETE,OPTIONS",
    "access-control-allow-headers": "content-type,x-boxteam-workspace-id",
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

async function sendDownload(response, file, { browserId, artifactId, kind, mediaType = "application/octet-stream" }) {
  let handle;
  let stage = "open";
  let failure = null;
  const artifactFailure = (cause, failedStage) => {
    console.error("[browser-backend] 读取浏览器产物失败", {
      browser_id: browserId, artifact_id: artifactId, kind, stage: failedStage, path: file.path,
    }, cause);
    const missing = failedStage === "open" && cause.code === "ENOENT";
    const error = new Error(
      `读取浏览器产物失败: browser_id=${browserId}, artifact_id=${artifactId}, kind=${kind}, stage=${failedStage}, code=${cause.code || "unknown"}`,
      { cause },
    );
    error.code = missing ? "browser_artifact_not_found" : cause.code;
    error.statusCode = missing ? 404 : 500;
    return error;
  };
  try {
    handle = await open(file.path, "r");
    stage = "stat";
    const fileStat = await handle.stat();
    if (!fileStat.isFile()) {
      throw Object.assign(new Error("浏览器产物不是普通文件"), { code: fileStat.isDirectory() ? "EISDIR" : "EINVAL" });
    }
    const headers = {
      "content-type": mediaType,
      "content-length": fileStat.size,
      "access-control-allow-origin": "*",
    };
    if (typeof file.filename === "string" && file.filename) {
      headers["content-disposition"] =
        `attachment; filename*=UTF-8''${encodeURIComponent(file.filename)}`;
    }
    response.writeHead(200, headers);
    stage = "stream";
    await pipeline(handle.createReadStream({ autoClose: false }), response);
  } catch (cause) {
    failure = artifactFailure(cause, stage);
  } finally {
    if (handle) {
      try {
        await handle.close();
      } catch (cause) {
        const cleanup = artifactFailure(cause, "close");
        if (failure) failure.cause = new AggregateError([failure.cause, cause], "产物读取及关闭失败", { cause: failure.cause });
        else failure = cleanup;
      }
    }
  }
  if (failure) throw failure;
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

async function runHttpOperation(request, browser, action, callback, { visible = true, queued = true } = {}) {
  try {
    if (!["close", "delete"].includes(action)) browser.assertReconciled();
    assertAgentAccessAllowed(request, browser);
    const result = queued
      ? await browser.enqueueOperation({ actor: requestActor(request), action, visible }, callback)
      : await callback();
    return { ...result, browser: browser.snapshot() };
  } catch (error) {
    error.browser = browser.snapshot();
    // 执行前策略拒绝或队列结清失败均有正式回执；连接断开没有此声明。
    error.operationOutcome = "failed";
    throw error;
  }
}

function requestWorkspace(request, url, mountedId) {
  const header = request.headers["x-boxteam-workspace-id"];
  const queries = url.searchParams.getAll("workspace_id");
  if (Array.isArray(header) || queries.length > 1 || (header && queries[0] && header !== queries[0])) {
    const error = new Error("Browser workspace_id 请求载体冲突");
    error.statusCode = 400;
    throw error;
  }
  const selected = header ?? queries[0];
  try { validateWorkspaceId(selected); } catch (error) { error.statusCode = 400; throw error; }
  if (selected !== mountedId) {
    const error = new Error("Browser 工作区 UUID 未挂载");
    error.statusCode = 404;
    throw error;
  }
}

function requireOwner(request) {
  if (request.headers["x-boxteam-resource-owner"] !== "workspace-browser") {
    const error = new Error("Browser 管理命令必须经 Workspace Browser owner");
    error.statusCode = 403;
    throw error;
  }
}

function wsClient(socket) {
  const frameFlow = new BrowserFrameFlow({
    socket,
    encodeFrame: encodeBinaryFrame,
  });
  return {
    close(code, reason) { socket.close(code, reason); },
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
  const workspaceId = validateWorkspaceId(args.get("workspace-id") || process.env.BOXTEAM_WORKSPACE_ID);
  const manager = new BrowserManager({ workspaceRoot, workspaceId });
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
    response.setHeader("access-control-allow-headers", "content-type,x-boxteam-workspace-id");
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
          workspace_id: workspaceId,
          browser_count: manager.browsersById.size,
          browser_runtime: manager.runtimePool.snapshot(),
          resource_governor: manager.resourceGovernorSnapshot(),
        });
        return;
      }

      requestWorkspace(request, url, workspaceId);
      const artifactRequest = /^\/api\/browsers\/[^/]+\/(downloads|screenshots)\/[^/]+$/.test(pathname);
      if (!artifactRequest) requireOwner(request);

      if (request.method === "GET" && pathname === "/api/browsers") {
        sendJson(response, 200, {
          data: manager.list(),
        });
        return;
      }

      if (request.method === "POST" && pathname === "/api/browsers") {
        const payload = await readJson(request);
        const browser = await manager.create({
          workspaceId: payload.workspace_id,
          browserId: payload.browser_id,
          ownerRevision: payload.owner_revision,
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
        await sendDownload(response, manager.download(downloadMatch[1], downloadMatch[2]), {
          browserId: downloadMatch[1], artifactId: downloadMatch[2], kind: "download",
        });
        return;
      }

      const screenshotMatch = pathname.match(/^\/api\/browsers\/([^/]+)\/screenshots\/([^/]+)$/);
      if (request.method === "GET" && screenshotMatch) {
        await sendDownload(
          response,
          manager.screenshot(screenshotMatch[1], screenshotMatch[2]),
          { browserId: screenshotMatch[1], artifactId: screenshotMatch[2], kind: "screenshot", mediaType: "image/png" },
        );
        return;
      }

      const browserMatch = pathname.match(/^\/api\/browsers\/([^/]+)(?:\/([^/]+))?$/);
      if (browserMatch) {
        const browserId = browserMatch[1];
        const action = browserMatch[2] || "";

        if (request.method === "GET" && !action) {
          sendJson(response, 200, { data: manager.get(browserId).snapshot() });
          return;
        }
        if (request.method === "POST" && action === "owner-revision") {
          const payload = await readJson(request);
          if (typeof payload.for_stop !== "boolean") {
            const error = new Error("Browser owner revision必须明确for_stop意图");
            error.statusCode = 400;
            throw error;
          }
          await manager.syncOwnerRevision(browserId, payload.owner_revision, { forStop: payload.for_stop });
          sendJson(response, 200, { data: manager.get(browserId).snapshot() });
          return;
        }
        if (request.method === "POST" && action === "transport-grant") {
          const payload = await readJson(request);
          sendJson(response, 200, { data: manager.issueTransportGrant(browserId, {
            ownerRevision: payload.owner_revision, association: payload.association,
          }) });
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
          const browser = manager.get(browserId);
          sendJson(response, 200, {
            data: await runHttpOperation(request, browser, "agent-lock",
              () => manager.setAgentAccessLocked(browserId, payload.locked, payload.owner_id), { queued: false }),
          });
          return;
        }

        if (request.method === "PATCH" && action === "resource-policy") {
          const payload = await readJson(request);
          const browser = manager.get(browserId);
          sendJson(response, 200, {
            data: await runHttpOperation(request, browser, "resource-policy",
              () => browser.setResourcePolicy(payload.policy), { queued: false }),
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
          sendJson(response, 200, { data: await runHttpOperation(request, browser, "freeze",
            () => browser.freeze({ reason: "manual" }), { queued: false }) });
          return;
        }

        if (request.method === "POST" && action === "wake") {
          const browser = manager.get(browserId);
          sendJson(response, 200, { data: await runHttpOperation(request, browser, "wake",
            () => browser.wake({ reason: "manual" }), { queued: false }) });
          return;
        }

        if (request.method === "POST" && action === "discard") {
          const browser = manager.get(browserId);
          sendJson(response, 200, { data: await runHttpOperation(request, browser, "discard",
            () => browser.discard({ reason: "manual" }), { queued: false }) });
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
            data: await runHttpOperation(request, browser, "close", () => manager.close(browserId), { queued: false }),
          });
          return;
        }

        if (request.method === "DELETE" && !action) {
          const browser = manager.get(browserId);
          sendJson(response, 200, {
            data: await runHttpOperation(request, browser, "delete", () => manager.delete(browserId), { queued: false }),
          });
          return;
        }
      }

      notFound(response);
    } catch (error) {
      if (response.headersSent || response.destroyed) {
        console.error("[browser-backend] 响应开始后传输失败", error);
        response.destroy(error);
        return;
      }
      if (error instanceof BrowserTransportGrantRejected) {
        sendJson(response, 409, {
          error: error.message,
          code: error.code,
          operation_outcome: error.operationOutcome,
          browser: error.browser,
        });
        return;
      }
      const message = error instanceof Error ? error.message : String(error);
      const status = error?.statusCode || (message.startsWith("浏览器页面不存在")
        ? 404
        : message.startsWith("浏览器下载不存在")
          ? 404
        : error?.code === "browser_artifact_not_found"
          ? 404
        : error?.code === "browser_stale_element_ref"
          ? 409
        : [
            "browser_resource_protected",
            "browser_resource_protected_during_transition",
            "browser_must_be_frozen_before_discard",
            "browser_workspace_logical_limit_exceeded",
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
          : error?.code === "browser_tool_timeout"
            ? 408
          : 500);
      sendJson(response, status, {
        error: message,
        ...(error?.creationReceipt ? { creation_receipt: error.creationReceipt } : {}),
        ...(error?.browser ? { browser: error.browser, operation_outcome: error.operationOutcome } : {}),
        ...(error?.code
          ? { code: error.code }
          : message.startsWith("浏览器页面不存在")
            ? {
                code: "browser_page_not_found",
                retryable: true,
                recovery: "list_or_open_browser_page",
              }
            : {}),
        ...(error?.code === "browser_tool_timeout"
          ? {
              retryable: error.retryable === true,
              recovery: error.recovery || "page_reset",
              timeout_ms: error.timeout_ms,
              ...(error.recovery_error ? { recovery_error: error.recovery_error } : {}),
            }
          : {}),
      });
    }
  });

  const wss = new WebSocketServer({ server, path: "/browser" });
  wss.on("connection", (socket, request) => {
    const client = wsClient(socket);
    try {
      requestWorkspace(request, new URL(request.url, "http://127.0.0.1"), workspaceId);
    } catch (error) {
      client.sendJson({ type: "error", message: error.message });
      socket.close(1008, error.message.slice(0, 120));
      return;
    }
    let attachedBrowser = null;
    let queued = Promise.resolve();

    // 撤销后的事件只丢弃旧连接输出；输入仍由 assertTransport 明确拒绝。
    const canSend = () => attachedBrowser?.transportAuthorized(client) === true;
    const onFrame = (frame) => { if (canSend()) client.sendFrame(frame); };
    const onState = (state) => {
      if (canSend()) client.sendJson({ type: "state", browserId: state.browser_id, state });
    };
    const onParticipantPointer = (pointer) => {
      if (canSend() && pointer.participantId !== client.participantId) {
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
      message, action, callback, { visible = true, interactive = false } = {},
    ) {
      const browser = attachedBrowser;
      if (!browser || message.browserId !== browser.id) throw new Error("Browser 消息与已授权连接不一致");
      browser.assertTransport(client);
      const operation = {
        actor: `user:${client.participantId}`, action, visible,
        transportOwnerRevision: browser.transports.get(client).transportOwnerRevision,
      };
      const result = interactive
        ? await browser.runInteractiveOperation(operation, callback)
        : await browser.enqueueOperation(operation, callback);
      browser.assertTransport(client);
      if (message.clientOperationId) {
        client.sendJson({
          type: "operationAck", browserId: browser.id,
          clientOperationId: message.clientOperationId, operation: result.operation,
        });
      }
      return result;
    }

    async function handleMessage(message) {
      if (message.type === "attach") {
        await detach(false);
        const browser = manager.get(message.browserId);
        const grant = manager.consumeTransportGrant(browser.id, message.grantToken);
        if (message.participantId) client.participantId = message.participantId;
        browser.transports.set(client, grant);
        attachedBrowser = browser;
        browser.assertTransport(client);
        browser.on("frame", onFrame);
        browser.on("state", onState);
        browser.on("participant-pointer", onParticipantPointer);
        const output = await browser.runInteractiveOperation({
          actor: `user:${client.participantId}`, action: "attach", transportOwnerRevision: grant.transportOwnerRevision,
        }, () => browser.attachClient(client));
        browser.assertTransport(client);
        client.sendJson({ type: "attached", browserId: browser.id, state: output });
        return;
      }

      if (message.type === "detach") {
        await detach(true);
        return;
      }

      const browser = attachedBrowser;
      if (!browser || message.browserId !== browser.id) throw new Error("尚未授权此 Browser");
      browser.assertTransport(client);

      if (message.type === "pointer") {
        await runUserOperation(
          message,
          `pointer:${message.action}`,
          () => browser.dispatchPointer(message),
          { visible: false, interactive: true },
        );
        browser.emit("participant-pointer", {
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
          () => browser.dispatchKey(message),
          { visible: false, interactive: true },
        );
        return;
      }
      if (message.type === "paste") {
        await runUserOperation(
          message,
          "insertText",
          () => browser.insertText(message.text),
          { visible: false, interactive: true },
        );
        return;
      }
      if (message.type === "handleDialog") {
        await runUserOperation(message, "dialog", () => browser.handleDialog({
            acceptModal: message.accept,
            promptText: message.promptText,
          }));
        return;
      }
      if (message.type === "selectFiles") {
        await runUserOperation(
          message,
          "selectFiles",
          () => browser.handleDialog({ filePayloads: message.files }),
        );
        return;
      }
      if (message.type === "readClipboard") {
        const output = await runUserOperation(
          message,
          "readClipboard",
          () => browser.readClipboardText(),
          { visible: false, interactive: true },
        );
        client.sendJson({
          type: "clipboardText",
          browserId: browser.id,
          text: output.result,
        });
        return;
      }
      if (message.type === "inspectElement" || message.type === "selectElement") {
        const output = await runUserOperation(
          message,
          message.type,
          () => browser.inspectElement({
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
          browserId: browser.id,
          mode: message.mode === "rich" ? "rich" : "basic",
          element,
        });
        return;
      }
      if (message.type === "viewport") {
        await runUserOperation(message, "viewport", () => browser.setViewport(
            parsePositiveInt(message.width, "width"),
            parsePositiveInt(message.height, "height"),
          ));
        return;
      }
      if (message.type === "deviceProfile") {
        await runUserOperation(
          message,
          "device-profile",
          () => browser.setDeviceProfile(message.profileId, message.orientation),
        );
        return;
      }
      if (message.type === "deviceSettings") {
        await runUserOperation(
          message,
          "device-settings",
          () => browser.setDeviceSettings(message.settings || {}),
        );
        return;
      }
      if (message.type === "saveDevicePreset") {
        await runUserOperation(
          message,
          "save-device-preset",
          () => browser.saveDevicePreset(message.name),
          { visible: false, interactive: true },
        );
        return;
      }
      if (message.type === "debugSnapshot") {
        const output = await runUserOperation(
          message,
          "debug-snapshot",
          () => browser.debugSnapshot(),
          { visible: false, interactive: true },
        );
        const data = Object.hasOwn(output, "result")
          ? output.result
          : Object.fromEntries(
              Object.entries(output).filter(([key]) => !["operation", "operation_revision"].includes(key)),
            );
        client.sendJson({
          type: "debugSnapshot",
          browserId: browser.id,
          data,
        });
        return;
      }
      if (message.type === "captureScreenshot") {
        const output = await runUserOperation(
          message,
          "capture-screenshot",
          async () => ({ result: await browser.captureClientScreenshot() }),
          { visible: false, interactive: true },
        );
        client.sendJson({
          type: "screenshotResult",
          browserId: browser.id,
          mimeType: "image/png",
          data: output.result.toString("base64"),
        });
        return;
      }
      if (message.type === "command") {
        const state = await runUserOperation(message, `command:${message.name}`, () => {
          if (message.name === "stop") {
            return browser.stopLoading();
          }
          if (message.name === "newPage") {
            return browser.createPage(message.url);
          }
          if (message.name === "activatePage") {
            return browser.activatePage(message.pageId);
          }
          if (message.name === "closePage") {
            return browser.closePage(message.pageId);
          }
          if (message.name === "find") {
            return browser.findText(message.query, message.backwards === true);
          }
          if (message.name === "clearNetwork") {
            return browser.clearNetworkRequests();
          }
          return browser.navigate(message.name === "goto" ? "url" : message.name, message.url);
        });
        client.sendJson({ type: "commandResult", browserId: browser.id, output: "命令已完成", state });
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
        try { attachedBrowser.assertTransport(client); } catch (error) { reportMessageError(message, error); return; }
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
  let hasFatalError = false;
  const shutdown = async (reason, exitCode, error = null) => {
    if (error) {
      hasFatalError = true;
      console.error(error);
    }
    if (shuttingDown) {
      return;
    }
    shuttingDown = true;
    for (const client of wss.clients) {
      client.close(1001, "browser manager shutting down");
    }
    try {
      await manager.shutdown(reason);
    } catch (shutdownError) {
      console.error(
        hasFatalError ? "[browser-backend] 致命错误后的关闭检查点失败:"
          : "[browser-backend] 关闭失败，保留原运行时与 HTTP 管理入口；核实原操作后再次发送关闭信号重试:",
        shutdownError,
      );
      if (!hasFatalError) {
        shuttingDown = false;
        return;
      }
    }
    // TODO：统一检查点持久化、HTTP 排空和整个 shutdown 的总期限及挂起终止核实。
    wss.close();
    await closeHttpServer(server);
    process.exit(hasFatalError ? 1 : exitCode);
  };

  process.on("SIGINT", () => void shutdown("browser_manager_sigint", 130));
  process.on("SIGTERM", () => void shutdown("browser_manager_sigterm", 143));
  process.on("SIGHUP", () => void shutdown("browser_manager_sighup", 129));
  if (process.platform === "win32") {
    process.on("SIGBREAK", () => void shutdown("browser_manager_sigbreak", 143));
  }
  process.once("uncaughtException", (error) => {
    void shutdown("browser_manager_uncaught_exception", 1, error);
  });
  process.once("unhandledRejection", (reason) => {
    const error = reason instanceof Error ? reason : new Error(String(reason));
    void shutdown("browser_manager_unhandled_rejection", 1, error);
  });
}

await main();
