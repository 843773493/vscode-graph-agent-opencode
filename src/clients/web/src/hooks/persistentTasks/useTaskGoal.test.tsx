import { afterEach, expect, spyOn, test } from "bun:test";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import * as api from "../../api/session/sessionGoals";
import type { SessionGoal } from "../../types/backend";
import { useTaskGoal } from "./useTaskGoal";

const original: SessionGoal = {
  goal_id: "goal-one", session_id: "session-one", thread_id: "main-one", objective: "原目标", status: "paused",
  token_budget: 1000, tokens_used: 12, time_used_seconds: 3,
  created_at: "2026-10-29T00:00:00Z", updated_at: "2026-10-29T00:00:00Z",
};
let renderer: ReactTestRenderer;
let state: ReturnType<typeof useTaskGoal>;
const restores: Array<() => void> = [];
function Probe({ revision = 1 }: { revision?: number }) { state = useTaskGoal(8111, "route-one", original.session_id, revision); return null; }
afterEach(() => { act(() => renderer?.unmount()); restores.splice(0).forEach((restore) => restore()); });

test("详情按精确工作区读取；迟到的旧读取不能覆盖完整写入结果", async () => {
  let finishOld: (goal: SessionGoal) => void = () => { throw new Error("旧读取尚未启动"); };
  const updated = { ...original, objective: "新目标", tokens_used: 25 };
  const read = spyOn(api, "getSessionGoal").mockResolvedValue(original);
  const write = spyOn(api, "updateSessionGoal").mockResolvedValue(updated);
  restores.push(() => read.mockRestore(), () => write.mockRestore());
  await act(async () => { renderer = create(<Probe />); });
  expect(read.mock.calls[0]).toEqual([8111, original.session_id, "route-one"]);
  read.mockImplementationOnce(() => new Promise((resolve) => { finishOld = resolve; }));
  let oldRequest: Promise<SessionGoal | null>;
  act(() => { oldRequest = state.refresh(); });
  read.mockResolvedValue(updated);
  await act(async () => { await state.update({ objective: "新目标" }); finishOld(original); await oldRequest; });
  expect(state.goal).toEqual(updated);
  expect(state.loading).toBe(false);
  expect(write.mock.calls[0]).toEqual([8111, original.session_id, { objective: "新目标" }, "route-one"]);
});

test("写入失败必须回读后端，保留失败原因；其他面板的 revision 会刷新详情", async () => {
  const read = spyOn(api, "getSessionGoal").mockResolvedValue(original);
  const write = spyOn(api, "updateSessionGoal").mockRejectedValue(new Error("后端拒绝修改"));
  restores.push(() => read.mockRestore(), () => write.mockRestore());
  await act(async () => { renderer = create(<Probe />); });
  const external = { ...original, objective: "另一面板修改的目标" };
  read.mockResolvedValue(external);
  await act(async () => { await expect(state.update({ status: "active" })).rejects.toThrow("后端拒绝修改"); });
  expect(state.goal).toEqual(external);
  expect(state.goal?.status).toBe("paused");
  expect(state.error).toBe("后端拒绝修改");
  read.mockResolvedValue({ ...external, tokens_used: 45 });
  await act(async () => { renderer.update(<Probe revision={3} />); });
  expect(state.goal?.tokens_used).toBe(45);
  expect(state.error).toBeNull();
});
