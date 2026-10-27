import { afterEach, beforeEach, expect, mock, spyOn, test } from "bun:test";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import * as api from "../../api/persistentTasks";
import type { GatewayWorkspaceRequestTarget } from "../../api/workspaceIdentity";
import { useGenerationActivity } from "./useGenerationActivity";

const target: GatewayWorkspaceRequestTarget = { workspace_id: "route-one", backend_workspace_id: "owner-one", connection_kind: "local" };
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
function run(phase: string, sessionId?: string): api.PersistentTaskRunDTO {
  return {
    run_id: "run-original", task_id: task.task_id, task_revision: 1, definition_revision: 1, trigger_key: "trigger-original",
    trigger_type: "manual", check_only: false, phase, created_at: task.created_at, updated_at: task.updated_at,
    generation: { run_id: "run-original", status: phase === "delivered" ? "completed" : "queued",
      outputs: sessionId ? [{ kind: "session", workspace_id: "owner-one", session_id: sessionId, navigation_path: [] }] : [] },
  };
}
let renderer: ReactTestRenderer;
let history: api.PersistentTaskRunDTO[];
let items: api.PersistentTaskDTO[];
let error: string | null;
const open = mock(async (_id: string) => {});
const status = mock((_text: string) => {});
let reader: ReturnType<typeof spyOn<typeof api, "listPersistentTaskRuns">>;
function Probe({ owner, loaded = true }: { owner: GatewayWorkspaceRequestTarget; loaded?: boolean }) {
  error = useGenerationActivity(8014, owner, items, loaded,
    { onStatusChange: status, onOpenSession: open });
  return null;
}
async function update(owner = target, loaded = true) {
  items = [{ ...task, workspace_id: owner.backend_workspace_id! }];
  await act(async () => { renderer.update(<Probe owner={owner} loaded={loaded} />); });
}
beforeEach(async () => {
  history = []; items = [task]; open.mockClear(); status.mockClear();
  reader = spyOn(api, "listPersistentTaskRuns").mockImplementation(async () => history);
  await act(async () => { renderer = create(<Probe owner={target} />); });
});
afterEach(() => { act(() => renderer.unmount()); reader.mockRestore(); });

test("queued不变时输出首次出现仍打开会话，终态通知只发生一次", async () => {
  history = [run("delivering")]; await update();
  expect(open).not.toHaveBeenCalled();
  history = [run("delivering", "session-generated")]; await update();
  expect(open.mock.calls).toEqual([["session-generated"]]);
  await update();
  expect(open).toHaveBeenCalledTimes(1);
  history = [run("delivered", "session-generated")]; await update();
  expect(status.mock.calls).toEqual([["生成任务：已完成"]]);
  await update();
  expect(status).toHaveBeenCalledTimes(1);
  expect(open).toHaveBeenCalledTimes(1);
});

test("切换工作区后同名历史只建立新基线，旧请求迟到不能触发当前回调", async () => {
  let finish: (value: api.PersistentTaskRunDTO[]) => void = () => { throw new Error("旧请求未开始"); };
  reader.mockImplementationOnce(() => new Promise((resolve) => { finish = resolve; }));
  await update();
  const signal = reader.mock.calls[reader.mock.calls.length - 1][3];
  const other = { ...target, workspace_id: "route-two", backend_workspace_id: "owner-two" };
  history = [run("delivered", "session-old-in-two")]; await update(other);
  expect(signal?.aborted).toBe(true);
  expect(open).not.toHaveBeenCalled(); expect(status).not.toHaveBeenCalled();
  await act(async () => { finish([run("delivered", "session-late-in-one")]); });
  expect(open).not.toHaveBeenCalled(); expect(status).not.toHaveBeenCalled();
  history = [...history, { ...run("delivering", "session-new-in-two"), run_id: "new-run" }];
  await update(other);
  expect(open.mock.calls).toEqual([["session-new-in-two"]]);
  expect(reader.mock.calls[reader.mock.calls.length - 1][1]).toEqual(other);
});

test("切换尚未加载的工作区时立即清除原工作区读取错误", async () => {
  reader.mockRejectedValueOnce(new Error("原工作区读取失败"));
  await update(); expect(error).toContain("原工作区读取失败");
  await update({ ...target, backend_workspace_id: "owner-two" }, false);
  expect(error).toBeNull();
});
