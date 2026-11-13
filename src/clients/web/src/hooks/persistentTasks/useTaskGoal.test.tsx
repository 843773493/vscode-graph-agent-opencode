import { afterEach, expect, spyOn, test } from "bun:test";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import * as api from "../../api/session/sessionGoals";
import type { SessionGoal, SessionGoalSnapshotDTO } from "../../types/backend";
import { useTaskGoal } from "./useTaskGoal";

const original: SessionGoal = {
  goal_id: "goal-one", session_id: "session-one", thread_id: "main-one", objective: "原目标", status: "paused",
  token_budget: 1000, tokens_used: 12, time_used_seconds: 3,
  created_at: "2026-10-29T00:00:00Z", updated_at: "2026-10-29T00:00:00Z",
  revision: 1, control_revision: 1, sync_version: 1,
};
const snapshot = (goal: SessionGoal | null): SessionGoalSnapshotDTO => ({
  session_id: original.session_id, thread_id: original.thread_id, goal,
  control_revision: goal?.control_revision ?? 0, sync_version: goal?.sync_version ?? 0,
});
const result = (goal: SessionGoal) => ({
  session_id: goal.session_id, thread_id: goal.thread_id, operation_id: "operation-one",
  status: "applied" as const, control_revision: goal.control_revision,
  sync_version: goal.sync_version, goal,
});
let renderer: ReactTestRenderer;
let state: ReturnType<typeof useTaskGoal>;
const restores: Array<() => void> = [];
function Probe({ revision = 1 }: { revision?: number }) { state = useTaskGoal(8111, "route-one", original.session_id, revision); return null; }
afterEach(() => { act(() => renderer?.unmount()); restores.splice(0).forEach((restore) => restore()); });

test("详情按精确工作区读取；迟到的旧读取不能覆盖完整写入结果", async () => {
  let finishOld: (goal: SessionGoalSnapshotDTO) => void = () => { throw new Error("旧读取尚未启动"); };
  const updated = { ...original, objective: "新目标", tokens_used: 25 };
  const read = spyOn(api, "getSessionGoal").mockResolvedValue(snapshot(original));
  const write = spyOn(api, "updateSessionGoal").mockResolvedValue(result(updated));
  restores.push(() => read.mockRestore(), () => write.mockRestore());
  await act(async () => { renderer = create(<Probe />); });
  expect(read.mock.calls[0]).toEqual([8111, original.session_id, "route-one", expect.any(AbortSignal), 0]);
  read.mockImplementationOnce(() => new Promise((resolve) => { finishOld = resolve; }));
  let oldRequest: Promise<SessionGoal | null>;
  act(() => { oldRequest = state.refresh(); });
  read.mockResolvedValue(snapshot(updated));
  await act(async () => { await state.update({ objective: "新目标" }); finishOld(snapshot(original)); await oldRequest; });
  expect(state.goal).toEqual(updated);
  expect(state.loading).toBe(false);
  expect(write.mock.calls[0]?.[2]).toMatchObject({ objective: "新目标", expected_control_revision: 1 });
});

test("写入失败必须回读后端，保留失败原因；其他面板的 revision 会刷新详情", async () => {
  const read = spyOn(api, "getSessionGoal").mockResolvedValue(snapshot(original));
  const write = spyOn(api, "updateSessionGoal").mockRejectedValue(new Error("后端拒绝修改"));
  const receipt = spyOn(api, "getSessionGoalOperation").mockResolvedValue({
    session_id: original.session_id, thread_id: original.thread_id, operation_id: "op_unknown",
    status: "unknown", control_revision: original.control_revision,
    sync_version: original.sync_version, goal: original,
  });
  restores.push(() => read.mockRestore(), () => write.mockRestore(), () => receipt.mockRestore());
  await act(async () => { renderer = create(<Probe />); });
  const external = { ...original, objective: "另一面板修改的目标" };
  read.mockResolvedValue(snapshot(external));
  await act(async () => { await expect(state.update({ status: "active" })).rejects.toThrow("后端拒绝修改"); });
  expect(state.goal).toEqual(external);
  expect(state.goal?.status).toBe("paused");
  expect(state.error).toBe("后端拒绝修改");
  read.mockResolvedValue(snapshot({ ...external, tokens_used: 45 }));
  await act(async () => { renderer.update(<Probe revision={3} />); });
  expect(state.goal?.tokens_used).toBe(45);
  expect(state.error).toBeNull();
});

