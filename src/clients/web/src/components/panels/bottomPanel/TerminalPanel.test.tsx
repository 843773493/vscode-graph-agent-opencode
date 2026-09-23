import { afterEach, describe, expect, jest, test } from "bun:test";
import React from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import type { SessionResource } from "../../../types/backend";
import type { GatewayExtensionResourceEntry } from "../../../hooks/gatewayExtensions/useGatewayExtensionResources";
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

function entry(workspaceId: string, resourceId: string): GatewayExtensionResourceEntry {
  const resource: SessionResource = {
    resource_id: resourceId,
    session_id: "ses_bottom_panel",
    kind: "terminal",
    name: "终端 / 主终端",
    status: "running",
    created_at: "2026-07-26T01:00:00Z",
    updated_at: "2026-07-26T02:00:00Z",
    started_at: null,
    ended_at: null,
    available_actions: [],
    metadata: {},
  };
  return {
    key: `${workspaceId}:${resourceId}`,
    connection_kind: "local",
    gateway_name: "本地 Gateway",
    workspace_id: workspaceId,
    workspace_name: "测试工作区",
    session_id: "ses_bottom_panel",
    session_title: "测试会话",
    resource: resource as GatewayExtensionResourceEntry["resource"],
  };
}

function panelProps(entries: GatewayExtensionResourceEntry[]) {
  return {
    entries,
    workspaceId: "workspace_test",
    workspaceName: "测试工作区",
    selectedTerminalId: null,
    height: 240,
    loading: false,
    onSelectTerminal: () => {},
    onRefresh: () => {},
    onSwitchToOutput: () => {},
    onSwitchToPorts: () => {},
    onSwitchToAutomation: () => {},
    onClose: () => {},
  };
}

function renderPanel(entries: GatewayExtensionResourceEntry[]): ReactTestRenderer {
  let renderer!: ReactTestRenderer;
  act(() => {
    renderer = create(<TerminalPanel {...panelProps(entries)} />);
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
    expect(text).toContain("无法构造终端连接地址");
    expect(text).toContain("workspace_id");
    renderer.unmount();
  });
});
