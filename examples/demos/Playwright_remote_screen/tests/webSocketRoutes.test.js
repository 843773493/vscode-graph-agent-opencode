import { EventEmitter } from "node:events";
import { describe, expect, test } from "bun:test";
import { createBrowserSessionStub } from "./browserSessionStub.js";
import {
  createViewerSocketHandler,
  webSocketUpgradePath,
} from "../src/server/webSocketRoutes.js";

class FakeViewerSocket extends EventEmitter {
  constructor() {
    super();
    this.readyState = 1;
    this.bufferedAmount = 0;
    this.sentMessages = [];
  }

  send(message) {
    this.sentMessages.push(message);
    const serverMessage = JSON.parse(message);
    this.emit("message-sent", serverMessage);
    if (serverMessage.type === "attached") {
      this.emit("attached-message-sent");
    }
  }

  close() {
    this.readyState = 3;
    this.emit("close");
  }
}

function createViewerSessionStub() {
  const session = new EventEmitter();
  session.browserId = "uuid:12";
  session.attachedClientIds = new Set();
  const createViewerPageState = () => ({
    browserId: session.browserId,
    closed: false,
    url: "https://example.test/",
    title: "Fixture page",
    viewport: { width: 1280, height: 800 },
    attachedClients: session.attachedClientIds.size,
    streaming: session.attachedClientIds.size > 0,
  });
  session.attachClient = async (clientId) => {
    session.attachedClientIds.add(clientId);
    const state = createViewerPageState();
    session.emit("state", state);
    return state;
  };
  session.detachClient = async (clientId) => {
    session.attachedClientIds.delete(clientId);
    const state = createViewerPageState();
    session.emit("state", state);
    return state;
  };
  return session;
}

async function createAttachedViewer(handleSocket) {
  const socket = new FakeViewerSocket();
  handleSocket(socket);
  const attachedMessageSent = new Promise((resolve) => {
    socket.once("attached-message-sent", resolve);
  });
  socket.emit("message", JSON.stringify({ type: "attach", browserId: "uuid:12" }));
  await attachedMessageSent;
  return socket;
}

function waitForSocketMessage(socket, messageType) {
  return new Promise((resolve) => {
    const onMessageSent = (message) => {
      if (message.type === messageType) {
        socket.off("message-sent", onMessageSent);
        resolve(message);
      }
    };
    socket.on("message-sent", onMessageSent);
  });
}

function createBrowserSessionWithFailingScreencastStart() {
  let failNextStart = true;
  const cdpMethods = [];
  const session = createBrowserSessionStub({
    async send(method) {
      cdpMethods.push(method);
      if (method === "Page.startScreencast" && failNextStart) {
        failNextStart = false;
        throw new Error("CDP screencast startup failed");
      }
    },
  });
  return { session, cdpMethods };
}