test("详情写入回包丢失时按原操作回执收敛", async () => {
  const updated = { ...original, objective: "其他客户端已提交" };
  const read = spyOn(api, "getSessionGoal")
    .mockResolvedValueOnce(snapshot(original))
    .mockResolvedValue(snapshot(updated));
  const write = spyOn(api, "updateSessionGoal").mockRejectedValue(new TypeError("响应连接中断"));
  const receipt = spyOn(api, "getSessionGoalOperation").mockImplementation(async (_port, _session, operationId) => ({
    ...result(updated), operation_id: operationId,
  }));
  restores.push(() => read.mockRestore(), () => write.mockRestore(), () => receipt.mockRestore());
  await act(async () => { renderer = create(<Probe />); });
  await act(async () => { await state.update({ objective: "新目标" }); });
  expect(state.goal).toEqual(updated);
  expect(state.error).toBeNull();
  expect(write.mock.calls[0]?.[2]).toMatchObject({ expected_control_revision: original.control_revision });
  expect(receipt.mock.calls[0]?.[2]).toBe(write.mock.calls[0]?.[2].operation_id);
});

test("详情先设置后清除串行发送，并以设置回执版本清除", async () => {
  const updated = { ...original, objective: "排队的设置", control_revision: 2, sync_version: 2 };
  let release!: () => void;
  const read = spyOn(api, "getSessionGoal").mockResolvedValue(snapshot(original));
  const write = spyOn(api, "updateSessionGoal").mockImplementation(async (_port, _session, payload) => {
    await new Promise<void>((resolve) => { release = resolve; });
    read.mockResolvedValue(snapshot(updated));
    return { ...result(updated), operation_id: payload.operation_id };
  });
  const clear = spyOn(api, "clearSessionGoal").mockImplementation(async (_port, _session, operation) => {
    const cleared = { session_id: original.session_id, thread_id: original.thread_id, goal: null,
      control_revision: 3, sync_version: 3 };
    read.mockResolvedValue(cleared);
    return { ...cleared, status: "applied", operation_id: operation.operation_id };
  });
  restores.push(() => read.mockRestore(), () => write.mockRestore(), () => clear.mockRestore());
  await act(async () => { renderer = create(<Probe />); });
  let update!: Promise<SessionGoal>;
  let clearing!: Promise<void>;
  await act(async () => { update = state.update({ objective: updated.objective }); clearing = state.clear(); await Promise.resolve(); });
  expect(clear).not.toHaveBeenCalled();
  await act(async () => { release(); await Promise.all([update, clearing]); });
  expect(clear.mock.calls[0]?.[2].expected_control_revision).toBe(2);
  expect(state.goal).toBeNull();
  expect(state.controlRevision).toBe(3);
});

test("详情控制冲突停止已经排队的依赖写入", async () => {
  const read = spyOn(api, "getSessionGoal").mockResolvedValue(snapshot(original));
  let reject!: (error: Error) => void;
  const write = spyOn(api, "updateSessionGoal").mockImplementation(() => new Promise((_resolve, rejectWrite) => { reject = rejectWrite; }));
  const clear = spyOn(api, "clearSessionGoal");
  const receipt = spyOn(api, "getSessionGoalOperation").mockImplementation(async (_port, _session, operationId) => ({
    ...snapshot(original), operation_id: operationId, status: "unknown",
  }));
  restores.push(() => read.mockRestore(), () => write.mockRestore(), () => clear.mockRestore(), () => receipt.mockRestore());
  await act(async () => { renderer = create(<Probe />); });
  let operations!: Promise<PromiseSettledResult<unknown>[]>;
  await act(async () => {
    operations = Promise.allSettled([state.update({ objective: "保留输入" }), state.clear()]);
    await Promise.resolve();
  });
  await act(async () => { reject(new Error("409 控制版本冲突")); await operations; });
  expect((await operations).map((item) => item.status)).toEqual(["rejected", "rejected"]);
  expect(clear).not.toHaveBeenCalled();
  expect(state.error).toContain("409 控制版本冲突");
  expect(state.goal).toEqual(original);
});
