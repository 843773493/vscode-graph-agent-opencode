import { WebSocket } from "ws";
import { encodeServerMessage, parseClientMessage } from "./protocol.js";
import { executeBrowserCommand } from "./browserControlCommands.js";

const MAX_PENDING_CDP_MESSAGE_BYTES = 1024 * 1024;
const MAX_CDP_SOCKET_BUFFERED_BYTES = 8 * 1024 * 1024;
const MAX_VIEWER_SOCKET_BUFFERED_BYTES = 8 * 1024 * 1024;
const MAX_QUEUED_VIEWER_MESSAGES = 2048;
const MAX_QUEUED_VIEWER_MESSAGE_BYTES = 8 * 1024 * 1024;

export function webSocketUpgradePath(requestUrl) {
  return new URL(requestUrl || "/", "http://127.0.0.1").pathname;
}

function payloadByteLength(payload) {
  if (Array.isArray(payload)) {
    return payload.reduce((byteLength, chunk) => byteLength + chunk.byteLength, 0);
  }
  return typeof payload === "string" ? Buffer.byteLength(payload) : payload.byteLength;
}

function closeSocket(socket, code, reason) {
  if (socket.readyState !== WebSocket.OPEN) {
    return;
  }
  const safeCode = Number.isInteger(code) && code >= 1000 && code <= 4999 && code !== 1005 && code !== 1006 && code !== 1015
    ? code
    : 1000;
  socket.close(safeCode, reason);
}

export function proxyCdpWebSocket(session, targetId, clientSocket) {
  const upstreamSocket = new WebSocket(`${session.cdpWebSocketOrigin()}/devtools/page/${targetId}`);
  const pendingMessages = [];
  let pendingMessageBytes = 0;
  let upstreamOpen = false;

  function clearPendingMessages() {
    pendingMessages.length = 0;
    pendingMessageBytes = 0;
  }

  function forwardCdpMessage(socket, payload, isBinary) {
    const payloadBytes = payloadByteLength(payload);
    if (
      socket.readyState !== WebSocket.OPEN
      || socket.bufferedAmount + payloadBytes > MAX_CDP_SOCKET_BUFFERED_BYTES
    ) {
      return false;
    }
    socket.send(payload, { binary: isBinary });
    return true;
  }

  function closeOverloadedProxy(reason) {
    clearPendingMessages();
    console.warn(`[cdp-ws] 关闭过载代理: ${reason}`);
    closeSocket(clientSocket, 1013, reason);
    upstreamSocket.terminate();
  }

  upstreamSocket.on("open", () => {
    if (clientSocket.readyState !== WebSocket.OPEN) {
      upstreamSocket.terminate();
      clearPendingMessages();
      return;
    }
    upstreamOpen = true;
    for (const message of pendingMessages) {
      if (!forwardCdpMessage(upstreamSocket, message.payload, message.isBinary)) {
        closeOverloadedProxy("CDP 上游发送缓冲已满");
        return;
      }
    }
    clearPendingMessages();
  });

  clientSocket.on("message", (raw, isBinary) => {
    const payload = raw;
    if (upstreamOpen) {
      if (!forwardCdpMessage(upstreamSocket, payload, isBinary)) {
        closeOverloadedProxy("CDP 上游发送缓冲已满");
      }
      return;
    }
    const payloadBytes = payloadByteLength(payload);
    if (pendingMessageBytes + payloadBytes > MAX_PENDING_CDP_MESSAGE_BYTES) {
      clearPendingMessages();
      upstreamSocket.terminate();
      closeSocket(clientSocket, 1013, "CDP upstream is not ready");
      return;
    }
    pendingMessages.push({ payload, isBinary });
    pendingMessageBytes += payloadBytes;
  });

  upstreamSocket.on("message", (raw, isBinary) => {
    if (clientSocket.readyState === WebSocket.OPEN) {
      const payload = raw;
      if (!forwardCdpMessage(clientSocket, payload, isBinary)) {
        closeOverloadedProxy("DevTools 客户端接收缓冲已满");
      }
    }
  });

  upstreamSocket.on("close", (code, reason) => {
    clearPendingMessages();
    closeSocket(clientSocket, code, reason);
  });

  clientSocket.on("close", () => {
    clearPendingMessages();
    if (upstreamSocket.readyState === WebSocket.OPEN) {
      closeSocket(upstreamSocket, 1000, "client closed");
      return;
    }
    if (upstreamSocket.readyState === WebSocket.CONNECTING) {
      upstreamSocket.terminate();
    }
  });

  upstreamSocket.on("error", (error) => {
    clearPendingMessages();
    console.error(`[cdp-ws] 上游连接错误: ${error instanceof Error ? error.message : String(error)}`);
    if (clientSocket.readyState === WebSocket.OPEN) {
      clientSocket.close(1011, "cdp upstream error");
    }
  });

  clientSocket.on("error", (error) => {
    console.error(`[cdp-ws] DevTools 客户端连接错误: ${error instanceof Error ? error.message : String(error)}`);
  });
}

