import { expect, test } from "bun:test";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import type { PersistentTaskConfigurationDTO } from "../../../api/persistentTasks";
import TaskEditor from "./TaskEditor";

const config: PersistentTaskConfigurationDTO = {
  name: "已有规则", trigger: { type_id: "builtin.interval", version: "1", interval_seconds: 73, timezone: "Asia/Shanghai" },
  condition: { type_id: "builtin.rule", version: "1", rule: { pointer: "/trigger/payload", expected: null } },
  context: { type_id: "builtin.literal", version: "1", mode: "state", content: "必须保留的正文" },
  action: { type_id: "builtin.runtime_source", version: "1", target_uri: "boxteam://workspace/ws/resources/session/ses/threads/thr", delivery_mode: "context_only", delivery_policy: "after_tool_result" },
  policies: { completion: "continuous", misfire: "catch_up", match_mode: "rising_edge" },
};

test("编辑基础字段保留null比较值及未展开的全部高级配置", async () => {
  const saved: PersistentTaskConfigurationDTO[] = [];
  let renderer!: ReactTestRenderer;
  try {
    await act(async () => { renderer = create(<TaskEditor apiPort={8014} target={{ workspace_id: "route", backend_workspace_id: "ws", connection_kind: "local" }} configuration={config} busy={false} onCancel={() => {}} onSave={async (value) => { saved.push(value); return true; }} />); });
    const name = renderer.root.findAllByType("input").find((node) => node.props.maxLength === 200)!;
    await act(async () => { name.props.onChange({ target: { value: "只改名称" } }); });
    await act(async () => { await renderer.root.findByType("form").props.onSubmit({ preventDefault() {} }); });
    expect(saved).toEqual([{ ...config, name: "只改名称" }]);
  } finally { act(() => renderer?.unmount()); }
});

test("重新选择规则时重置比较输入，不沿用已移除判断器的值", async () => {
  let renderer!: ReactTestRenderer;
  const saved: PersistentTaskConfigurationDTO[] = [];
  try {
    await act(async () => { renderer = create(<TaskEditor apiPort={8014} target={{ workspace_id: "route", backend_workspace_id: "ws", connection_kind: "local" }} configuration={config} busy={false} onCancel={() => {}} onSave={async (value) => { saved.push(value); return true; }} />); });
    const select = () => renderer.root.findAllByType("select").find((node) => node.props["aria-label"] === "判断方式")!;
    const toggle = () => renderer.root.findByProps({ "aria-label": "启用附加条件" });
    await act(async () => { toggle().props.onChange({ target: { checked: false } }); });
    expect(renderer.root.findAllByProps({ "aria-label": "判断方式" })).toHaveLength(0);
    await act(async () => { toggle().props.onChange({ target: { checked: true } }); });
    await act(async () => { select().props.onChange({ target: { value: "builtin.rule" } }); });
    await act(async () => { await renderer.root.findByType("form").props.onSubmit({ preventDefault() {} }); });
    expect(saved).toEqual([{ ...config, condition: { type_id: "builtin.rule", version: "1", rule: { pointer: "", expected: true } } }]);
  } finally { act(() => renderer?.unmount()); }
});

