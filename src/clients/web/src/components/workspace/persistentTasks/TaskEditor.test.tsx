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
    await act(async () => { renderer = create(<TaskEditor configuration={config} busy={false} onCancel={() => {}} onSave={async (value) => { saved.push(value); return true; }} />); });
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
    await act(async () => { renderer = create(<TaskEditor configuration={config} busy={false} onCancel={() => {}} onSave={async (value) => { saved.push(value); return true; }} />); });
    const select = () => renderer.root.findAllByType("select").find((node) => node.props["aria-label"] === "判断方式")!;
    await act(async () => { select().props.onChange({ target: { value: "builtin.always" } }); });
    await act(async () => { select().props.onChange({ target: { value: "builtin.rule" } }); });
    await act(async () => { await renderer.root.findByType("form").props.onSubmit({ preventDefault() {} }); });
    expect(saved).toEqual([{ ...config, condition: { type_id: "builtin.rule", version: "1", rule: { pointer: "", expected: true } } }]);
  } finally { act(() => renderer?.unmount()); }
});

test("未知组件版本明确不可编辑，不生成替代配置", async () => {
  let renderer!: ReactTestRenderer;
  try {
    await act(async () => { renderer = create(<TaskEditor configuration={{ ...config, context: { ...config.context!, version: "future" } }} busy={false} onCancel={() => {}} onSave={async () => { throw new Error("不得保存未知组件"); }} />); });
    expect(renderer.root.findAllByType("form")).toHaveLength(0);
    expect(renderer.root.findByProps({ role: "alert" }).children.join("")).toContain("不支持的组件版本");
  } finally { act(() => renderer?.unmount()); }
});
