import { afterEach, describe, expect, test } from "bun:test";
import React from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import { renderToStaticMarkup } from "react-dom/server";
import type { SessionResource } from "../../../types/backend";
import {
  actionLabelForKind,
  groupSessionResources,
  resourceAttentionGroup,
  resourceKindIcon,
  resourceTreeStatus,
} from "../../../state/display/resourceDisplay";
import ResourcePanel from "./ResourcePanel";
import WarmConfirmProvider from "../../shell/WarmConfirmProvider";
import { restoreGlobalDescriptor } from "../../../tests/testGlobals";

const originalClipboard = Object.getOwnPropertyDescriptor(navigator, "clipboard");
const originalDocument = Object.getOwnPropertyDescriptor(globalThis, "document");

/** 递归收集子节点文本，避开 React 元素树的循环引用。 */
function nodeText(node: unknown): string {
  if (typeof node === "string") return node;
  if (typeof node === "number") return String(node);
  if (Array.isArray(node)) return node.map(nodeText).join("");
  if (node && typeof node === "object" && "props" in node) {
    return nodeText((node as { props: { children?: unknown } }).props.children);
  }
  return "";
}

afterEach(() => {
  if (originalClipboard) {
    Object.defineProperty(navigator, "clipboard", originalClipboard);
  } else {
    Reflect.deleteProperty(navigator, "clipboard");
  }
  restoreGlobalDescriptor("document", originalDocument);
});

function resource(
  index: number,
  overrides: Partial<SessionResource> = {},
): SessionResource {
  return {
    resource_id: `resource_${index.toString().padStart(2, "0")}_full_identifier`,
    session_id: "ses_resource_tree",
    kind: "browser",
    name: `浏览器 / 页面 ${index}`,
    status: "running",
    created_at: `2026-07-26T01:${index.toString().padStart(2, "0")}:00Z`,
    updated_at: `2026-07-26T02:${index.toString().padStart(2, "0")}:00Z`,
    started_at: null,
    ended_at: null,
    available_actions: ["cancel", "delete"],
    metadata: {
      title: `页面 ${index}`,
      url: `https://site-${index}.example/path`,
      resource_state: "background",
      client_count: 0,
    },
    ...overrides,
  };
}