test("普通定时默认隐藏判断器，增加模型条件时保留时间、工作动作与高级策略", async () => {
  const original = { ...config, condition: { type_id: "builtin.always", version: "1" } };
  const saved: PersistentTaskConfigurationDTO[] = [];
  let renderer!: ReactTestRenderer;
  try {
    await act(async () => { renderer = create(<TaskEditor apiPort={8014} target={{ workspace_id: "route", backend_workspace_id: "ws", connection_kind: "local" }} configuration={original} busy={false} onCancel={() => {}} onSave={async (value) => { saved.push(value); return false; }} />); });
    expect(renderer.root.findAllByProps({ "aria-label": "判断方式" })).toHaveLength(0);
    await act(async () => { await renderer.root.findByType("form").props.onSubmit({ preventDefault() {} }); });
    expect(saved[0]).toEqual(original);
    await act(async () => { renderer.root.findByProps({ "aria-label": "启用附加条件" }).props.onChange({ target: { checked: true } }); });
    await act(async () => { renderer.root.findByProps({ "aria-label": "判断方式" }).props.onChange({ target: { value: "builtin.model" } }); });
    await act(async () => { renderer.root.findByProps({ "aria-label": "判断 Thread URI" }).props.onChange({ target: { value: "boxteam://workspace/ws/resources/session/ses/threads/judge" } }); });
    await act(async () => { renderer.root.findByProps({ "aria-label": "判断说明" }).props.onChange({ target: { value: "判断是否上线" } }); });
    await act(async () => { await renderer.root.findByType("form").props.onSubmit({ preventDefault() {} }); });
    expect(saved[1]).toEqual({ ...original, condition: { type_id: "builtin.model", version: "1", model: { thread_uri: "boxteam://workspace/ws/resources/session/ses/threads/judge", instruction: "判断是否上线" } } });
  } finally { act(() => renderer?.unmount()); }
});

test("未知组件版本明确不可编辑，不生成替代配置", async () => {
  let renderer!: ReactTestRenderer;
  try {
    await act(async () => { renderer = create(<TaskEditor apiPort={8014} target={{ workspace_id: "route", backend_workspace_id: "ws", connection_kind: "local" }} configuration={{ ...config, context: { ...config.context!, version: "future" } }} busy={false} onCancel={() => {}} onSave={async () => { throw new Error("不得保存未知组件"); }} />); });
    expect(renderer.root.findAllByType("form")).toHaveLength(0);
    expect(renderer.root.findByProps({ role: "alert" }).children.join("")).toContain("不支持的组件版本");
  } finally { act(() => renderer?.unmount()); }
});

test("会话动作保留三种策略、回报与UI选项，并保存同一完整定义", async () => {
  const { initialGeneration } = await import("./GenerationActionEditor");
  const generation = initialGeneration("backend-owner");
  const original = { ...config, context: { ...config.context!, mode: "event" }, action: { type_id: "builtin.session_generation", version: "1", generation: { ...generation, config: { prompt: "执行任务", session_title: "固定标题" } } } };
  const saved: PersistentTaskConfigurationDTO[] = [];
  let renderer!: ReactTestRenderer;
  try {
    await act(async () => { renderer = create(<TaskEditor apiPort={8014} target={{ workspace_id: "route", backend_workspace_id: "backend-owner", connection_kind: "local" }} configuration={original} busy={false} onCancel={() => {}} onSave={async (value) => { saved.push(value); return true; }} />); });
    const select = (name: string) => renderer.root.findByProps({ "aria-label": name });
    expect(select("会话策略").findAllByType("option").map((node) => node.props.value)).toEqual(["new_per_run", "continue_existing", "fork_new_and_report_back"]);
    await act(async () => { select("会话策略").props.onChange({ target: { value: "fork_new_and_report_back" } }); });
    await act(async () => { select("目标会话 ID").props.onChange({ target: { value: "ses_target" } }); });
    await act(async () => { select("目标 Thread ID").props.onChange({ target: { value: "thr_target" } }); });
    await act(async () => { select("回报方式").props.onChange({ target: { value: "summary_and_link" } }); });
    await act(async () => { select("运行开始时").props.onChange({ target: { value: "open_generated" } }); });
    await act(async () => { await renderer.root.findByType("form").props.onSubmit({ preventDefault() {} }); });
    expect(saved).toHaveLength(1);
    expect(saved[0].action?.generation).toEqual({ ...original.action.generation, session_strategy: { mode: "fork_new_and_report_back", concurrency: "queue", report_back: "summary_and_link", target: { workspace_id: "backend-owner", session_id: "ses_target", thread_id: "thr_target" } }, ui_policy: { on_run_started: "open_generated", on_run_completed: "notify" } });
    expect(saved[0].trigger).toEqual(config.trigger);
    expect(saved[0].policies).toEqual(config.policies);
    expect(saved[0].action).not.toHaveProperty("target_uri");
    expect(saved[0].action).not.toHaveProperty("delivery_mode");
  } finally { act(() => renderer?.unmount()); }
});
