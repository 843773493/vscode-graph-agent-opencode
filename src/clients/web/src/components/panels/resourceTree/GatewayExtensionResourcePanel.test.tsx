import { afterEach, describe, expect, test } from "bun:test";
import React from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import type { GatewayExtensionResourceEntry } from "../../../hooks/gatewayExtensions/useGatewayExtensionResources";
import type { ResourceThreadRef } from "../../../api/workspaceResources";
import GatewayExtensionResourcePanel from "./GatewayExtensionResourcePanel";
import WarmConfirmProvider from "../../shell/WarmConfirmProvider";
import { restoreGlobalDescriptor } from "../../../tests/testGlobals";

const originalClipboard = Object.getOwnPropertyDescriptor(navigator, "clipboard");
const originalDocument = Object.getOwnPropertyDescriptor(globalThis, "document");

afterEach(() => {
  if (originalClipboard) {
    Object.defineProperty(navigator, "clipboard", originalClipboard);
  } else {
    Reflect.deleteProperty(navigator, "clipboard");
  }
  restoreGlobalDescriptor("document", originalDocument);
});

/** 测试环境没有 DOM；用最小假 document 驱动 utils/clipboard 的兼容复制路径。 */
function installFakeDocument(execCommandResult: boolean): { copied: number } {
  const state = { copied: 0 };
  const textarea = {
    value: "",
    style: { position: "", left: "", top: "" },
    setAttribute: () => undefined,
    focus: () => undefined,
    select: () => undefined,
    remove: () => undefined,
  };
  Object.defineProperty(globalThis, "document", {
    configurable: true,
    value: {
      createElement: () => textarea,
      body: { appendChild: () => undefined },
      execCommand: (command: string) => {
        if (command === "copy") state.copied += 1;
        return execCommandResult;
      },
    },
  });
  return state;
}

function entry(): GatewayExtensionResourceEntry {
  const resource: GatewayExtensionResourceEntry["resource"] = {
    resource_id: "terminal_abcdef0123456789",
    workspace_id: "workspace_test",
    kind: "terminal",
    status: "running",
    created_at: "2026-07-26T01:00:00Z",
    updated_at: "2026-07-26T02:00:00Z",
    retention_scope: "workspace",
    source_thread: { session_id: "ses_gateway_extension", thread_id: "thr_source" },
    thread_associations: [],
    owner_revision: 1,
    reconciliation_status: "verified",
    available_actions: ["stop", "delete"],
    metadata: { title: "主终端", cwd_relative: "." },
  };
  return {
    key: "workspace_test:ses_gateway_extension",
    connection_kind: "local",
    gateway_name: "本地 Gateway",
    workspace_id: "workspace_test",
    workspace_name: "测试工作区",
    session_id: "ses_gateway_extension",
    session_title: "测试会话",
    resource,
  };
}

const MAIN_THREAD: ResourceThreadRef = {
  session_id: "ses_gateway_extension",
  thread_id: "thr_main",
};

function panelProps(overrides: {
  entries?: GatewayExtensionResourceEntry[];
  currentOwner?: { workspaceId: string; thread: ResourceThreadRef } | null;
  onOwnerControl?: (
    entry: GatewayExtensionResourceEntry,
    action: "promote" | "attach" | "detach",
    thread?: ResourceThreadRef,
  ) => Promise<void>;
} = {}) {
  return {
    entries: [entry()],
    errors: [],
    loading: false,
    loadedAt: null,
    selectedKey: null,
    onSelect: () => {},
    onRefresh: () => {},
    onControl: async () => {},
    onOpen: () => {},
    onCreateReplacement: async () => {},
    currentOwner: null,
    onOwnerControl: async () => {},
    ...overrides,
  };
}

function renderPanel(props = panelProps()): ReactTestRenderer {
  let renderer!: ReactTestRenderer;
  act(() => {
    renderer = create(
      <WarmConfirmProvider>
        <GatewayExtensionResourcePanel {...props} />
      </WarmConfirmProvider>,
    );
  });
  return renderer;
}

function noticeText(renderer: ReactTestRenderer): string {
  const notices = renderer.root.findAll(
    (node) => typeof node.props.className === "string"
      && node.props.className.split(" ").includes("resource-notice"),
  );
  return notices
    .map((node) => `${node.props.className}| ${JSON.stringify(node.props.children)}`)
    .join(" ");
}

