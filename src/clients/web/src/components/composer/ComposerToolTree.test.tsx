import { describe, expect, test } from "bun:test";
import React from "react";
import { act, create } from "react-test-renderer";
import type { ToolCatalogItem } from "../../types/toolTesting";
import ComposerToolTree, { type ToolGroup } from "./ComposerToolTree";

function tool(
  toolId: string,
  executionEnabled: boolean,
  modelVisible: boolean,
): ToolCatalogItem {
  return {
    tool_id: toolId,
    name: toolId,
    origin: "builtin",
    description: `${toolId} description`,
    parameters: {},
    category: "general",
    group_id: "default",
    group_name: "默认工具",
    kind: "default",
    execution_enabled: executionEnabled,
    model_visible: modelVisible,
    test_supported: true,
  };
}

function renderTree(
  group: ToolGroup,
  callbacks: {
    onToggleGroupCapability: (group: ToolGroup, capability: "execution" | "model") => void;
    onToggleToolExecution: (toolId: string) => void;
    onToggleToolModelVisibility: (toolId: string) => void;
  },
) {
  return create(
    <ComposerToolTree
      groups={[group]}
      loading={false}
      savingToolIds={new Set()}
      runs={new Map()}
      testingTools={new Set()}
      collapsedGroups={new Set()}
      onToggleCollapsed={() => {}}
      onToggleGroupCapability={callbacks.onToggleGroupCapability}
      onToggleToolExecution={callbacks.onToggleToolExecution}
      onToggleToolModelVisibility={callbacks.onToggleToolModelVisibility}
      onRunTest={() => {}}
    />,
  );
}

describe("ComposerToolTree 工具能力按钮", () => {
  test("使用工具和眼睛两个图标按钮，不再渲染复选框", () => {
    const renderer = renderTree(
      {
        id: "default",
        name: "默认工具",
        kind: "default",
        items: [tool("read_file", true, true)],
      },
      {
        onToggleGroupCapability: () => {},
        onToggleToolExecution: () => {},
        onToggleToolModelVisibility: () => {},
      },
    );

    expect(renderer.root.findAllByType("input")).toHaveLength(0);
    const buttons = renderer.root.findAllByType("button");
    expect(buttons.map((button) => button.props["aria-label"])).toEqual([
      "折叠 默认工具",
      "默认工具：关闭工具能力",
      "默认工具：隐藏工具说明",
      "read_file：关闭工具能力",
      "read_file：隐藏工具说明",
      undefined,
    ]);
    expect(buttons[5].props.title).toBe("测试 read_file");
    expect(buttons[1].findByType("span").props.className).toContain("codicon-tools");
    expect(buttons[2].findByType("span").props.className).toContain("codicon-eye");
    renderer.unmount();
  });

  test("执行能力关闭时模型可见按钮被禁用，并支持点击回调", () => {
    const calls: string[] = [];
    const group: ToolGroup = {
      id: "extension:test",
      name: "扩展工具",
      kind: "extension",
      items: [tool("custom_tool", false, false)],
    };
    const renderer = renderTree(group, {
      onToggleGroupCapability: (_group, capability) => calls.push(`group:${capability}`),
      onToggleToolExecution: (toolId) => calls.push(`execution:${toolId}`),
      onToggleToolModelVisibility: (toolId) => calls.push(`model:${toolId}`),
    });

    const buttons = renderer.root.findAllByType("button");
    expect(buttons[2].props.disabled).toBe(true);
    act(() => buttons[1].props.onClick());
    act(() => buttons[3].props.onClick());
    expect(calls).toEqual(["group:execution", "execution:custom_tool"]);
    renderer.unmount();
  });

  test("组级混合状态通过 aria-pressed=mixed 暴露给辅助技术", () => {
    const renderer = renderTree(
      {
        id: "default",
        name: "默认工具",
        kind: "default",
        items: [tool("read_file", true, true), tool("write_file", false, false)],
      },
      {
        onToggleGroupCapability: () => {},
        onToggleToolExecution: () => {},
        onToggleToolModelVisibility: () => {},
      },
    );

    const buttons = renderer.root.findAllByType("button");
    expect(buttons[1].props["aria-pressed"]).toBe("mixed");
    renderer.unmount();
  });

  test("执行关闭项仍参与模型可见性聚合，避免组状态虚假显示为开启", () => {
    const renderer = renderTree(
      {
        id: "extension:test",
        name: "扩展工具",
        kind: "extension",
        items: [tool("visible", true, true), tool("disabled", false, false)],
      },
      {
        onToggleGroupCapability: () => {},
        onToggleToolExecution: () => {},
        onToggleToolModelVisibility: () => {},
      },
    );

    const buttons = renderer.root.findAllByType("button");
    expect(buttons[2].props["aria-pressed"]).toBe("mixed");
    expect(buttons[2].props.disabled).toBe(false);
    renderer.unmount();
  });
});
