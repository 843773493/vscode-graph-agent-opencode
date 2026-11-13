import { afterEach, beforeEach, expect, mock, test } from "bun:test";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import * as api from "../../api/persistentTasks";
import type { GatewayWorkspaceRequestTarget } from "../../api/workspaceIdentity";
import { useGenerationActivity } from "./useGenerationActivity";

const target: GatewayWorkspaceRequestTarget = { gateway_id: "gateway-test", workspace_id: "owner-one" };
const task: api.PersistentTaskDTO = {
  task_id: "gen_imported", workspace_id: "owner-one", revision: 1, definition_revision: 1,
  application_type: "automation", application_version: "1", lifecycle: "active", phase: "waiting_trigger", recovering: false,
  created_at: "2026-10-27T00:00:00Z", updated_at: "2026-10-27T00:00:00Z",
  configuration: {
    name: "生成任务", trigger: { type_id: "builtin.manual", version: "1", timezone: "UTC" },
    condition: { type_id: "builtin.always", version: "1" }, context: { type_id: "builtin.literal", version: "1", content: "" },
    policies: { completion: "continuous", misfire: "skip", match_mode: "each_match" },
    action: { type_id: "builtin.session_generation", version: "1", generation: {
      generator_type: { type_id: "builtin.agent_prompt", version: "1" }, config: { prompt: "检查" },
      placement: { kind: "workspace", workspace_id: "owner-one" }, context_source: { kind: "fresh" },
      session_strategy: { mode: "new_per_run", concurrency: "queue", report_back: "none" },
      title_template: "输出", path_template: [], mount_missing: "pause", delete_outputs: "keep",
      ui_policy: { on_run_started: "open_generated", on_run_completed: "notify" },
    } },
  },
};
function run(phase: string, sessionId?: string): api.PersistentTaskRunSummaryDTO {
  return {
    run_id: "run-original", task_id: task.task_id, run_sequence: 1, definition_revision: 1,
    trigger_type: "manual", check_only: false, phase, created_at: task.created_at, updated_at: task.updated_at,
    generation: { run_id: "run-original", status: phase === "delivered" ? "completed" : "queued",
      outputs: sessionId ? [{ kind: "session", workspace_id: "owner-one", session_id: sessionId, navigation_path: [] }] : [] },
  };
}
let renderer: ReactTestRenderer;
let activity: ReturnType<typeof useGenerationActivity>;
const open = mock(async (_id: string) => {});
const status = mock((_text: string) => {});
function Probe({ owner, active = true }: { owner: GatewayWorkspaceRequestTarget; active?: boolean }) {
  activity = useGenerationActivity(8014, owner, active, { onStatusChange: status, onOpenSession: open });
  return null;
}
async function baseline(runs: api.PersistentTaskRunSummaryDTO[]) {
  await act(async () => { activity.observe({ snapshot: { items: [task], active_runs: runs, runtime: { state: "running", recovering_tasks: 0 } } }); });
}
async function changed(value: api.PersistentTaskRunSummaryDTO) {
  await act(async () => { activity.observe({ changed: { task_id: task.task_id, task, run: value } }); });
}
beforeEach(async () => {
  open.mockReset(); open.mockImplementation(async () => {}); status.mockClear();
  await act(async () => { renderer = create(<Probe owner={target} />); });
});
afterEach(() => { act(() => renderer.unmount()); });

test("queued不变时输出首次出现仍打开会话，初始输出不重放且终态通知只发生一次", async () => {
  await baseline([run("delivering", "old-output")]);
  expect(open).not.toHaveBeenCalled();
  await baseline([run("delivering")]);
  await changed(run("delivering", "session-generated"));
  await changed(run("delivering", "session-generated"));
  expect(open.mock.calls).toEqual([["session-generated"]]);
  await changed(run("delivered", "session-generated"));
  await changed(run("delivered", "session-generated"));
  expect(status.mock.calls).toEqual([["生成任务：已完成"]]);
  expect(open).toHaveBeenCalledTimes(1);
});

test("工作区切换后旧观察闭包不能触发回调，新快照只建立基线", async () => {
  await baseline([]);
  const previous = activity;
  await act(async () => { renderer.update(<Probe owner={{ ...target, workspace_id: "owner-two" }} />); });
  await baseline([run("delivering", "old-in-two")]);
  await act(async () => { previous.observe({ changed: { task_id: task.task_id, task, run: run("delivered", "late-in-one") } }); });
  expect(open).not.toHaveBeenCalled(); expect(status).not.toHaveBeenCalled();
  await changed({ ...run("delivering", "new-in-two"), run_id: "new-run", run_sequence: 2 });
  expect(open.mock.calls).toEqual([["new-in-two"]]);
});

test("隐藏后停止消费，恢复快照不重放旧输出；切换工作区立即清除旧错误", async () => {
  open.mockRejectedValueOnce(new Error("打开失败"));
  await baseline([]); await changed(run("delivering", "output"));
  expect(activity.error).toContain("打开失败");
  await act(async () => { renderer.update(<Probe owner={target} active={false} />); });
  await changed({ ...run("delivering", "hidden"), run_id: "hidden" });
  expect(open).toHaveBeenCalledTimes(1);
  expect(activity.error).toBeNull();
  await act(async () => { renderer.update(<Probe owner={target} />); });
  await baseline([run("delivering", "output")]);
  await changed(run("delivering", "output"));
  expect(open).toHaveBeenCalledTimes(1);
});
