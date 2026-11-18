import { afterEach, beforeEach, expect, spyOn, test } from "bun:test";
import * as http from "./http";
import * as reconnect from "../hooks/sessionEventStream/waitForReconnect";
import { watchPersistentTasks } from "./persistentTasks";
import { installGatewayTestWindow } from "../tests/fixtures/session/gatewayBrowserGlobals";
import { restoreGatewayTestEnvironment } from "../tests/fixtures/session/gatewayEnvironment";

const target = { gateway_id: "gateway-one", workspace_id: "workspace-one" };
const slot = { session_id: "session", thread_id: "main", goal: null, control_revision: 3, sync_version: 7 };
const snapshot = { items: [], active_runs: [], runtime: { state: "running", recovering_tasks: 0 }, session_goal: slot };
const restores: Array<() => void> = [];
function stream(value: unknown, init?: RequestInit) {
  const body = JSON.parse(String(init?.body)) as { subscriptions: Array<{ subscription_id: string }> };
  const subscription_id = body.subscriptions[0]!.subscription_id;
  const envelopes = [{ subscription_id, state: "connected" },
    { subscription_id, state: "frame", frame: `event: snapshot\ndata: ${JSON.stringify(value)}\n\n` }];
  return new Response(envelopes.map(envelope => `event: observation\ndata: ${JSON.stringify(envelope)}\n\n`).join(""),
    { headers: { "content-type": "text/event-stream" } });
}
beforeEach(() => { installGatewayTestWindow(8111); });
afterEach(() => { restores.splice(0).forEach(restore => restore()); restoreGatewayTestEnvironment(); });

test("断线退避后恢复同一owner的完整槽位，不补Goal GET；取消即停止", async () => {
  const retry = spyOn(reconnect, "waitForReconnect").mockResolvedValue(undefined);
  const request = spyOn(http, "requestGatewayResponse").mockRejectedValueOnce(new TypeError("网络断开"))
    .mockImplementationOnce(async (_port, _path, init) => stream(snapshot, init));
  restores.push(() => retry.mockRestore(), () => request.mockRestore());
  const controller = new AbortController();
  const received: unknown[] = [];
  await watchPersistentTasks(8111, target, controller.signal, value => { received.push(value.session_goal); controller.abort(); }, () => {}, "session");
  expect(received).toEqual([slot]);
  expect(request).toHaveBeenCalledTimes(2);
  expect(retry).toHaveBeenCalledTimes(1);
  expect(request.mock.calls.every(([, path]) => path === "/api/v1/workspace/observations/stream")).toBe(true);
  expect(JSON.parse(String(request.mock.calls[1]?.[2]?.body)).subscriptions[0]).toMatchObject({ kind: "persistent_tasks", session_id: "session" });
  expect(new Headers(request.mock.calls[1]?.[2]?.headers).get("X-BoxTeam-Target-Gateway-Id")).toBe("gateway-one");
});

test("协议缺少Goal槽位明确失败，不用无限重连掩盖", async () => {
  const request = spyOn(http, "requestGatewayResponse").mockImplementation(async (_port, _path, init) => stream({ ...snapshot, session_goal: undefined }, init));
  restores.push(() => request.mockRestore());
  await expect(watchPersistentTasks(8111, target, new AbortController().signal, () => {}, () => {}, "session"))
    .rejects.toThrow("缺少 Goal 槽位");
  expect(request).toHaveBeenCalledTimes(1);
});

test("消费者的main归属错误立即暴露，不当作网络故障重试", async () => {
  const request = spyOn(http, "requestGatewayResponse").mockImplementation(async (_port, _path, init) => stream(snapshot, init));
  restores.push(() => request.mockRestore());
  await expect(watchPersistentTasks(8111, target, new AbortController().signal, () => { throw new Error("main Thread 不一致"); }, () => {}, "session"))
    .rejects.toThrow("main Thread 不一致");
  expect(request).toHaveBeenCalledTimes(1);
});
