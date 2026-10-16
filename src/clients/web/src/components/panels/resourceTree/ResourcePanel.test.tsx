import { afterEach, describe, expect, mock, test } from "bun:test";
import React, { type ComponentProps } from "react";
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
import { restoreGlobalDescriptor } from "../../../tests/testGlobals";

type WarmActionDialogProps = {
  open: boolean;
  title: string;
  description?: string;
  confirmText: string;
  onClose: () => void;
  onConfirm: (value: string) => Promise<void>;
};

function InlineWarmActionDialog({
  open,
  title,
  description,
  confirmText,
  onClose,
  onConfirm,
}: WarmActionDialogProps) {
  if (!open) return null;
  return (
    <div role="dialog" aria-label={title}>
      {description ? <p>{description}</p> : null}
      <button type="button" onClick={() => onConfirm("").then(onClose)}>{confirmText}</button>
      <button type="button" onClick={onClose}>取消</button>
    </div>
  );
}

// 该测试文件由固定入口单独启动。Bun 的 mock.module 无法撤销，因此只替身确认弹窗的 DOM
// 外壳；真实 WarmConfirmProvider 的队列、确认 Promise 与 ResourcePanel 操作链仍会执行。
mock.module("../../overlays/WarmActionDialog", () => ({
  default: InlineWarmActionDialog,
}));

const { default: ResourcePanel } = await import("./ResourcePanel");
const { default: WarmConfirmProvider } = await import("../../shell/WarmConfirmProvider");
type ResourcePanelProps = ComponentProps<typeof ResourcePanel>;

const originalClipboard = Object.getOwnPropertyDescriptor(navigator, "clipboard");
const originalDocument = Object.getOwnPropertyDescriptor(globalThis, "document");
const MAIN_THREAD = { session_id: "ses_resource_tree", thread_id: "thr_main" };

const DEFAULT_PANEL_PROPS: ResourcePanelProps = {
  resources: [],
  loading: false,
  error: null,
  loadedAt: null,
  sessionId: "ses_resource_tree",
  workspaceId: "workspace_test",
  mainThread: MAIN_THREAD,
  activePreviewPath: null,
  onRefresh: () => {},
  onControl: async () => {},
  onOpenTerminalPreview: () => {},
  onOpenBrowserPreview: () => {},
  onCloseResourcePreview: async () => {},
  onCreateConnection: async () => {},
};

function renderPanel(props: Partial<ResourcePanelProps> = {}): ReactTestRenderer {
  let renderer!: ReactTestRenderer;
  act(() => {
    renderer = create(
      <WarmConfirmProvider>
        <ResourcePanel {...DEFAULT_PANEL_PROPS} {...props} />
      </WarmConfirmProvider>,
    );
  });
  return renderer;
}

function updatePanel(renderer: ReactTestRenderer, props: Partial<ResourcePanelProps>): void {
  act(() => {
    renderer.update(
      <WarmConfirmProvider>
        <ResourcePanel {...DEFAULT_PANEL_PROPS} {...props} />
      </WarmConfirmProvider>,
    );
  });
}

