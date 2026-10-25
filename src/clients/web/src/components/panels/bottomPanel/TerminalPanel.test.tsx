import { afterEach, describe, expect, jest, test } from "bun:test";
import React from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import type { WorkspaceResourceDTO } from "../../../api/workspaceResources";
import TerminalPanel from "./TerminalPanel";
import { restoreGlobalDescriptor } from "../../../tests/testGlobals";

const originalWindowDescriptor = Object.getOwnPropertyDescriptor(globalThis, "window");

afterEach(() => {
  jest.useRealTimers();
  restoreGlobalDescriptor("window", originalWindowDescriptor);
});

/** buildGatewayAttachUrl 依赖 window.location.origin；测试环境没有 DOM。 */
function installWindow(): void {
  Object.defineProperty(globalThis, "window", {
    configurable: true,
    value: {
      location: { origin: "http://127.0.0.1:8011", port: "8011" },
      setTimeout: globalThis.setTimeout.bind(globalThis),
      clearTimeout: globalThis.clearTimeout.bind(globalThis),
    },
  });
}

function entry(workspaceId: string, resourceId: string): WorkspaceResourceDTO {
  return {
    resource_id: resourceId, workspace_id: workspaceId, kind: "terminal",
    retention_scope: "workspace", owner_revision: 1, source_thread: { session_id: "ses_owner", thread_id: "thr_owner" },
    thread_associations: [], status: "running", reconciliation_status: "verified", available_actions: [],
    metadata: { title: "主终端" }, created_at: "2026-07-26T01:00:00Z", updated_at: "2026-07-26T02:00:00Z",
  };
}

function panelProps(entries: WorkspaceResourceDTO[], threadTargetUnavailable = false) {
  return {
    controller: {
      items: entries, loading: false, mutating: false, error: null, refresh: async () => {},
      control: async () => entries[0]!, create: async () => entries[0]!,
    },
    currentThread: null,
    threadTargetUnavailable,
    workspaceId: entries[0]?.workspace_id ?? "workspace_test",
    workspaceName: "测试工作区",
    selectedTerminalId: null,
    height: 240,
    onSelectTerminal: () => {},
    onSwitchToOutput: () => {},
    onSwitchToPorts: () => {},
    onSwitchToAutomation: () => {},
    onClose: () => {},
  };
}

function renderPanel(entries: WorkspaceResourceDTO[], threadTargetUnavailable = false): ReactTestRenderer {
  let renderer!: ReactTestRenderer;
  act(() => {
    renderer = create(<TerminalPanel {...panelProps(entries, threadTargetUnavailable)} />);
  });
  return renderer;
}

function findFrameFallback(renderer: ReactTestRenderer) {
  return renderer.root.findAll(
    (node) => typeof node.props.className === "string"
      && node.props.className.split(" ").includes("terminal-panel-frame-fallback"),
  )[0];
}

/** 递归收集子节点文本，避开 React 元素树的循环引用。 */
function textOf(node: unknown): string {
  if (typeof node === "string") return node;
  if (typeof node === "number") return String(node);
  if (Array.isArray(node)) return node.map(textOf).join("");
  if (node && typeof node === "object" && "props" in node) {
    return textOf((node as { props: { children?: unknown } }).props.children);
  }
  return "";
}

describe("TerminalPanel 终端连接兜底", () => {
  test("终端 iframe 长时间未就绪时给出超时说明与重试入口", () => {
    installWindow();
    jest.useFakeTimers();
    const renderer = renderPanel([entry("workspace_test", "terminal_ready")]);

    // 刚挂载时只显示 iframe，不应立刻报超时。
    expect(findFrameFallback(renderer)).toBeUndefined();

    act(() => {
      jest.advanceTimersByTime(15_000);
    });

    const fallback = findFrameFallback(renderer);
    expect(fallback).toBeDefined();
    expect(textOf(fallback!.props.children)).toContain("15 秒仍未就绪");
    renderer.unmount();
  });

  test("iframe 触发 onLoad 后不再显示超时兜底", () => {
    installWindow();
    jest.useFakeTimers();
    const renderer = renderPanel([entry("workspace_test", "terminal_ready")]);
    const frame = renderer.root.findAll(
      (node) => typeof node.props.className === "string"
        && node.props.className.split(" ").includes("terminal-panel-frame"),
    )[0]!;

    act(() => frame.props.onLoad());
    act(() => {
      jest.advanceTimersByTime(60_000);
    });

    expect(findFrameFallback(renderer)).toBeUndefined();
    renderer.unmount();
  });

  test("缺少 workspace_id 时明确报错而不是静默空白", () => {
    installWindow();
    const renderer = renderPanel([entry("", "terminal_missing_workspace")]);

    const fallback = findFrameFallback(renderer);
    expect(fallback).toBeDefined();
    const text = textOf(fallback!.props.children);
    expect(text).toContain("终端暂不可连接");
    expect(text).toContain("workspace_id");
    renderer.unmount();
  });

  test("资源错误和操作错误显示在固定状态行，不进入可换行工具栏", () => {
    installWindow();
    const props = panelProps([{
      ...entry("workspace_test", "terminal_error"),
      error: "终端运行状态尚待核实",
    }]);
    const renderer = create(
      <TerminalPanel
        {...props}
        controller={{ ...props.controller, error: "刷新终端失败" }}
      />,
    );

    const toolbar = renderer.root.findByProps({ className: "terminal-panel-resource-actions" });
    const errorSlot = renderer.root.findByProps({ className: "terminal-panel-error-slot" });
    const alert = errorSlot.findByProps({ role: "alert" });
    expect(toolbar.findAll((node) => node.props.role === "alert")).toHaveLength(0);
    expect(alert.props.title).toBe("终端运行状态尚待核实 · 刷新终端失败");
    expect(textOf(alert.props.children)).toBe("终端运行状态尚待核实 · 刷新终端失败");
    expect(renderer.root.findAllByProps({ className: "terminal-panel-frame-fallback" })).toHaveLength(0);
    renderer.unmount();
  });

  test("临时孤儿不发起无授权 iframe，显示重新关联与保留入口", () => {
    installWindow();
    const orphan = { ...entry("workspace_test", "term_orphan"), retention_scope: "thread", available_actions: ["promote", "attach"], thread_associations: [] };
    const renderer = renderPanel([orphan]);
    expect(renderer.root.findAllByType("iframe")).toHaveLength(0);
    expect(textOf(findFrameFallback(renderer)!.props.children)).toContain("没有线程关联");
    const promote = renderer.root.findAllByType("button").find((button) => textOf(button.props.children) === "保留到工作区");
    expect(promote?.props.disabled).toBe(false);
    renderer.unmount();
  });

  test("child 目标尚未解析时禁用新建与资源管理操作", () => {
    installWindow();
    const renderer = renderPanel([entry("workspace_test", "term_pending_target")], true);

    const createButton = renderer.root.findAllByType("button").find(
      (button) => textOf(button.props.children) === "新建终端",
    );
    expect(createButton?.props.disabled).toBe(true);
    expect(renderer.root.findAllByType("iframe")).toHaveLength(0);
    renderer.unmount();
  });
});
