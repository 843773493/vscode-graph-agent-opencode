import { afterEach, beforeEach, expect, spyOn, test } from "bun:test";
import { act } from "react-test-renderer";
import * as tasks from "../../api/persistentTasks";
import * as controls from "../../api/session/sessionGoals";
import { gatewayWorkspaceKey } from "../../api/workspaceIdentity";
import { INITIAL_APP_STATE } from "../app/appStateSeed";
import { cloneMaps } from "../../state/appStateMaps";
import { createSessionGoalControllerHarness } from "../../tests/fixtures/session/sessionGoalController";
import { mountReactTestHarness } from "../../tests/fixtures/session/reactHarness";
import { installGatewayTestWindow, installVisibleTestDocument } from "../../tests/fixtures/session/gatewayBrowserGlobals";
import { restoreGatewayTestEnvironment } from "../../tests/fixtures/session/gatewayEnvironment";
import type { SessionGoal, SessionGoalSnapshotDTO } from "../../types/backend";

const workspace = gatewayWorkspaceKey({ gatewayId: "gateway-one", workspaceId: "workspace-one" });
const goal: SessionGoal = { goal_id: "goal-one", session_id: "session-one", thread_id: "main-one", objective: "统一任务目标", status: "paused",
  token_budget: null, tokens_used: 0, time_used_seconds: 0, revision: 1, control_revision: 1, sync_version: 1,
  created_at: "2026-11-15T00:00:00Z", updated_at: "2026-11-15T00:00:00Z" };
const slot = (value: SessionGoal | null, version = 0): SessionGoalSnapshotDTO => ({ session_id: goal.session_id, thread_id: goal.thread_id,
  goal: value, control_revision: value?.control_revision ?? version, sync_version: value?.sync_version ?? version });
const snapshot = (value: SessionGoalSnapshotDTO): tasks.PersistentTaskSnapshot => ({ items: [], active_runs: [],
  runtime: { state: "running", recovering_tasks: 0 }, session_goal: value });
const restores: Array<() => void> = [];
let unmount: (() => void) | undefined;
beforeEach(() => { installGatewayTestWindow(49_406); installVisibleTestDocument(); });
afterEach(() => { unmount?.(); restores.splice(0).forEach(restore => restore()); restoreGatewayTestEnvironment(); });
async function mount() {
  const initial = { ...cloneMaps(INITIAL_APP_STATE), currentSessionWorkspaceId: workspace,
    currentSession: { session_id: goal.session_id, thread_id: goal.thread_id, workspace_id: "workspace-one", resource_identity: "session:one", uri: "boxteam://workspace/workspace-one/resources/session/session-one", scope: "workspace", title: "会话", current_agent_id: "default",
      created_at: goal.created_at, updated_at: goal.updated_at } };
  const mounted = createSessionGoalControllerHarness({ apiPort: 49_406, initial });
  unmount = await mountReactTestHarness(mounted.Harness, 1);
  return mounted;
}

test("会话镜像只消费持续任务通知，预算和清空同步且旧版本不复活", async () => {
  let publish!: (value: tasks.PersistentTaskSnapshot) => void;
  const watch = spyOn(tasks, "watchPersistentTasks").mockImplementation(async (_p, _t, signal, accept) => {
    publish = accept; accept(snapshot(slot(null)));
    await new Promise<void>(resolve => signal.addEventListener("abort", () => resolve(), { once: true }));
  });
  restores.push(() => watch.mockRestore());
  const mounted = await mount();
  expect(mounted.state().goalLoading).toBe(false);
  act(() => publish(snapshot(slot({ ...goal, status: "usage_limited", tokens_used: 100, sync_version: 4 }))));
  expect(mounted.state().currentGoal?.status).toBe("usage_limited");
  expect(mounted.state().currentGoal?.tokens_used).toBe(100);
  act(() => publish(snapshot(slot(null, 5))));
  act(() => publish(snapshot(slot(goal))));
  expect(mounted.state().currentGoal).toBeNull();
  expect(mounted.state().currentGoalSyncVersion).toBe(5);
});

test("输入区控制直接采用完整回执，拒绝错误 Thread 的命令", async () => {
  const watch = spyOn(tasks, "watchPersistentTasks").mockImplementation(async (_p, _t, signal, accept) => {
    accept(snapshot(slot(goal))); await new Promise<void>(resolve => signal.addEventListener("abort", () => resolve(), { once: true }));
  });
  const updated = { ...goal, status: "complete" as const, control_revision: 2, sync_version: 2 };
  const write = spyOn(controls, "updateSessionGoal").mockResolvedValue({ ...slot(updated), operation_id: "op", status: "applied" });
  restores.push(() => watch.mockRestore(), () => write.mockRestore());
  const mounted = await mount();
  expect(() => mounted.controller().updateGoal({ status: "active" }, { sessionId: goal.session_id, threadId: "child", workspaceId: workspace }))
    .toThrow("同步读取");
  expect(write).not.toHaveBeenCalled();
  await act(async () => { await mounted.controller().updateGoal({ status: "complete" }); });
  expect(mounted.state().currentGoal).toEqual(updated);
  expect(mounted.state().currentGoalControlRevision).toBe(2);
});

test("同名会话切换目标后保留新目标版本零，旧通知和旧动作失效", async () => {
  const callbacks: Array<(value: tasks.PersistentTaskSnapshot) => void> = [];
  const watch = spyOn(tasks, "watchPersistentTasks").mockImplementation(async (_p, _t, signal, accept) => {
    callbacks.push(accept); accept(snapshot(slot(callbacks.length === 1 ? goal : null)));
    await new Promise<void>(resolve => signal.addEventListener("abort", () => resolve(), { once: true }));
  });
  restores.push(() => watch.mockRestore());
  const mounted = await mount();
  const old = mounted.controller();
  const otherWorkspace = gatewayWorkspaceKey({ gatewayId: "other-gateway", workspaceId: "workspace-one" });
  act(() => mounted.setState(previous => ({ ...previous, currentSessionWorkspaceId: otherWorkspace,
    currentGoal: null, currentGoalSyncVersion: 0, currentGoalControlRevision: 0 })));
  act(() => callbacks[0]!(snapshot(slot({ ...goal, sync_version: 99 }))));
  expect(mounted.state().currentGoal).toBeNull();
  expect(mounted.state().currentGoalSyncVersion).toBe(0);
  await expect(old.clearGoal()).rejects.toThrow("同步读取");
});
