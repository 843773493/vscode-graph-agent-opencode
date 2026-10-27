import { afterEach, beforeEach, expect, spyOn, test } from "bun:test";
import * as api from "../../api/mcpConnections";
import { authorizeMcpInWindow, MCP_OAUTH_CALLBACK_PATH, MCP_OAUTH_MESSAGE } from "./authorizationWindow";

const target = { workspace_id: "gateway-remote", backend_workspace_id: "remote-owner", connection_kind: "remote_gateway" as const };
const operation: api.McpConnectionOperationDTO = { operation_id: "original-operation", connection_id: "connection", server_id: "secure", action: "connect", status: "waiting_user", stage: "authorize", created_at: 1, updated_at: 1, deadline: Date.now() / 1000 + 60 };
const originalWindow = Object.getOwnPropertyDescriptor(globalThis, "window");
let events: EventTarget;
let popup: { closed: boolean; location: { href: string }; close: () => void };
let checkClosed: () => void;
let expire: () => void;
let listenerCount: number;
const restores: Array<() => void> = [];

function message(source: unknown, origin = "http://client.example", data = { type: MCP_OAUTH_MESSAGE, code: "private-code", state: "private-state" }) {
  const event = new Event("message");
  Object.defineProperties(event, { source: { value: source }, origin: { value: origin }, data: { value: data } });
  events.dispatchEvent(event);
}
beforeEach(() => {
  events = new EventTarget(); listenerCount = 0;
  popup = { closed: false, location: { href: "about:blank" }, close: () => { popup.closed = true; } };
  Object.defineProperty(globalThis, "window", { configurable: true, value: {
    location: { origin: "http://client.example" }, open: () => popup,
    addEventListener: (name: string, callback: EventListener) => { ++listenerCount; events.addEventListener(name, callback); },
    removeEventListener: (name: string, callback: EventListener) => { --listenerCount; events.removeEventListener(name, callback); },
    setInterval: (callback: () => void) => { checkClosed = callback; return 1; }, clearInterval: () => {},
    setTimeout: (callback: () => void) => { expire = callback; return 2; }, clearTimeout: () => {},
  } });
  const authorization = spyOn(api, "getMcpAuthorization").mockResolvedValue({ authorization_url: `https://server.example/authorize?redirect_uri=${encodeURIComponent(`http://client.example${MCP_OAUTH_CALLBACK_PATH}`)}` });
  const callback = spyOn(api, "completeMcpAuthorization").mockResolvedValue({ ...operation, status: "verifying" });
  restores.push(() => authorization.mockRestore(), () => callback.mockRestore());
});
afterEach(() => {
  for (const restore of restores.splice(0)) restore();
  if (originalWindow) Object.defineProperty(globalThis, "window", originalWindow);
  else Reflect.deleteProperty(globalThis, "window");
});

test("只接纳同源且来自指定弹窗的回调，私有 POST 固定原工作区和操作", async () => {
  const signal = new AbortController().signal;
  const callback = spyOn(api, "completeMcpAuthorization");
  const result = authorizeMcpInWindow(8014, target, operation, signal);
  await Promise.resolve();
  message({}); message(popup, "https://other.example");
  expect(callback).not.toHaveBeenCalled();
  message(popup); message(popup);
  expect((await result).status).toBe("verifying");
  expect(callback).toHaveBeenCalledTimes(1);
  expect(callback).toHaveBeenCalledWith(8014, target, "original-operation", { code: "private-code", state: "private-state" }, signal);
  expect(listenerCount).toBe(0);
  expect(popup.closed).toBe(true);
});

test.each(["close", "abort", "deadline"])("%s 结束等待并释放监听器，不能把关闭弹窗当就绪", async (reason) => {
  const controller = new AbortController();
  const result = authorizeMcpInWindow(8014, target, operation, controller.signal);
  void result.catch(() => {});
  await Promise.resolve();
  if (reason === "close") { popup.closed = true; checkClosed(); }
  if (reason === "abort") controller.abort(new Error("目标工作区已切换"));
  if (reason === "deadline") expire();
  await expect(result).rejects.toThrow();
  expect(api.completeMcpAuthorization).not.toHaveBeenCalled();
  expect(listenerCount).toBe(0);
  expect(popup.closed).toBe(true);
});

test("远端回调配置不能指向客户端无法接收的 localhost", async () => {
  spyOn(api, "getMcpAuthorization").mockResolvedValue({ authorization_url: "https://server.example/authorize?redirect_uri=http%3A%2F%2Flocalhost%2Fcallback" });
  await expect(authorizeMcpInWindow(8014, target, operation, new AbortController().signal)).rejects.toThrow("oauth.redirect_uri");
  expect(api.completeMcpAuthorization).not.toHaveBeenCalled();
  expect(popup.closed).toBe(true);
});
