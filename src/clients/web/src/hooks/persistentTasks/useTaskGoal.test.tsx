import { afterEach, beforeEach, expect, spyOn, test } from "bun:test";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import * as controls from "../../api/session/sessionGoals";
import * as tasks from "../../api/persistentTasks";
import { gatewayWorkspaceKey } from "../../api/workspaceIdentity";
import type { SessionGoal, SessionGoalOperationResultDTO, SessionGoalSnapshotDTO } from "../../types/backend";
import { installGatewayTestWindow, installVisibleTestDocument } from "../../tests/fixtures/session/gatewayBrowserGlobals";
import { restoreGatewayTestEnvironment } from "../../tests/fixtures/session/gatewayEnvironment";
import { useTaskGoal } from "./useTaskGoal";

const workspace = gatewayWorkspaceKey({ gatewayId: "gateway-one", workspaceId: "workspace-one" });
const original: SessionGoal = {
  goal_id: "goal-one", session_id: "session-one", thread_id: "main-one", objective: "原目标", status: "paused",
  token_budget: 1000, tokens_used: 12, time_used_seconds: 3,
  created_at: "2026-10-29T00:00:00Z", updated_at: "2026-10-29T00:00:00Z",
  revision: 1, control_revision: 1, sync_version: 1,
};
const snapshot = (goal: SessionGoal | null, version = 0): SessionGoalSnapshotDTO => ({
  session_id: original.session_id, thread_id: original.thread_id, goal,
  control_revision: goal?.control_revision ?? version, sync_version: goal?.sync_version ?? version,
});
const list = (value = snapshot(original)): tasks.PersistentTaskSnapshot => ({
  items: [], active_runs: [], runtime: { state: "running", recovering_tasks: 0 }, session_goal: value,
});
const receipt = (value: SessionGoalSnapshotDTO, operationId = "operation-one") => ({
  ...value, status: "applied" as const, operation_id: operationId,
});
let renderer: ReactTestRenderer;
let state: ReturnType<typeof useTaskGoal>;
const restores: Array<() => void> = [];
function Probe({ seed, owner = workspace, active = true }: { seed?: SessionGoalSnapshotDTO; owner?: string; active?: boolean }) {
  state = useTaskGoal(8111, owner, original.session_id, { seed, active, mainThreadId: original.thread_id });
  return null;
}
beforeEach(() => { installGatewayTestWindow(8111); installVisibleTestDocument(); });
afterEach(() => { act(() => renderer?.unmount()); restores.splice(0).forEach(restore => restore()); restoreGatewayTestEnvironment(); });

test("已有任务详情直接消费父列表；成功控制不补读，旧读取不能覆盖新回执", async () => {
  const read = spyOn(tasks, "listPersistentTasks").mockResolvedValue(list());
  const watch = spyOn(tasks, "watchPersistentTasks");
  const updated = { ...original, objective: "新目标", sync_version: 2, control_revision: 2 };
  const write = spyOn(controls, "updateSessionGoal").mockResolvedValue(receipt(snapshot(updated)));
  restores.push(() => read.mockRestore(), () => watch.mockRestore(), () => write.mockRestore());
  await act(async () => { renderer = create(<Probe seed={snapshot(original)} />); });
  expect(read).not.toHaveBeenCalled(); expect(watch).not.toHaveBeenCalled();
  let finish!: (value: tasks.PersistentTaskSnapshot) => void;
  read.mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
  let oldRequest!: Promise<SessionGoal | null>;
  await act(async () => { oldRequest = state.refresh(); await Promise.resolve(); });
  await act(async () => { await state.update({ objective: "新目标" }); finish(list()); await oldRequest; });
  expect(state.goal).toEqual(updated);
  expect(read).toHaveBeenCalledTimes(1);
  expect(read.mock.calls[0]?.[1]).toEqual({ gateway_id: "gateway-one", workspace_id: "workspace-one" });
  expect(write.mock.calls[0]?.[2]).toMatchObject({ expected_control_revision: 1 });
});

