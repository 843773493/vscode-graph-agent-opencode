import { afterEach, describe, expect, test } from "bun:test";
import React from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import type { SessionResource } from "../../../types/backend";
import type { GatewayExtensionResourceEntry } from "../../../hooks/gatewayExtensions/useGatewayExtensionResources";
import GatewayExtensionResourcePanel from "./GatewayExtensionResourcePanel";
import WarmConfirmProvider from "../../shell/WarmConfirmProvider";

const originalClipboard = Object.getOwnPropertyDescriptor(navigator, "clipboard");
const originalDocument = Object.getOwnPropertyDescriptor(globalThis, "document");

afterEach(() => {
  if (originalClipboard) {
    Object.defineProperty(navigator, "clipboard", originalClipboard);
  } else {
    Reflect.deleteProperty(navigator, "clipboard");
  }
  if (originalDocument) {
    Object.defineProperty(globalThis, "document", originalDocument);
  } else {
    Reflect.deleteProperty(globalThis, "document");
  }
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
  const resource: SessionResource = {
    resource_id: "terminal_abcdef0123456789",
    session_id: "ses_gateway_extension",
    kind: "terminal",
    name: "终端 / 主终端",
    status: "running",
    created_at: "2026-07-26T01:00:00Z",
    updated_at: "2026-07-26T02:00:00Z",
    started_at: null,
    ended_at: null,
    available_actions: ["delete"],
    metadata: { cwd: "/workspace" },
  };
  return {
    key: "workspace_test:ses_gateway_extension",
    connection_kind: "local",
    gateway_name: "本地 Gateway",
    workspace_id: "workspace_test",
    workspace_name: "测试工作区",
    session_id: "ses_gateway_extension",
    session_title: "测试会话",
    resource: resource as GatewayExtensionResourceEntry["resource"],
  };
}

function panelProps() {
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
  };
}

function renderPanel(): ReactTestRenderer {
  let renderer!: ReactTestRenderer;
  act(() => {
    renderer = create(
      <WarmConfirmProvider>
        <GatewayExtensionResourcePanel {...panelProps()} />
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
