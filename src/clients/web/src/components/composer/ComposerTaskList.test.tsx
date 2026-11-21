import { expect, test } from "bun:test";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import type { PersistentTaskDTO } from "../../api/persistentTasks";
import ComposerTaskList, { type ComposerTasks } from "./ComposerTaskList";
import { ComposerDiagnosticsContext } from "./ComposerDiagnosticsContext";

const task: PersistentTaskDTO = {
  task_id: "scheduled", workspace_id: "workspace", revision: 7, definition_revision: 2,
  tokens_used: 0, time_used_seconds: 0, sync_version: 7,
  application_type: "automation", application_version: "1", configuration: {
    name: "检查服务是否上线", trigger: { type_id: "builtin.interval", version: "1", interval_seconds: 60, timezone: "UTC" },
    condition: { type_id: "builtin.script", version: "1", script: { command: ["node", "check.mjs"], timeout_seconds: 10, output_limit_bytes: 4096, memory_limit_mb: 128 } },
    context: { type_id: "builtin.literal", version: "1", content: "服务已上线" },
    action: { type_id: "builtin.runtime_source", version: "1", target_uri: "boxteam://workspace/workspace/resources/session/session/threads/thread", delivery_mode: "context_only", delivery_policy: "after_turn" },
    policies: { completion: "continuous", misfire: "skip", match_mode: "each_match" },
  }, lifecycle: "active", phase: "waiting_trigger", recovering: false, created_at: undefined, updated_at: undefined,
};
const goal: PersistentTaskDTO = { ...task, task_id: "goal", application_type: "goal", configuration: undefined };
const base: ComposerTasks = { items: [goal, task], loaded: true, error: null, onAction: async () => {}, onManage() {} };
const row = (renderer: ReactTestRenderer, id: string) => renderer.root.findByProps({ "data-task-id": id });
const button = (renderer: ReactTestRenderer, id: string, name: string) => row(renderer, id).findByProps({ "aria-label": name });

test("统一展示 Goal 与条件任务；行内按钮仅图标，暂停和详情固定原任务对象", async () => {
  const calls: unknown[] = [];
  let renderer!: ReactTestRenderer;
  act(() => { renderer = create(<ComposerTaskList {...base} onAction={async (...args) => { calls.push(args); }} />); });
  expect(renderer.root.findAllByProps({ role: "listitem" })).toHaveLength(2);
  expect(row(renderer, "goal").findAllByProps({ "aria-label": "暂停任务" })).toHaveLength(0);
  for (const control of renderer.root.findAllByType("button")) {
    expect(control.props.title).toBe(control.props["aria-label"]);
    expect(control.children.filter(child => typeof child === "string")).toHaveLength(0);
  }
  await act(async () => { await button(renderer, "scheduled", "暂停任务").props.onClick(); });
  await act(async () => { await button(renderer, "goal", "查看任务详情").props.onClick(); });
  expect(calls).toEqual([[task, "pause"], [goal, "details"]]);
  act(() => renderer.unmount());
});

test("更多操作保留历史、编辑、检查及终止，Goal 只进入其原详情与历史", () => {
  let renderer!: ReactTestRenderer;
  act(() => { renderer = create(<ComposerTaskList {...base} />); });
  act(() => button(renderer, "scheduled", "更多操作").props.onClick({ currentTarget: null }));
  expect(renderer.root.findAllByProps({ role: "menuitem" }).map(control => control.children.join("")))
    .toEqual(["查看运行历史", "编辑任务", "立即检查", "检查试运行", "终止任务"]);
  act(() => button(renderer, "goal", "更多操作").props.onClick({ currentTarget: null }));
  expect(renderer.root.findAllByProps({ role: "menuitem" }).map(control => control.children.join(""))).toEqual(["查看运行历史"]);
  act(() => renderer.unmount());
});

test("任务操作错误只标记原行并进入输出，不增加列表项；新快照继续替换原行", async () => {
  const messages: string[] = [];
  let renderer!: ReactTestRenderer;
  const view = (items: PersistentTaskDTO[]) => <ComposerDiagnosticsContext.Provider value={message => messages.push(message)}>
    <ComposerTaskList {...base} items={items} onAction={async () => { throw new Error("HTTP 409 revision=7"); }} />
  </ComposerDiagnosticsContext.Provider>;
  act(() => { renderer = create(view([task])); });
  await act(async () => { await button(renderer, "scheduled", "暂停任务").props.onClick(); });
  expect(renderer.root.findAllByProps({ role: "listitem" })).toHaveLength(1);
  expect(row(renderer, "scheduled").findByProps({ role: "alert" }).props.title).toBe("操作失败，详情见输出");
  expect(JSON.stringify(renderer.toJSON())).not.toContain("HTTP 409");
  expect(messages).toEqual(["task_id=scheduled: HTTP 409 revision=7"]);
  act(() => renderer.update(view([{ ...task, revision: 8, lifecycle: "paused" }])));
  expect(button(renderer, "scheduled", "恢复任务")).toBeDefined();
  act(() => renderer.unmount());
});