async function clickCopy(renderer: ReactTestRenderer): Promise<void> {
  // 「复制 ID」位于展开后的详情区，先展开资源行。
  const chevron = renderer.root.findAll(
    (node) => typeof node.props.className === "string"
      && node.props.className.split(" ").includes("resource-tree-chevron"),
  )[0]!;
  act(() => chevron.props.onClick());
  const copyButton = renderer.root.findByProps({ children: "复制 ID" });
  await act(async () => {
    copyButton.props.onClick();
    await new Promise((resolve) => setTimeout(resolve, 0));
  });
}

describe("扩展窗口资源面板复制反馈", () => {
  test("Clipboard API 不可用时走兼容复制并提示成功", async () => {
    // 模拟非安全上下文：navigator.clipboard 缺失，只能靠 document.execCommand。
    Object.defineProperty(navigator, "clipboard", {
      value: undefined,
      configurable: true,
    });
    const documentState = installFakeDocument(true);

    const renderer = renderPanel();
    await clickCopy(renderer);

    expect(documentState.copied).toBe(1);
    expect(noticeText(renderer)).toContain("已复制 UUID");
    renderer.unmount();
  });

  test("writeText 被拒绝且兼容复制也失败时给出可见失败提示", async () => {
    Object.defineProperty(navigator, "clipboard", {
      value: {
        writeText: async () => {
          throw new Error("权限被拒绝");
        },
      },
      configurable: true,
    });
    installFakeDocument(false);

    const renderer = renderPanel();
    await clickCopy(renderer);

    const text = noticeText(renderer);
    expect(text).toContain("复制失败");
    expect(text).toContain("权限被拒绝");
    // 失败提示必须以 error 类呈现，而不是伪装成成功状态。
    expect(text).toContain("is-error");
    renderer.unmount();
  });
});

describe("扩展窗口资源关联 owner", () => {
  test("关联时传递完整的当前主线程 pair", async () => {
    const calls: Array<[GatewayExtensionResourceEntry, string, ResourceThreadRef | undefined]> = [];
    const props = panelProps({
      currentOwner: { workspaceId: "workspace_test", thread: MAIN_THREAD },
      onOwnerControl: async (resourceEntry, action, thread) => {
        calls.push([resourceEntry, action, thread]);
      },
    });
    const renderer = renderPanel(props);
    const chevron = renderer.root.findAll(
      (node) => typeof node.props.className === "string"
        && node.props.className.split(" ").includes("resource-tree-chevron"),
    )[0]!;
    act(() => chevron.props.onClick());
    const attach = renderer.root.findByProps({ children: "关联当前会话" });
    await act(async () => {
      attach.props.onClick();
      await new Promise((resolve) => setTimeout(resolve, 0));
    });

    expect(calls).toHaveLength(1);
    expect(calls[0]?.[1]).toBe("attach");
    expect(calls[0]?.[2]).toEqual(MAIN_THREAD);
    renderer.unmount();
  });

  test("相同 thread_id 的其它 session 关联不吞掉完整 pair 的关联入口", () => {
    const resourceEntry = entry();
    resourceEntry.resource.thread_associations = [{
      session_id: "ses_other",
      thread_id: MAIN_THREAD.thread_id,
    }];
    const renderer = renderPanel(panelProps({
      entries: [resourceEntry],
      currentOwner: { workspaceId: "workspace_test", thread: MAIN_THREAD },
    }));
    const chevron = renderer.root.findAll(
      (node) => typeof node.props.className === "string"
        && node.props.className.split(" ").includes("resource-tree-chevron"),
    )[0]!;
    act(() => chevron.props.onClick());
    expect(renderer.root.findAllByProps({ children: "关联当前会话" })).toHaveLength(1);
    renderer.unmount();
  });

  test("同一 session 的 child 已关联时，main thread 精确 pair 仍可关联", () => {
    const resourceEntry = entry();
    resourceEntry.resource.retention_scope = "thread";
    resourceEntry.resource.source_thread = MAIN_THREAD;
    resourceEntry.resource.thread_associations = [{
      session_id: MAIN_THREAD.session_id,
      thread_id: "thr_existing_child",
    }];
    const renderer = renderPanel(panelProps({
      entries: [resourceEntry],
      currentOwner: { workspaceId: "workspace_test", thread: MAIN_THREAD },
    }));
    const chevron = renderer.root.findAll(
      (node) => typeof node.props.className === "string"
        && node.props.className.split(" ").includes("resource-tree-chevron"),
    )[0]!;
    act(() => chevron.props.onClick());
    expect(renderer.root.findAllByProps({ children: "关联当前会话" })).toHaveLength(1);
    renderer.unmount();
  });
});
