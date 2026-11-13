import { afterEach, beforeEach, expect, spyOn, test } from "bun:test";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import * as api from "../../api/persistentTasks";
import type { GatewayWorkspaceRequestTarget } from "../../api/workspaceIdentity";
import { usePersistentTasks } from "./usePersistentTasks";

const target: GatewayWorkspaceRequestTarget = { gateway_id: "gateway-test", workspace_id: "backend-one" };
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
const healthy: api.TaskRuntimeStatusDTO = { state: "running", recovering_tasks: 0 };
const snapshot = (items: api.PersistentTaskDTO[], runtime = healthy): api.PersistentTaskSnapshot => ({ items, runtime, active_runs: [] });
const streams: Array<{ signal: AbortSignal; publish: (value: api.PersistentTaskSnapshot) => void; changed: (value: api.PersistentTaskChangedDTO) => void; sessionId?: string }> = [];
const restores: Array<() => void> = [];
function Probe({ owner, sessionId, active = true }: { owner: GatewayWorkspaceRequestTarget; sessionId?: string; active?: boolean }) { value = usePersistentTasks(8014, owner, sessionId, active); return null; }
async function mount() { await act(async () => { renderer = create(<Probe owner={target} />); }); }

beforeEach(() => {
  records = [original]; streams.length = 0;
  const watch = spyOn(api, "watchPersistentTasks").mockImplementation(async (_port, _owner, signal, publish, changed, sessionId) => {
    streams.push({ signal, publish, changed, sessionId }); publish(snapshot(records));
    await new Promise<void>((resolve) => signal.addEventListener("abort", () => resolve(), { once: true }));
  });
  const list = spyOn(api, "listPersistentTasks").mockImplementation(async () => snapshot(records));
  const history = spyOn(api, "listPersistentTaskRuns").mockResolvedValue({ items: [] });
  restores.push(() => watch.mockRestore(), () => list.mockRestore(), () => history.mockRestore());
});
afterEach(() => { act(() => renderer?.unmount()); restores.splice(0).forEach((restore) => restore()); });

test("切换关联会话撤销旧状态流，迟到的原会话快照不能覆盖新会话", async () => {
  await act(async () => { renderer = create(<Probe owner={target} sessionId="session-one" />); });
  const previous = streams[0];
  records = [];
  await act(async () => { renderer.update(<Probe owner={target} sessionId="session-two" />); });
  expect(previous.signal.aborted).toBe(true);
  expect(streams[streams.length - 1].sessionId).toBe("session-two");
  await act(async () => { previous.publish(snapshot([original])); });
  expect(value.items).toEqual([]);
  expect(value.loaded).toBe(true);
});

test("隐藏编辑器停止订阅并保留选中任务，重新显示后读取新快照", async () => {
  await mount();
  await act(async () => { value.select(original.task_id); });
  const previous = streams[0];
  await act(async () => { renderer.update(<Probe owner={target} active={false} />); });
  expect(previous.signal.aborted).toBe(true);
  expect(streams).toHaveLength(1);
  expect(value.selected?.task_id).toBe(original.task_id);
  await act(async () => { previous.publish(snapshot([])); });
  expect(value.items).toEqual([original]);
  records = [{ ...original, revision: 2, lifecycle: "paused" }];
  await act(async () => { renderer.update(<Probe owner={target} />); });
  expect(streams).toHaveLength(2);
  expect(streams[1].signal.aborted).toBe(false);
  expect(value.selected).toEqual(records[0]);
});

test("切换后端工作区后不保留旧详情，旧订阅和动作闭包均不能回写", async () => {
  const control = spyOn(api, "controlPersistentTask").mockResolvedValue(original);
  restores.push(() => control.mockRestore());
  await mount();
  await act(async () => { value.select(original.task_id); });
  expect(value.selected?.task_id).toBe(original.task_id);
  const previous = value;
  const stream = streams[0];
  records = [{ ...original, workspace_id: "backend-two", task_id: "task-two" }];
  await act(async () => { renderer.update(<Probe owner={{ ...target, workspace_id: "backend-two" }} />); });
  expect(stream.signal.aborted).toBe(true);
  expect(value.selected).toBeNull();
  await act(async () => { stream.publish(snapshot([original])); await previous.run(original, "pause"); });
  expect(value.items).toEqual(records);
  expect(control).not.toHaveBeenCalled();
});