describe("后台连接目录", () => {
  test("资源种类图标来自 resourceDisplay 唯一映射，面板分组标题与树行一致", () => {
    expect(resourceKindIcon("browser")).toBe("codicon-globe");
    expect(resourceKindIcon("terminal")).toBe("codicon-terminal");
    expect(resourceKindIcon("background_task")).toBe("codicon-server-process");

    const html = renderToStaticMarkup(
      <WarmConfirmProvider>
        <ResourcePanel
          resources={[
            resource(1),
            resource(2, {
              kind: "terminal",
              name: "终端 / 主终端",
              metadata: { cwd: "/workspace" },
            }),
          ]}
          loading={false}
          error={null}
          loadedAt={null}
          sessionId="ses_resource_tree"
          workspaceId="workspace_test"
          activePreviewPath={null}
          onRefresh={() => {}}
          onControl={async () => {}}
          onOpenTerminalPreview={() => {}}
          onOpenBrowserPreview={() => {}}
          onCloseResourcePreview={async () => {}}
          onCreateConnection={async () => {}}
        />
      </WarmConfirmProvider>,
    );

    // 分组标题按种类渲染图标，必须与 resourceKindIcon 完全一致。
    // ResourcePanel 只渲染可重连的 browser/terminal，background_task 由 resourceKindIcon 单测覆盖。
    expect(html).toContain(
      'resource-tree-kind-heading"><span class="codicon codicon-globe" aria-hidden="true"></span><span>浏览器</span>',
    );
    expect(html).toContain(
      'resource-tree-kind-heading"><span class="codicon codicon-terminal" aria-hidden="true"></span><span>终端</span>',
    );
  });

  test("按用户注意力分组，并将当前预览资源置顶", () => {
    const resources = [
      resource(1),
      resource(2, { metadata: { title: "当前页面", url: "https://active.example", client_count: 1 } }),
      resource(3, { status: "failed", metadata: { error_message: "启动失败" } }),
      resource(4, { metadata: { resource_state: "frozen" } }),
      resource(5, { status: "closed" }),
    ];
    const groups = groupSessionResources(resources, "browser://resource_02_full_identifier");

    expect(groups.map((group) => group.key)).toEqual([
      "active",
      "attention",
      "available",
      "sleeping",
      "history",
    ]);
    expect(groups[0]?.resources[0]?.resource_id).toBe("resource_02_full_identifier");
    expect(resourceAttentionGroup(resources[3]!, null)).toBe("sleeping");
    expect(resourceAttentionGroup(resources[4]!, null)).toBe("history");
    expect(resourceAttentionGroup(
      resources[4]!,
      "browser://resource_05_full_identifier",
    )).toBe("history");
    expect(resourceTreeStatus(resource(6, {
      status: "closed",
      metadata: { resource_state: "background" },
    }))).toBe("已关闭");
    const recoverable = resource(7, {
      status: "lost",
      available_actions: ["resume", "delete"],
      metadata: {
        resource_state: "discarded",
        checkpoint: { version: 1 },
      },
    });
    expect(resourceAttentionGroup(recoverable, null)).toBe("sleeping");
    expect(resourceTreeStatus(recoverable)).toBe("已冷回收");
    expect(actionLabelForKind("browser", "resume")).toBe("重新打开");
  });

  test("20 个资源默认仅渲染活动目录单行，历史与挂起资源折叠", () => {
    const resources = Array.from({ length: 20 }, (_, offset) => {
      const index = offset + 1;
      if (index > 15) {
        return resource(index, { status: "closed", metadata: { title: `历史页面 ${index}` } });
      }
      if (index > 12) {
        return resource(index, { metadata: { title: `挂起页面 ${index}`, resource_state: "frozen" } });
      }
      return resource(index);
    });
    const html = renderToStaticMarkup(
      <WarmConfirmProvider>
        <ResourcePanel
          resources={resources}
          loading={false}
          error={null}
          loadedAt="2026-07-26T03:00:00Z"
          sessionId="ses_resource_tree"
          workspaceId="workspace_test"
          activePreviewPath="browser://resource_01_full_identifier"
          onRefresh={() => {}}
          onControl={async () => {}}
          onOpenTerminalPreview={() => {}}
          onOpenBrowserPreview={() => {}}
          onCloseResourcePreview={async () => {}}
          onCreateConnection={async () => {}}
        />
      </WarmConfirmProvider>,
    );

    expect(html).toContain("连接总数 <span class=\"resource-total-count\">20</span>");
    expect(html).toContain("后台可用");
    expect(html).toContain("已挂起 / 可恢复");
    expect(html).toContain("历史记录");
    expect(html).toContain("页面 12");
    expect(html).not.toContain("挂起页面 13");
    expect(html).not.toContain("历史页面 16");
    expect(html).not.toContain("resource_01_full_identifier");
    expect(html).not.toContain("resource-card");
    expect(html).toContain("resource-tree-item is-selected");
    expect(html).toContain(">当前</span>");
    expect((html.match(/resource-tree-item/g) ?? []).length).toBe(12);
  });

  test("复制 ID 失败时给出可见原因，而不是把失败静默吞成成功提示", async () => {
    // Clipboard API 被拒绝 + 兼容复制也失败：这是非安全上下文下真实可达的组合。
    Object.defineProperty(navigator, "clipboard", {
      value: {
        writeText: async () => {
          throw new Error("权限被拒绝");
        },
      },
      configurable: true,
    });
    Object.defineProperty(globalThis, "document", {
      configurable: true,
      value: {
        createElement: () => ({
          value: "",
          style: { position: "", left: "" },
          setAttribute: () => undefined,
          focus: () => undefined,
          select: () => undefined,
          remove: () => undefined,
        }),
        body: { appendChild: () => undefined },
        execCommand: () => false,
      },
    });

    let renderer!: ReactTestRenderer;
    act(() => {
      renderer = create(
        <WarmConfirmProvider>
          <ResourcePanel
            resources={[resource(1)]}
            loading={false}
            error={null}
            loadedAt={null}
            sessionId="ses_resource_tree"
            workspaceId="workspace_test"
            activePreviewPath={null}
            onRefresh={() => {}}
            onControl={async () => {}}
            onOpenTerminalPreview={() => {}}
            onOpenBrowserPreview={() => {}}
            onCloseResourcePreview={async () => {}}
            onCreateConnection={async () => {}}
          />
        </WarmConfirmProvider>,
      );
    });

    // 展开资源行后才能点到「复制 ID」。
    act(() => {
      renderer.root.findByProps({ className: "resource-tree-chevron" }).props.onClick();
    });
    const copyButton = renderer.root.findByProps({ children: "复制 ID" });
    await act(async () => {
      copyButton.props.onClick();
      await new Promise((resolve) => setTimeout(resolve, 0));
    });

    const notice = renderer.root.findAll(
      (node) => typeof node.props.className === "string"
        && node.props.className.split(" ").includes("resource-notice"),
    );
    const text = notice.map((node) => nodeText(node.props.children)).join(" ");
    expect(text).toContain("复制失败");
    expect(text).not.toContain("已复制 UUID");
    renderer.unmount();
  });
});