describe("查看器屏幕帧背压", () => {
  test("缓冲加帧数据长度下界已超限时，不序列化即将丢弃的帧", async () => {
    const session = createViewerSessionStub();
    const socket = await createAttachedViewer(createViewerSocketHandler(session));

    const messagesBeforeFrame = socket.sentMessages.length;
    const dataUrl = "x".repeat(11);
    socket.bufferedAmount = (8 * 1024 * 1024) - dataUrl.length + 1;
    const frame = new Proxy({ dataUrl }, {
      ownKeys() {
        throw new Error("过载帧不应进入 JSON 序列化");
      },
    });

    session.emit("frame", frame);

    expect(socket.sentMessages).toHaveLength(messagesBeforeFrame);
    socket.bufferedAmount = 0;
    session.emit("frame", {
      browserId: "uuid:12",
      dataUrl: "data:image/jpeg;base64,AA==",
      width: 1280,
      height: 800,
      pageScaleFactor: 1,
      timestamp: 1,
    });
    const encodedFrame = socket.sentMessages.at(-1);
    expect(JSON.parse(encodedFrame)).toMatchObject({
      type: "frame",
      browserId: "uuid:12",
      width: 1280,
      height: 800,
    });
    expect(Buffer.byteLength(encodedFrame, "utf8")).toBe(encodedFrame.length);
    socket.close();
  });

  test("Unicode browserId 使用准确的 UTF-8 帧字节数", async () => {
    const session = createViewerSessionStub();
    const socket = await createAttachedViewer(createViewerSocketHandler(session));
    const frame = {
      browserId: "浏览器",
      dataUrl: "data:image/jpeg;base64,AA==",
      width: 1280,
      height: 800,
      pageScaleFactor: 1,
      timestamp: 1,
    };
    const encodedMessage = JSON.stringify({ type: "frame", ...frame });
    expect(Buffer.byteLength(encodedMessage, "utf8")).toBeGreaterThan(encodedMessage.length);
    socket.bufferedAmount = (8 * 1024 * 1024) - encodedMessage.length;

    session.emit("frame", frame);

    expect(socket.sentMessages.some((message) => JSON.parse(message).type === "frame")).toBe(false);
    socket.close();
  });

  test("同一帧发给多个查看器时只序列化一次", async () => {
    const session = createViewerSessionStub();
    const handleSocket = createViewerSocketHandler(session);
    const firstSocket = await createAttachedViewer(handleSocket);
    const secondSocket = await createAttachedViewer(handleSocket);
    const frameValues = {
      browserId: "uuid:12",
      dataUrl: "data:image/jpeg;base64,AA==",
      width: 1280,
      height: 800,
      pageScaleFactor: 1,
      timestamp: 1,
    };
    let propertyEnumerations = 0;
    const frame = new Proxy(frameValues, {
      ownKeys(target) {
        propertyEnumerations += 1;
        return Reflect.ownKeys(target);
      },
    });

    session.emit("frame", frame);

    const firstFrameMessage = JSON.parse(firstSocket.sentMessages.at(-1));
    const secondFrameMessage = JSON.parse(secondSocket.sentMessages.at(-1));
    expect(firstFrameMessage.type).toBe("frame");
    expect(secondFrameMessage.type).toBe("frame");
    expect(propertyEnumerations).toBe(1);
    firstSocket.close();
    secondSocket.close();
  });
});

describe("WebSocket upgrade path", () => {
  test("只按请求 URL 解析路径，不依赖监听地址格式", () => {
    expect(webSocketUpgradePath("/remote-screen?client=viewer")).toBe("/remote-screen");
    expect(webSocketUpgradePath("/cdp/page/target-12")).toBe("/cdp/page/target-12");
  });
});