test("revision冲突后读回完整对象，保留后端错误且不伪造暂停", async () => {
  const control = spyOn(api, "controlPersistentTask").mockRejectedValue(new Error("revision已变化"));
  restores.push(() => control.mockRestore());
  await mount();
  records = [{ ...original, revision: 2, definition_revision: 2, reason: "另一客户端已编辑", configuration: { ...original.configuration!, name: "完整新任务", context: { ...original.configuration!.context!, content: "完整新正文" } } }];
  await act(async () => { expect(await value.run(original, "pause")).toBe(false); });
  expect(value.error).toContain("revision已变化");
  expect(value.items).toEqual(records);
  expect(value.items[0].lifecycle).toBe("active");
  expect(value.busy).toBe(false);
  expect(control.mock.calls[0][2]).toBe(original);
});

test("写入保持状态订阅，较早操作回执不能覆盖已收到的新状态", async () => {
  records = [{ ...original, phase: "recovering", recovering: true }];
  const paused = { ...original, revision: 2, phase: "recovering", recovering: true, lifecycle: "paused" };
  const settled = { ...paused, phase: "paused", recovering: false };
  const control = spyOn(api, "controlPersistentTask").mockImplementation(async () => {
    records = [paused];
    streams[0].changed({ task_id: original.task_id, task: settled });
    return paused;
  });
  restores.push(() => control.mockRestore());
  await mount();
  const stream = streams[0];
  await act(async () => { await value.run(value.items[0], "pause"); });
  expect(stream.signal.aborted).toBe(false);
  expect(streams).toHaveLength(1);
  expect(value.items).toEqual([settled]);
});

test("worker失败由完整快照替换，刷新和空列表均不能伪造恢复成功", async () => {
  await mount();
  expect(value.runtime).toEqual(healthy);
  const failed: api.TaskRuntimeStatusDTO = { state: "failed", recovering_tasks: 2, error: "持久任务后台运行失败（RuntimeError）" };
  await act(async () => { streams[0].publish(snapshot([], failed)); });
  expect(value.runtime).toEqual(failed);
  expect(value.items).toEqual([]);
  const list = spyOn(api, "listPersistentTasks").mockResolvedValue(snapshot([], failed));
  const watch = spyOn(api, "watchPersistentTasks").mockImplementation(async (_port, _owner, signal, publish) => {
    publish(snapshot([], failed));
    await new Promise<void>((resolve) => signal.addEventListener("abort", () => resolve(), { once: true }));
  });
  restores.push(() => list.mockRestore(), () => watch.mockRestore());
  await act(async () => { await value.refresh(); });
  expect(value.runtime).toEqual(failed);
});

test("Goal 详情也从同一任务 ID 读取运行记录", async () => {
  records = [{ ...original, application_type: "goal", configuration: undefined }];
  await act(async () => { renderer = create(<Probe owner={target} sessionId="session-one" />); });
  await act(async () => { value.select(original.task_id); });
  expect(api.listPersistentTaskRuns).toHaveBeenCalledWith(8014, target, original.task_id, expect.any(AbortSignal), undefined);
  expect(value.history?.runs).toEqual([]);
  expect(value.history?.loading).toBe(false);
});


test("分页保持接受顺序，实时更新只替换对应运行且不重读历史", async () => {
  const run = (id: string, sequence: number, phase = "delivering"): api.PersistentTaskRunSummaryDTO => ({
    run_id: id, task_id: original.task_id, run_sequence: sequence, definition_revision: 1,
    check_only: false, phase, trigger_type: "manual", created_at: original.created_at, updated_at: original.updated_at,
  });
  const history = spyOn(api, "listPersistentTaskRuns").mockResolvedValue({ items: [run("second", 2)], next_before: "second" });
  restores.push(() => history.mockRestore());
  await mount();
  await act(async () => { value.select(original.task_id); });
  expect(value.history?.runs.map(item => item.run_id)).toEqual(["second"]);
  const reads = history.mock.calls.length;
  await act(async () => {
    streams[0].changed({ task_id: "other", task: { ...original, task_id: "other" } });
    streams[0].changed({ task_id: original.task_id, task: original, run: run("second", 2, "delivered") });
    streams[0].changed({ task_id: original.task_id, task: original, run: run("third", 3) });
  });
  expect(history.mock.calls.length).toBe(reads);
  expect(value.history?.runs.map(item => [item.run_id, item.phase])).toEqual([["second", "delivered"], ["third", "delivering"]]);
  history.mockResolvedValueOnce({ items: [run("first", 1)] });
  await act(async () => { value.loadOlder(); });
  expect(history.mock.calls.at(-1)?.[4]).toBe("second");
  expect(value.history?.runs.map(item => item.run_id)).toEqual(["first", "second", "third"]);
  expect(value.history?.nextBefore).toBeUndefined();
});
