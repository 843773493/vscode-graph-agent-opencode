import { afterEach, beforeEach, expect, spyOn, test } from "bun:test";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import * as api from "../../api/persistentTasks";
import type { GatewayWorkspaceRequestTarget } from "../../api/workspaceIdentity";
import { usePersistentTasks } from "./usePersistentTasks";

const target: GatewayWorkspaceRequestTarget = { workspace_id: "gateway-one", backend_workspace_id: "backend-one", connection_kind: "local" };
const original: api.PersistentTaskDTO = {
  task_id: "task-one", workspace_id: "backend-one", revision: 1, definition_revision: 1,
  application_type: "automation", application_version: "1", configuration: {
    name: "原任务", trigger: { type_id: "builtin.manual", version: "1", timezone: "UTC" }, condition: { type_id: "builtin.always", version: "1" },
    context: { type_id: "builtin.literal", version: "1", content: "原内容" }, action: { type_id: "builtin.runtime_source", version: "1", target_uri: "boxteam://workspace/backend-one/resources/session/session-one/threads/thread-one", delivery_mode: "context_only", delivery_policy: "after_turn" },
    policies: { completion: "continuous", misfire: "skip", match_mode: "each_match" },
  }, lifecycle: "active", phase: "waiting_trigger", recovering: false, created_at: "2026-10-27T00:00:00Z", updated_at: "2026-10-27T00:00:00Z",
};
let renderer: ReactTestRenderer;
let value: ReturnType<typeof usePersistentTasks>;
let records: api.PersistentTaskDTO[];
const streams: Array<{ signal: AbortSignal; publish: (items: api.PersistentTaskDTO[]) => void }> = [];
const restores: Array<() => void> = [];
function Probe({ owner }: { owner: GatewayWorkspaceRequestTarget }) { value = usePersistentTasks(8014, owner); return null; }
async function mount() { await act(async () => { renderer = create(<Probe owner={target} />); }); }

beforeEach(() => {
  records = [original]; streams.length = 0;
  const watch = spyOn(api, "watchPersistentTasks").mockImplementation(async (_port, _owner, signal, publish) => {
    streams.push({ signal, publish }); publish(records);
    await new Promise<void>((resolve) => signal.addEventListener("abort", () => resolve(), { once: true }));
  });
  const list = spyOn(api, "listPersistentTasks").mockImplementation(async () => records);
  const history = spyOn(api, "listPersistentTaskRuns").mockResolvedValue([]);
  restores.push(() => watch.mockRestore(), () => list.mockRestore(), () => history.mockRestore());
});
afterEach(() => { act(() => renderer?.unmount()); restores.splice(0).forEach((restore) => restore()); });

test("切换后端工作区后不保留旧详情，旧订阅和动作闭包均不能回写", async () => {
  const control = spyOn(api, "controlPersistentTask").mockResolvedValue(original);
  restores.push(() => control.mockRestore());
  await mount();
  await act(async () => { value.select(original.task_id); });
  expect(value.selected?.task_id).toBe(original.task_id);
  const previous = value;
  const stream = streams[0];
  records = [{ ...original, workspace_id: "backend-two", task_id: "task-two" }];
  await act(async () => { renderer.update(<Probe owner={{ ...target, backend_workspace_id: "backend-two" }} />); });
  expect(stream.signal.aborted).toBe(true);
  expect(value.selected).toBeNull();
  await act(async () => { stream.publish([original]); await previous.control(original, "pause"); });
  expect(value.items).toEqual(records);
  expect(control).not.toHaveBeenCalled();
});

test("revision冲突后读回完整对象，保留后端错误且不伪造暂停", async () => {
  const control = spyOn(api, "controlPersistentTask").mockRejectedValue(new Error("revision已变化"));
  restores.push(() => control.mockRestore());
  await mount();
  records = [{ ...original, revision: 2, definition_revision: 2, reason: "另一客户端已编辑", configuration: { ...original.configuration!, name: "完整新任务", context: { ...original.configuration!.context!, content: "完整新正文" } } }];
  await act(async () => { expect(await value.control(original, "pause")).toBe(false); });
  expect(value.error).toContain("revision已变化");
  expect(value.items).toEqual(records);
  expect(value.items[0].lifecycle).toBe("active");
  expect(value.busy).toBe(false);
  expect(control.mock.calls[0][2]).toBe(original);
});

test("写入停止旧订阅；恢复中的暂停使用完整回执，迟到快照不能重新激活", async () => {
  records = [{ ...original, phase: "recovering", recovering: true }];
  const paused = { ...original, revision: 2, phase: "recovering", recovering: true, lifecycle: "paused" };
  const control = spyOn(api, "controlPersistentTask").mockImplementation(async () => { records = [paused]; return paused; });
  restores.push(() => control.mockRestore());
  await mount();
  const stream = streams[0];
  await act(async () => { await value.control(value.items[0], "pause"); stream.publish([original]); });
  expect(stream.signal.aborted).toBe(true);
  expect(value.items).toEqual([paused]);
  await act(async () => { streams[streams.length - 1].publish([{ ...paused, phase: "paused", recovering: false }]); });
  expect(value.items[0].lifecycle).toBe("paused");
  expect(value.items[0].recovering).toBe(false);
});