function sendJson(socket, message) {
  const encodedMessage = encodeServerMessage(message);
  return sendEncodedJson(
    socket,
    encodedMessage,
    Buffer.byteLength(encodedMessage),
  );
}

function sendEncodedJson(socket, encodedMessage, encodedByteLength, dropIfBacklogged = false) {
  if (socket.readyState !== WebSocket.OPEN) {
    console.warn("[ws] socket 未打开，跳过发送消息");
    return false;
  }
  if (socket.bufferedAmount + encodedByteLength > MAX_VIEWER_SOCKET_BUFFERED_BYTES) {
    if (!dropIfBacklogged) {
      console.warn("[ws] 查看器发送缓冲已满，关闭慢速连接");
      socket.close(1013, "viewer send buffer full");
    }
    return false;
  }
  socket.send(encodedMessage);
  return true;
}

function encodedFrameByteLength(frame, encodedMessage) {
  if (typeof frame.browserId === "string" && /^[\x00-\x7F]*$/.test(frame.browserId)) {
    return encodedMessage.length;
  }
  return Buffer.byteLength(encodedMessage);
}

function sendSocketError(socket, error) {
  const message = error instanceof Error ? error.message : String(error);
  sendJson(socket, { type: "error", message });
}

function assertAttached(attached) {
  if (!attached) {
    throw new Error("请先 attach 到 uuid:12");
  }
}

function browserControlCommandFromClientMessage(message) {
  if (message.name === "goto") {
    return { name: "goto", url: message.url };
  }
  return { name: message.name };
}

