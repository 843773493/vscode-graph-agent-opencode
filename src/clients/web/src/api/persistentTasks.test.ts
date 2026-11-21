import { afterEach, beforeEach, expect, spyOn, test } from "bun:test";
import * as realtime from "./stream/realtime";
import * as reconnect from "../hooks/sessionEventStream/waitForReconnect";
import { watchPersistentTasks } from "./persistentTasks";
import { installGatewayTestWindow } from "../tests/fixtures/session/gatewayBrowserGlobals";
import { restoreGatewayTestEnvironment } from "../tests/fixtures/session/gatewayEnvironment";

const target = { gateway_id: "gateway-one", workspace_id: "workspace-one" };
const slot = { session_id: "session", thread_id: "main", goal: null, control_revision: 3, sync_version: 7 };
const snapshot = { items: [], active_runs: [], runtime: { state: "running", recovering_tasks: 0 }, session_goal: slot };
const restores: Array<() => void> = [];
async function stream(value: unknown, options: Parameters<typeof realtime.consumeRealtimeSubscription>[2]) {
  const handler = options.events.snapshot;
  if (handler && typeof handler !== "function") { const frame = { event: "snapshot", data: JSON.stringify(value), id: null };
    await handler.handle(handler.decode(frame.data, frame), frame); }
}
beforeEach(() => { installGatewayTestWindow(8111); });
afterEach(() => { restores.splice(0).forEach(restore => restore()); restoreGatewayTestEnvironment(); });

test("断线退避后恢复同一owner的完整槽位，不补Goal GET；取消即停止", async () => {
  const retry = spyOn(reconnect, "waitForReconnect").mockResolvedValue(undefined);
  const request = spyOn(realtime, "consumeRealtimeSubscription").mockRejectedValueOnce(new TypeError("网络断开"))
    .mockImplementationOnce(async (_port, _request, options) => stream(snapshot, options));
  restores.push(() => retry.mockRestore(), () => request.mockRestore());
  const controller = new AbortController();
  const received: unknown[] = [];
  await watchPersistentTasks(8111, target, controller.signal, value => { received.push(value.session_goal); controller.abort(); }, () => {}, "session");
  expect(received).toEqual([slot]);
  expect(request).toHaveBeenCalledTimes(2);
  expect(retry).toHaveBeenCalledTimes(1);
  expect(request.mock.calls.every(([, path]) => path().path === "/api/v1/persistent-tasks/events?session_id=session")).toBe(true);

  expect(new Headers(request.mock.calls[1]?.[1]().headers).get("X-BoxTeam-Target-Gateway-Id")).toBe("gateway-one");
});

test("协议缺少Goal槽位明确失败，不用无限重连掩盖", async () => {
  const request = spyOn(realtime, "consumeRealtimeSubscription").mockImplementation(async (_port, _request, options) => stream({ ...snapshot, session_goal: undefined }, options));
  restores.push(() => request.mockRestore());
  await expect(watchPersistentTasks(8111, target, new AbortController().signal, () => {}, () => {}, "session"))
    .rejects.toThrow("缺少 Goal 槽位");
  expect(request).toHaveBeenCalledTimes(1);
});

test("消费者的main归属错误立即暴露，不当作网络故障重试", async () => {
  const request = spyOn(realtime, "consumeRealtimeSubscription").mockImplementation(async (_port, _request, options) => stream(snapshot, options));
  restores.push(() => request.mockRestore());
  await expect(watchPersistentTasks(8111, target, new AbortController().signal, () => { throw new Error("main Thread 不一致"); }, () => {}, "session"))
    .rejects.toThrow("main Thread 不一致");
  expect(request).toHaveBeenCalledTimes(1);
});