describe("查看器 attach 生命周期", () => {
  test("多个查看器共享准确的 attach 数量并在 detach 后更新剩余查看器", async () => {
    const session = createViewerSessionStub();
    const handleSocket = createViewerSocketHandler(session);
    const firstSocket = await createAttachedViewer(handleSocket);
    const firstViewerState = waitForSocketMessage(firstSocket, "state");
    const secondSocket = await createAttachedViewer(handleSocket);

    expect(await firstViewerState).toMatchObject({
      state: { attachedClients: 2, streaming: true },
    });
    const secondAttachedMessage = secondSocket.sentMessages
      .map((message) => JSON.parse(message))
      .find((message) => message.type === "attached");
    expect(secondAttachedMessage.state).toMatchObject({
      attachedClients: 2,
      streaming: true,
    });

    const secondViewerState = waitForSocketMessage(secondSocket, "state");
    firstSocket.close();
    expect(await secondViewerState).toMatchObject({
      state: { attachedClients: 1, streaming: true },
    });
    expect(session.attachedClientIds.size).toBe(1);

    const lastDetachState = new Promise((resolve) => {
      const onState = (state) => {
        if (state.attachedClients === 0) {
          session.off("state", onState);
          resolve(state);
        }
      };
      session.on("state", onState);
    });
    secondSocket.close();
    expect(await lastDetachState).toMatchObject({ attachedClients: 0, streaming: false });
    expect(session.attachedClientIds.size).toBe(0);
  });

  test("socket 在 attachClient 完成前关闭时，等待登记后再清理客户端", async () => {
    const session = createViewerSessionStub();
    const attachedClientIds = new Set();
    let completeAttach;
    let announceAttachStarted;
    let announceAttachCompleted;
    let announceDetachCompleted;
    const attachGate = new Promise((resolve) => {
      completeAttach = resolve;
    });
    const attachStarted = new Promise((resolve) => {
      announceAttachStarted = resolve;
    });
    const attachCompleted = new Promise((resolve) => {
      announceAttachCompleted = resolve;
    });
    const detachCompleted = new Promise((resolve) => {
      announceDetachCompleted = resolve;
    });
    let detachCount = 0;
    session.attachClient = async (clientId) => {
      announceAttachStarted();
      await attachGate;
      attachedClientIds.add(clientId);
      announceAttachCompleted();
      return {
        browserId: session.browserId,
        closed: false,
        url: "https://example.test/",
        title: "Fixture page",
        viewport: { width: 1280, height: 800 },
        attachedClients: attachedClientIds.size,
        streaming: true,
      };
    };
    session.detachClient = async (clientId) => {
      detachCount += 1;
      attachedClientIds.delete(clientId);
      announceDetachCompleted();
      return {
        browserId: session.browserId,
        closed: false,
        url: "https://example.test/",
        title: "Fixture page",
        viewport: { width: 1280, height: 800 },
        attachedClients: attachedClientIds.size,
        streaming: false,
      };
    };
    const socket = new FakeViewerSocket();
    createViewerSocketHandler(session)(socket);

    socket.emit("message", JSON.stringify({ type: "attach", browserId: "uuid:12" }));
    await attachStarted;
    socket.close();
    completeAttach();
    await attachCompleted;
    await detachCompleted;

    expect(attachedClientIds.size).toBe(0);
    expect(detachCount).toBe(1);
    expect(session.listenerCount("frame")).toBe(0);
    expect(session.listenerCount("state")).toBe(0);
    expect(socket.sentMessages).toEqual([]);
  });

  test("导航命令响应复用会话状态快照", async () => {
    const session = createBrowserSessionStub({ send: async () => undefined });
    let titleReads = 0;
    const readPageTitle = session.page.title;
    session.page.title = async () => {
      titleReads += 1;
      return await readPageTitle();
    };
    const socket = await createAttachedViewer(createViewerSocketHandler(session));
    const titleReadsAfterAttach = titleReads;

    const commandResult = waitForSocketMessage(socket, "commandResult");
    socket.emit("message", JSON.stringify({
      type: "command",
      browserId: session.browserId,
      name: "goto",
      url: "https://example.test/next",
    }));

    expect(await commandResult).toEqual({
      type: "commandResult",
      browserId: session.browserId,
      output: "已进入 https://example.test/next",
      state: {
        browserId: session.browserId,
        closed: false,
        url: "https://example.test/next",
        title: "fixture page",
        viewport: { width: 1280, height: 800 },
        attachedClients: 1,
        streaming: true,
      },
    });
    expect(titleReads - titleReadsAfterAttach).toBe(1);
    socket.close();
  });

  test("attach 失败时撤销监听和会话登记，并允许同一 socket 重试", async () => {
    const { session, cdpMethods } = createBrowserSessionWithFailingScreencastStart();
    const socket = new FakeViewerSocket();
    createViewerSocketHandler(session)(socket);

    const failedAttach = waitForSocketMessage(socket, "error");
    socket.emit("message", JSON.stringify({ type: "attach", browserId: "uuid:12" }));
    expect(await failedAttach).toMatchObject({
      type: "error",
      message: "CDP screencast startup failed",
    });
    expect(session.attachedClientIds.size).toBe(0);
    expect(session.listenerCount("frame")).toBe(0);
    expect(session.listenerCount("state")).toBe(0);

    const retriedAttach = waitForSocketMessage(socket, "attached");
    socket.emit("message", JSON.stringify({ type: "attach", browserId: "uuid:12" }));
    await retriedAttach;
    expect(session.attachedClientIds.size).toBe(1);

    const detached = waitForSocketMessage(socket, "detached");
    socket.emit("message", JSON.stringify({ type: "detach", browserId: "uuid:12" }));
    await detached;
    expect(session.attachedClientIds.size).toBe(0);
    expect(session.listenerCount("frame")).toBe(0);
    expect(session.listenerCount("state")).toBe(0);
    expect(cdpMethods).toEqual([
      "Page.startScreencast",
      "Page.startScreencast",
      "Page.stopScreencast",
    ]);
    socket.close();
  });
});