function deferred<T>() {
  let resolve!: (value: T | PromiseLike<T>) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

function rowForKind(renderer: ReactTestRenderer, kind: SessionResource["kind"]) {
  return renderer.root.findAll(
    (node) => node.type === "article" && node.props["data-resource-kind"] === kind,
  )[0]!;
}

function rowForPage(renderer: ReactTestRenderer, page: number) {
  return renderer.root.findAll(
    (node) => node.type === "article"
      && node.props["data-resource-kind"] === "browser"
      && nodeText(node.props.children).includes(`页面 ${page}`),
  )[0]!;
}

function noticeText(renderer: ReactTestRenderer): string {
  const notice = renderer.root.findAllByProps({ className: "resource-notice" })[0];
  return notice ? nodeText(notice.props.children) : "";
}

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
      retention_scope: "thread",
      owner_revision: 1,
      reconciliation_status: "verified",
      thread_associations: [{ session_id: "ses_resource_tree", thread_id: "thr_resource_tree" }],
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
              metadata: { cwd_relative: ".", retention_scope: "thread", reconciliation_status: "verified", thread_associations: [{ session_id: "ses_resource_tree", thread_id: "thr_actual_child" }] },
            }),
          ]}
          loading={false}
          error={null}
          loadedAt={null}
          sessionId="ses_resource_tree"
          workspaceId="workspace_test"
          mainThread={MAIN_THREAD}
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
        checkpoint_available: true,
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
          mainThread={MAIN_THREAD}
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
            mainThread={MAIN_THREAD}
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

  test("创建连接只用已解析的主线程；目标缺失或属于其它会话时禁用入口", async () => {
    const onCreateConnection = async () => {};
    const unavailable = renderToStaticMarkup(
      <WarmConfirmProvider>
        <ResourcePanel
          resources={[]}
          loading={false}
          error={null}
          loadedAt={null}
          sessionId="ses_resource_tree"
          workspaceId="workspace_test"
          mainThread={null}
          activePreviewPath={null}
          onRefresh={() => {}}
          onControl={async () => {}}
          onOpenTerminalPreview={() => {}}
          onOpenBrowserPreview={() => {}}
          onCloseResourcePreview={async () => {}}
          onCreateConnection={onCreateConnection}
        />
      </WarmConfirmProvider>,
    );
    expect(unavailable).toContain('class="resource-refresh-button resource-create-button" disabled="" title="等待解析当前会话主线程"');

    const foreignOwner = renderToStaticMarkup(
      <WarmConfirmProvider>
        <ResourcePanel
          resources={[]}
          loading={false}
          error={null}
          loadedAt={null}
          sessionId="ses_resource_tree"
          workspaceId="workspace_test"
          mainThread={{ session_id: "ses_other", thread_id: "thr_other" }}
          activePreviewPath={null}
          onRefresh={() => {}}
          onControl={async () => {}}
          onOpenTerminalPreview={() => {}}
          onOpenBrowserPreview={() => {}}
          onCloseResourcePreview={async () => {}}
          onCreateConnection={onCreateConnection}
        />
      </WarmConfirmProvider>,
    );
    expect(foreignOwner).toContain('class="resource-refresh-button resource-create-button" disabled="" title="等待解析当前会话主线程"');
  });

  test("创建回调收到面板明确的主线程 pair", async () => {
    const calls: unknown[][] = [];
    let renderer!: ReactTestRenderer;
    act(() => {
      renderer = create(
        <WarmConfirmProvider>
          <ResourcePanel
            resources={[]}
            loading={false}
            error={null}
            loadedAt={null}
            sessionId="ses_resource_tree"
            workspaceId="workspace_test"
            mainThread={MAIN_THREAD}
            activePreviewPath={null}
            onRefresh={() => {}}
            onControl={async () => {}}
            onOpenTerminalPreview={() => {}}
            onOpenBrowserPreview={() => {}}
            onCloseResourcePreview={async () => {}}
            onCreateConnection={async (...args) => { calls.push(args); }}
          />
        </WarmConfirmProvider>,
      );
    });

    act(() => renderer.root.findByProps({ className: "resource-refresh-button resource-create-button" }).props.onClick());
    await act(async () => {
      const terminal = renderer.root.findAllByProps({ role: "menuitem" })
        .find((item) => nodeText(item.props.children).includes("新建终端"));
      terminal!.props.onClick();
    });

    expect(calls).toEqual([["terminal", MAIN_THREAD]]);
    renderer.unmount();
  });

  test("创建回调直接显示 owner 切换结果，不误报资源刷新失败", async () => {
    const staleError = new Error(
      "终端 term_created_from_resource_panel 已创建，但当前会话已切换；子线程仍保持打开，请刷新资源后再打开终端。",
    );
    let renderer!: ReactTestRenderer;
    act(() => {
      renderer = create(
        <WarmConfirmProvider>
          <ResourcePanel
            resources={[]}
            loading={false}
            error={null}
            loadedAt={null}
            sessionId="ses_resource_tree"
            workspaceId="workspace_test"
            mainThread={MAIN_THREAD}
            activePreviewPath={null}
            onRefresh={() => {}}
            onControl={async () => {}}
            onOpenTerminalPreview={() => {}}
            onOpenBrowserPreview={() => {}}
            onCloseResourcePreview={async () => {}}
            onCreateConnection={async () => { throw staleError; }}
          />
        </WarmConfirmProvider>,
      );
    });

    act(() => renderer.root.findByProps({ className: "resource-refresh-button resource-create-button" }).props.onClick());
    const terminalAction = renderer.root.findAllByProps({ role: "menuitem" })
      .find((item) => nodeText(item.props.children).includes("新建终端"));
    expect(terminalAction).toBeDefined();
    await act(async () => {
      terminalAction!.props.onClick();
      await new Promise((resolve) => setTimeout(resolve, 0));
    });

    const notice = nodeText(renderer.root.findByProps({ className: "resource-notice" }).props.children);
    expect(notice).toBe(staleError.message);
    expect(notice).toContain("当前会话已切换");
    expect(notice).not.toContain("资源列表刷新失败");
    renderer.unmount();
  });

  test("同一 owner 的较早控制请求完成时不清除较新请求的 busy，也不覆盖其 notice", async () => {
    const first = deferred<void>();
    const second = deferred<void>();
    const renderer = renderPanel({
      resources: [resource(1), resource(2)],
      onControl: (_kind, resourceId) => resourceId === resource(1).resource_id
        ? first.promise
        : second.promise,
    });
    const rows = [rowForPage(renderer, 1), rowForPage(renderer, 2)];
    for (const row of rows) {
      act(() => row.findByProps({ className: "resource-tree-chevron" }).props.onClick());
    }
    const firstButton = rows[0]!.findByProps({ className: "resource-action-cancel" });
    const secondButton = rows[1]!.findByProps({ className: "resource-action-cancel" });
    let firstRequest!: Promise<void>;
    let secondRequest!: Promise<void>;
    act(() => { firstRequest = firstButton.props.onClick(); });
    act(() => { secondRequest = secondButton.props.onClick(); });
    expect(rows[0]!.props["aria-busy"]).toBe(false);
    expect(rows[1]!.props["aria-busy"]).toBe(true);

    await act(async () => {
      first.resolve();
      await firstRequest;
    });
    expect(rows[1]!.props["aria-busy"]).toBe(true);
    expect(noticeText(renderer)).toBe("");

    await act(async () => {
      second.resolve();
      await secondRequest;
    });
    expect(rows[1]!.props["aria-busy"]).toBe(false);
    expect(noticeText(renderer)).toContain("resource_02_full_identifier");
    renderer.unmount();
  });

  test("A→B→A 后旧 owner 的异步错误不能写入新 A 实例", async () => {
    const oldRequest = deferred<void>();
    const originalResource = resource(1);
    const renderer = renderPanel({
      resources: [originalResource],
      onControl: () => oldRequest.promise,
    });
    const originalRow = rowForKind(renderer, "browser");
    act(() => originalRow.findByProps({ className: "resource-tree-chevron" }).props.onClick());
    let request!: Promise<void>;
    act(() => {
      request = originalRow.findByProps({ className: "resource-action-cancel" }).props.onClick();
    });

    updatePanel(renderer, { sessionId: "ses_b", workspaceId: "workspace_b", resources: [] });
    updatePanel(renderer, { sessionId: "ses_resource_tree", workspaceId: "workspace_test", resources: [originalResource], onControl: () => oldRequest.promise });
    const newArow = rowForKind(renderer, "browser");
    expect(newArow.props["aria-busy"]).toBe(false);

    await act(async () => {
      oldRequest.reject(new Error("旧 A 请求失败"));
      await request;
    });
    expect(newArow.props["aria-busy"]).toBe(false);
    expect(noticeText(renderer)).toBe("");
    renderer.unmount();
  });

  test("owner 切换会重置本地 preview 展示态", () => {
    const activeBrowser = resource(1);
    const previewCalls: string[] = [];
    const renderer = renderPanel({
      resources: [activeBrowser],
      onOpenBrowserPreview: (id) => previewCalls.push(id),
    });
    act(() => rowForKind(renderer, "browser").findByProps({ className: "resource-tree-main" }).props.onClick());
    expect(previewCalls).toEqual([activeBrowser.resource_id]);

    updatePanel(renderer, { sessionId: "ses_b", workspaceId: "workspace_b", resources: [] });
    updatePanel(renderer, {
      sessionId: "ses_resource_tree",
      workspaceId: "workspace_test",
      resources: [resource(1, { status: "closed" })],
    });
    expect(noticeText(renderer)).toBe("");
    renderer.unmount();
  });

  test("删除确认等待期间切换 owner 后，确认不会再执行控制或关闭预览", async () => {
    const controls: string[] = [];
    const closePreview: string[] = [];
    const target = resource(1);
    const renderer = renderPanel({
      resources: [target],
      onControl: async (_kind, id) => { controls.push(id); },
      onCloseResourcePreview: async (_kind, id) => { closePreview.push(id); },
    });
    const row = rowForKind(renderer, "browser");
    act(() => row.findByProps({ className: "resource-tree-chevron" }).props.onClick());
    let deleteRequest!: Promise<void>;
    act(() => {
      deleteRequest = row.findByProps({ className: "resource-action-delete" }).props.onClick();
    });
    expect(renderer.root.findByProps({ role: "dialog" })).toBeDefined();

    updatePanel(renderer, { sessionId: "ses_b", workspaceId: "workspace_b", resources: [] });
    const dialog = renderer.root.findByProps({ role: "dialog" });
    await act(async () => {
      const confirmButton = dialog.findAllByType("button")
        .find((button) => nodeText(button.props.children) === "删除")!;
      await confirmButton.props.onClick();
      await deleteRequest;
    });
    expect(controls).toEqual([]);
    expect(closePreview).toEqual([]);
    renderer.unmount();
  });

  test("控制已启动后切换 owner，迟到的删除成功不再关闭预览或写入 notice", async () => {
    const control = deferred<void>();
    const controls: string[] = [];
    const closePreview: string[] = [];
    const renderer = renderPanel({
      resources: [resource(1)],
      onControl: async (_kind, id) => {
        controls.push(id);
        await control.promise;
      },
      onCloseResourcePreview: async (_kind, id) => { closePreview.push(id); },
    });
    const row = rowForKind(renderer, "browser");
    act(() => row.findByProps({ className: "resource-tree-chevron" }).props.onClick());
    let deleteRequest!: Promise<void>;
    act(() => {
      deleteRequest = row.findByProps({ className: "resource-action-delete" }).props.onClick();
    });
    const confirmButton = renderer.root.findByProps({ role: "dialog" }).findAllByType("button")
      .find((button) => nodeText(button.props.children) === "删除")!;
    await act(async () => {
      confirmButton.props.onClick();
      await Promise.resolve();
    });
    expect(controls).toEqual([resource(1).resource_id]);

    updatePanel(renderer, { sessionId: "ses_b", workspaceId: "workspace_b", resources: [] });
    await act(async () => {
      control.resolve();
      await deleteRequest;
    });
    expect(closePreview).toEqual([]);
    expect(noticeText(renderer)).toBe("");
    renderer.unmount();
  });

  test("A→B→A 后较早的创建请求不能污染新 owner 的创建状态或 notice", async () => {
    const oldCreate = deferred<void>();
    const renderer = renderPanel({ onCreateConnection: () => oldCreate.promise });
    act(() => renderer.root.findByProps({ className: "resource-refresh-button resource-create-button" }).props.onClick());
    const terminalAction = renderer.root.findAllByProps({ role: "menuitem" })
      .find((item) => nodeText(item.props.children).includes("新建终端"))!;
    act(() => terminalAction.props.onClick());
    const createButtonClass = "resource-refresh-button resource-create-button";
    expect(nodeText(renderer.root.findByProps({ className: createButtonClass }).props.children)).toContain("创建中");

    updatePanel(renderer, { sessionId: "ses_b", workspaceId: "workspace_b", resources: [] });
    updatePanel(renderer, { sessionId: "ses_resource_tree", workspaceId: "workspace_test", resources: [] });
    expect(nodeText(renderer.root.findByProps({ className: createButtonClass }).props.children)).toContain("新建连接");

    await act(async () => {
      oldCreate.reject(new Error("旧 owner 创建失败"));
      await oldCreate.promise.catch(() => undefined);
    });
    expect(nodeText(renderer.root.findByProps({ className: createButtonClass }).props.children)).toContain("新建连接");
    expect(noticeText(renderer)).toBe("");
    renderer.unmount();
  });
});