test("会话首次只订阅任务；无Goal、用量、预算终态和清空版本均从统一快照收敛，无固定轮询", async () => {
  let publish!: (value: tasks.PersistentTaskSnapshot) => void;
  const watch = spyOn(tasks, "watchPersistentTasks").mockImplementation(async (_port, _target, signal, onSnapshot) => {
    publish = onSnapshot; onSnapshot(list(snapshot(null)));
    await new Promise<void>(resolve => signal.addEventListener("abort", () => resolve(), { once: true }));
  });
  const read = spyOn(tasks, "listPersistentTasks");
  const interval = spyOn(window, "setInterval");
  restores.push(() => watch.mockRestore(), () => read.mockRestore(), () => interval.mockRestore());
  await act(async () => { renderer = create(<Probe />); });
  expect(state.goal).toBeNull(); expect(state.loading).toBe(false);
  const used = { ...original, status: "budget_limited" as const, tokens_used: 1000, time_used_seconds: 60, sync_version: 4 };
  act(() => publish(list(snapshot(used))));
  expect(state.goal).toEqual(used);
  act(() => publish(list(snapshot(null, 5))));
  act(() => publish(list(snapshot(original))));
  expect(state.goal).toBeNull(); expect(state.controlRevision).toBe(5);
  expect(read).not.toHaveBeenCalled(); expect(interval).not.toHaveBeenCalled();
});

test("新建Goal复用父列表的空槽位及退休版本，不增订阅且以该版本创建", async () => {
  const watch = spyOn(tasks, "watchPersistentTasks");
  const read = spyOn(tasks, "listPersistentTasks");
  const created = { ...original, control_revision: 6, sync_version: 6 };
  const write = spyOn(controls, "updateSessionGoal").mockResolvedValue(receipt(snapshot(created)));
  restores.push(() => watch.mockRestore(), () => read.mockRestore(), () => write.mockRestore());
  await act(async () => { renderer = create(<Probe seed={snapshot(null, 5)} />); });
  expect(state.goal).toBeNull(); expect(state.loading).toBe(false);
  await act(async () => { await state.update({ objective: created.objective }); });
  expect(write.mock.calls[0]?.[2].expected_control_revision).toBe(5);
  expect(watch).not.toHaveBeenCalled(); expect(read).not.toHaveBeenCalled();
});

test("写入响应丢失只查询原操作，完整回执无需额外回读", async () => {
  const updated = { ...original, sync_version: 2, control_revision: 2, objective: "已提交" };
  const write = spyOn(controls, "updateSessionGoal").mockRejectedValue(new TypeError("连接中断"));
  const query = spyOn(controls, "getSessionGoalOperation").mockImplementation(async (_p, _s, id) => receipt(snapshot(updated), id));
  const read = spyOn(tasks, "listPersistentTasks");
  restores.push(() => write.mockRestore(), () => query.mockRestore(), () => read.mockRestore());
  await act(async () => { renderer = create(<Probe seed={snapshot(original)} />); });
  await act(async () => { await state.update({ objective: updated.objective }); });
  expect(state.goal).toEqual(updated); expect(state.error).toBeNull();
  expect(query.mock.calls[0]?.[2]).toBe(write.mock.calls[0]?.[2].operation_id);
  expect(read).not.toHaveBeenCalled();
});

test("Composer 全任务镜像复用同一会话订阅，完整快照与 changed 替换或移除原任务", async () => {
  let publish!: (value: tasks.PersistentTaskSnapshot) => void;
  let changed!: (value: tasks.PersistentTaskChangedDTO) => void;
  const automation: tasks.PersistentTaskDTO = { task_id: "condition-one", workspace_id: "workspace-one", revision: 1,
    definition_revision: 1, tokens_used: 0, time_used_seconds: 0, sync_version: 1, application_type: "automation", application_version: "1", configuration: undefined,
    lifecycle: "active", phase: "waiting_trigger", recovering: false, created_at: undefined, updated_at: undefined };
  const watch = spyOn(tasks, "watchPersistentTasks").mockImplementation(async (_port, _target, signal, onSnapshot, onChanged) => {
    publish = onSnapshot; changed = onChanged;
    onSnapshot({ ...list(), items: [automation] });
    await new Promise<void>(resolve => signal.addEventListener("abort", () => resolve(), { once: true }));
  });
  const read = spyOn(tasks, "listPersistentTasks");
  restores.push(() => watch.mockRestore(), () => read.mockRestore());
  await act(async () => { renderer = create(<Probe />); });
  expect(state.tasksLoaded).toBe(true); expect(state.tasks).toEqual([automation]);
  const updated = { ...automation, revision: 2, lifecycle: "paused" };
  act(() => changed({ task_id: automation.task_id, task: updated }));
  expect(state.tasks).toEqual([updated]);
  act(() => changed({ task_id: "new-task", task: { ...automation, task_id: "new-task" } }));
  expect(state.tasks.map(task => task.task_id)).toEqual([automation.task_id, "new-task"]);
  act(() => changed({ task_id: automation.task_id }));
  expect(state.tasks.map(task => task.task_id)).toEqual(["new-task"]);
  act(() => publish({ ...list(), items: [] }));
  expect(state.tasks).toEqual([]); expect(watch).toHaveBeenCalledTimes(1);
  expect(watch.mock.calls[0]?.[5]).toBe(original.session_id); expect(read).not.toHaveBeenCalled();
});