export function createViewerSocketHandler(session) {
  let nextClientNumber = 0;
  const encodedFrameCache = new WeakMap();

  return (socket) => {
    const clientId = `viewer:${++nextClientNumber}`;
    let attached = false;
    let messageProcessingChain = Promise.resolve();
    let queuedMessageCount = 0;
    let queuedMessageBytes = 0;
    let messageProcessingStopped = false;
    let frameBackpressureWarningActive = false;

    const onFrame = (frame) => {
      if (attached && socket.readyState === WebSocket.OPEN) {
        if (socket.bufferedAmount + frame.dataUrl.length >= MAX_VIEWER_SOCKET_BUFFERED_BYTES) {
          if (!frameBackpressureWarningActive) {
            console.warn(`[ws] ${clientId} 接收缓慢，丢弃过期屏幕帧`);
          }
          frameBackpressureWarningActive = true;
          return;
        }

        let encodedFrame = encodedFrameCache.get(frame);
        if (!encodedFrame) {
          const encodedMessage = encodeServerMessage({ type: "frame", ...frame });
          encodedFrame = {
            message: encodedMessage,
            byteLength: encodedFrameByteLength(frame, encodedMessage),
          };
          encodedFrameCache.set(frame, encodedFrame);
        }
        const sent = sendEncodedJson(socket, encodedFrame.message, encodedFrame.byteLength, true);
        if (!sent && socket.readyState === WebSocket.OPEN) {
          if (!frameBackpressureWarningActive) {
            console.warn(`[ws] ${clientId} 接收缓慢，丢弃过期屏幕帧`);
          }
          frameBackpressureWarningActive = true;
        } else if (sent) {
          frameBackpressureWarningActive = false;
        }
      }
    };
    const onState = (state) => {
      if (attached && socket.readyState === WebSocket.OPEN) {
        sendJson(socket, { type: "state", browserId: session.browserId, state });
      }
    };

    async function detach(sendDetached) {
      if (!attached) {
        return;
      }
      attached = false;
      session.off("frame", onFrame);
      session.off("state", onState);
      const state = await session.detachClient(clientId);
      if (sendDetached && !messageProcessingStopped && socket.readyState === WebSocket.OPEN) {
        sendJson(socket, { type: "detached", browserId: session.browserId, state });
      }
    }

    async function handleViewerMessage(rawMessage) {
      const message = parseClientMessage(rawMessage);
      if (message.type === "attach") {
        if (attached) {
          throw new Error(`${clientId} 已经 attach`);
        }
        attached = true;
        session.on("frame", onFrame);
        session.on("state", onState);
        let state;
        try {
          state = await session.attachClient(clientId);
        } catch (error) {
          try {
            await detach(false);
          } catch (rollbackError) {
            throw new AggregateError(
              [error, rollbackError],
              `${clientId} attach 失败，且清理登记也失败`,
            );
          }
          throw error;
        }
        if (!messageProcessingStopped && socket.readyState === WebSocket.OPEN) {
          sendJson(socket, { type: "attached", browserId: message.browserId, state });
        }
        return;
      }

      if (message.type === "detach") {
        await detach(true);
        return;
      }

      assertAttached(attached);
      if (message.type === "pointer") {
        await session.dispatchPointer(message);
        return;
      }
      if (message.type === "key") {
        await session.dispatchKey(message);
        return;
      }
      if (message.type === "paste") {
        await session.insertText(message.text);
        return;
      }
      if (message.type === "viewport") {
        await session.setViewport(message.width, message.height);
        return;
      }
      if (message.type === "command") {
        const commandResult = await executeBrowserCommand(
          session,
          browserControlCommandFromClientMessage(message),
        );
        const state = commandResult.state ?? await session.currentState();
        sendJson(socket, {
          type: "commandResult",
          browserId: message.browserId,
          output: commandResult.output,
          state,
        });
        return;
      }
      throw new Error(`未处理消息类型: ${message.type}`);
    }

    socket.on("message", (rawMessage) => {
      if (messageProcessingStopped) {
        return;
      }
      const messageBytes = payloadByteLength(rawMessage);
      if (
        queuedMessageCount >= MAX_QUEUED_VIEWER_MESSAGES
        || queuedMessageBytes + messageBytes > MAX_QUEUED_VIEWER_MESSAGE_BYTES
      ) {
        messageProcessingStopped = true;
        sendSocketError(socket, new Error("查看器消息队列已满，连接将关闭"));
        socket.close(1013, "viewer message queue full");
        return;
      }
      queuedMessageCount += 1;
      queuedMessageBytes += messageBytes;
      messageProcessingChain = messageProcessingChain
        .then(() => (messageProcessingStopped ? undefined : handleViewerMessage(rawMessage)))
        .catch((error) => {
          if (messageProcessingStopped || socket.readyState !== WebSocket.OPEN) {
            const details = error instanceof Error ? error.message : String(error);
            console.error(`[ws] ${clientId} 断连时消息处理失败: ${details}`);
            return;
          }
          sendSocketError(socket, error);
        })
        .finally(() => {
          queuedMessageCount -= 1;
          queuedMessageBytes -= messageBytes;
        });
    });

    socket.on("close", () => {
      messageProcessingStopped = true;
      messageProcessingChain = messageProcessingChain
        .then(() => detach(false))
        .catch((error) => {
          const details = error instanceof Error ? error.message : String(error);
          console.error(`[ws] ${clientId} 关闭清理失败: ${details}`);
        });
    });

    socket.on("error", (error) => {
      console.error(`[ws] ${clientId} 连接错误: ${error instanceof Error ? error.message : String(error)}`);
    });
  };
}