test("设置与清空串行，清空使用最新回执版本", async () => {
  let release!: () => void;
  const updated = { ...original, sync_version: 2, control_revision: 2 };
  const write = spyOn(controls, "updateSessionGoal").mockImplementation(async () => {
    await new Promise<void>(resolve => { release = resolve; }); return receipt(snapshot(updated));
  });
  const clear = spyOn(controls, "clearSessionGoal").mockResolvedValue(receipt(snapshot(null, 3)));
  restores.push(() => write.mockRestore(), () => clear.mockRestore());
  await act(async () => { renderer = create(<Probe seed={snapshot(original)} />); });
  let setting!: Promise<SessionGoal>, clearing!: Promise<void>;
  await act(async () => { setting = state.update({ status: "active" }); clearing = state.clear(); await Promise.resolve(); });
  expect(clear).not.toHaveBeenCalled();
  await act(async () => { release(); await Promise.all([setting, clearing]); });
  expect(clear.mock.calls[0]?.[2].expected_control_revision).toBe(2);
  expect(state.goal).toBeNull(); expect(state.controlRevision).toBe(3);
});

test("控制冲突回读任务且停止排队的依赖写入，错误不会被重读成功掩盖", async () => {
  let reject!: (cause: Error) => void;
  const write = spyOn(controls, "updateSessionGoal").mockImplementation(() => new Promise((_resolve, fail) => { reject = fail; }));
  const clear = spyOn(controls, "clearSessionGoal");
  const external = { ...original, objective: "另一入口已改", control_revision: 3, sync_version: 3 };
  const query = spyOn(controls, "getSessionGoalOperation").mockImplementation(async (_p, _s, id) => ({ ...receipt(snapshot(external), id), status: "unknown" }));
  const read = spyOn(tasks, "listPersistentTasks").mockResolvedValue(list(snapshot(external)));
  restores.push(() => write.mockRestore(), () => clear.mockRestore(), () => query.mockRestore(), () => read.mockRestore());
  await act(async () => { renderer = create(<Probe seed={snapshot(original)} />); });
  let operations!: Promise<PromiseSettledResult<unknown>[]>;
  await act(async () => { operations = Promise.allSettled([state.update({ objective: "保留草稿" }), state.clear()]); await Promise.resolve(); });
  await act(async () => { reject(new Error("409 控制版本冲突")); await operations; });
  expect((await operations).map(item => item.status)).toEqual(["rejected", "rejected"]);
  expect(clear).not.toHaveBeenCalled(); expect(state.goal).toEqual(external);
  expect(state.error).toContain("409 控制版本冲突"); expect(read).toHaveBeenCalledTimes(1);
});

test("切换Gateway后取消旧订阅，旧读取及旧动作不能污染同名会话", async () => {
  const signals: AbortSignal[] = [];
  const callbacks: Array<(value: tasks.PersistentTaskSnapshot) => void> = [];
  const watch = spyOn(tasks, "watchPersistentTasks").mockImplementation(async (_p, _t, signal, publish) => {
    signals.push(signal); callbacks.push(publish); publish(list());
    await new Promise<void>(resolve => signal.addEventListener("abort", () => resolve(), { once: true }));
  });
  const write = spyOn(controls, "updateSessionGoal");
  restores.push(() => watch.mockRestore(), () => write.mockRestore());
  await act(async () => { renderer = create(<Probe />); });
  const oldAction = state.update;
  await act(async () => { renderer.update(<Probe owner={gatewayWorkspaceKey({ gatewayId: "other-gateway", workspaceId: "workspace-one" })} />); });
  expect(signals[0]?.aborted).toBe(true);
  act(() => callbacks[0]!(list(snapshot({ ...original, objective: "旧回包", sync_version: 99 }))));
  expect(state.goal?.objective).toBe(original.objective);
  await expect(oldAction({ status: "active" })).rejects.toThrow("同步读取");
  expect(write).not.toHaveBeenCalled();
});

test.each([false, true])("写入期间隐藏会话仍完成收尾，失败=%s", async (failed) => {
  let observed = original;
  const watch = spyOn(tasks, "watchPersistentTasks").mockImplementation(async (_p, _t, signal, publish) => {
    publish(list(snapshot(observed)));
    await new Promise<void>(resolve => signal.addEventListener("abort", () => resolve(), { once: true }));
  });
  let finish!: (value: SessionGoalOperationResultDTO) => void;
  let reject!: (cause: Error) => void;
  const write = spyOn(controls, "updateSessionGoal").mockImplementation(() => new Promise((resolve, fail) => {
    finish = resolve; reject = fail;
  }));
  const query = spyOn(controls, "getSessionGoalOperation").mockImplementation(async (_p, _s, id) => ({
    ...receipt(snapshot(original), id), status: "unknown",
  }));
  restores.push(() => watch.mockRestore(), () => write.mockRestore(), () => query.mockRestore());
  await act(async () => { renderer = create(<Probe />); });
  let pending!: Promise<PromiseSettledResult<SessionGoal>[]>;
  await act(async () => { pending = Promise.allSettled([state.update({ status: "active" })]); await Promise.resolve(); });
  expect(state.loading).toBe(true);
  await act(async () => { renderer.update(<Probe active={false} />); });
  const updated = { ...original, status: "active" as const, control_revision: 2, sync_version: 2 };
  await act(async () => {
    if (failed) reject(new Error("409 控制版本冲突"));
    else finish(receipt(snapshot(updated)));
    await pending;
  });
  observed = failed ? original : updated;
  await act(async () => { renderer.update(<Probe />); });
  expect(state.goal).toEqual(observed);
  expect(state.loading).toBe(false);
  expect((await pending)[0].status).toBe(failed ? "rejected" : "fulfilled");
  if (failed) expect(state.error).toContain("409 控制版本冲突");
  else expect(state.error).toBeNull();
});

test("订阅恢复清除连接错误，后续快照不抹去控制操作失败", async () => {
  let publish!: (value: tasks.PersistentTaskSnapshot) => void;
  const watch = spyOn(tasks, "watchPersistentTasks").mockImplementation(async (_p, _t, signal, onSnapshot) => {
    publish = onSnapshot; publish(list());
    await new Promise<void>(resolve => signal.addEventListener("abort", () => resolve(), { once: true }));
  }).mockRejectedValueOnce(new Error("旧连接失败"));
  const write = spyOn(controls, "updateSessionGoal").mockRejectedValue(new Error("409 控制版本冲突"));
  const query = spyOn(controls, "getSessionGoalOperation").mockImplementation(async (_p, _s, id) => ({
    ...receipt(snapshot(original), id), status: "unknown",
  }));
  const read = spyOn(tasks, "listPersistentTasks").mockResolvedValue(list());
  restores.push(() => watch.mockRestore(), () => write.mockRestore(), () => query.mockRestore(), () => read.mockRestore());
  await act(async () => { renderer = create(<Probe />); });
  expect(state.error).toBe("旧连接失败");
  await act(async () => { renderer.update(<Probe active={false} />); });
  await act(async () => { renderer.update(<Probe />); });
  expect(state.goal).toEqual(original); expect(state.error).toBeNull();
  await act(async () => { await expect(state.update({ status: "active" })).rejects.toThrow("409 控制版本冲突"); });
  act(() => publish(list()));
  expect(state.error).toContain("409 控制版本冲突");
});

test.each([false, true])("刷新被 changed 取代后收尾，不提前结束后续读取，后续读取=%s", async (readAgain) => {
  let changed!: (value: tasks.PersistentTaskChangedDTO) => void;
  const watch = spyOn(tasks, "watchPersistentTasks").mockImplementation(async (_p, _t, signal, publish, onChanged) => {
    changed = onChanged; publish(list());
    await new Promise<void>(resolve => signal.addEventListener("abort", () => resolve(), { once: true }));
  });
  const finishes: Array<(value: tasks.PersistentTaskSnapshot) => void> = [];
  const read = spyOn(tasks, "listPersistentTasks").mockImplementation(() => new Promise(resolve => { finishes.push(resolve); }));
  restores.push(() => watch.mockRestore(), () => read.mockRestore());
  await act(async () => { renderer = create(<Probe />); });
  let first!: Promise<SessionGoal | null>, second: Promise<SessionGoal | null> | undefined;
  await act(async () => { first = state.refresh(); await Promise.resolve(); });
  act(() => changed({ task_id: "removed-automation" }));
  if (readAgain) await act(async () => { second = state.refresh(); await Promise.resolve(); });
  await act(async () => { finishes[0](list()); await first; });
  expect(state.loading).toBe(readAgain);
  if (second) await act(async () => { finishes[1](list()); await second; });
  expect(state.loading).toBe(false); expect(state.goal).toEqual(original); expect(state.error).toBeNull();
});
