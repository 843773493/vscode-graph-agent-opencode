import { afterEach, describe, expect, test } from "bun:test";
import React from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import type { ResourceAction, ResourceThreadRef, WorkspaceResourceDTO } from "../../../api/workspaceResources";
import type { WorkspaceResourcesController } from "../../../hooks/workspace/useWorkspaceResources";
import WarmConfirmProvider from "../../shell/WarmConfirmProvider";
import { restoreGlobalDescriptor } from "../../../tests/testGlobals";
import { BrowserContent } from "./BrowserContent";

const currentThread: ResourceThreadRef = { session_id: "ses_current", thread_id: "thr_main" };
const renderers: ReactTestRenderer[] = [];
const originalClipboard = Object.getOwnPropertyDescriptor(navigator, "clipboard");
const originalDocument = Object.getOwnPropertyDescriptor(globalThis, "document");

afterEach(() => {
  for (const renderer of renderers.splice(0)) act(() => renderer.unmount());
  if (originalClipboard) Object.defineProperty(navigator, "clipboard", originalClipboard);
  else Reflect.deleteProperty(navigator, "clipboard");
  restoreGlobalDescriptor("document", originalDocument);
});

function resource(overrides: Partial<WorkspaceResourceDTO> = {}): WorkspaceResourceDTO {
  return {
    resource_id: "browser_shared", workspace_id: "backend-workspace", kind: "browser",
    retention_scope: "workspace", source_thread: currentThread, thread_associations: [], owner_revision: 7,
    status: "running", reconciliation_status: "verified", available_actions: ["attach", "detach"],
    metadata: { title: "工作区浏览器" }, created_at: "2026-10-31T00:00:00Z", updated_at: "2026-10-31T00:00:00Z",
    ...overrides,
  };
}

function mount(item: WorkspaceResourceDTO, thread: ResourceThreadRef | null = currentThread) {
  const calls: Array<{ resource: WorkspaceResourceDTO; action: ResourceAction; thread?: ResourceThreadRef }> = [];
  const controller: WorkspaceResourcesController = {
    items: [item], loading: false, mutating: false, error: null,
    refresh: async () => {}, create: async () => { throw new Error("本场景不得创建浏览器"); },
    control: async (target, action, ref) => {
      calls.push({ resource: target, action, thread: ref });
      return target;
    },
  };
  let renderer!: ReactTestRenderer;
  act(() => {
    renderer = create(<WarmConfirmProvider><BrowserContent controller={controller} workspaceId="gateway-workspace"
      currentThread={thread} renderToolbar={tools => <header>{tools}</header>} /></WarmConfirmProvider>);
  });
  renderers.push(renderer);
  act(() => renderer.root.findByProps({ className: "resource-tree-chevron" }).props.onClick());
  return { renderer, calls, attach: () => renderer.root.findAllByProps({ children: "关联当前会话" }) };
}

/** 仅替换浏览器能力；复制仍经过真实资源行和共享剪贴板实现。 */
function fallbackClipboard(succeeds: boolean) {
  const copied: string[] = [];
  const textarea = {
    value: "", style: { position: "", left: "", top: "" },
    setAttribute() {}, focus() {}, select() {}, remove() {},
  };
  Object.defineProperty(globalThis, "document", { configurable: true, value: {
    createElement: () => textarea, body: { appendChild() {} },
    execCommand(command: string) {
      expect(command).toBe("copy");
      copied.push(textarea.value);
      return succeeds;
    },
  } });
  return copied;
}

describe("工作区 Browser 的复制入口", () => {
  test("Clipboard API 缺失时仍复制精确资源 ID，不发出资源变更", async () => {
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: undefined });
    const copied = fallbackClipboard(true);
    const item = resource();
    const mounted = mount(item);
    await act(async () => { mounted.renderer.root.findByProps({ children: "复制 ID" }).props.onClick(); });
    expect(copied).toEqual([item.resource_id]);
    expect(mounted.renderer.root.findAllByProps({ role: "alert" })).toHaveLength(0);
    expect(mounted.calls).toEqual([]);
  });

  test("Clipboard API 与兼容复制均拒绝时，在当前面板保留完整错误", async () => {
    const written: string[] = [];
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: {
      async writeText(value: string) { written.push(value); throw new Error("fixture-clipboard-rejected"); },
    } });
    const copied = fallbackClipboard(false);
    const item = resource();
    const mounted = mount(item);
    await act(async () => { mounted.renderer.root.findByProps({ children: "复制 ID" }).props.onClick(); });
    expect(written).toEqual([item.resource_id]);
    expect(copied).toEqual([item.resource_id]);
    expect(mounted.renderer.root.findByProps({ role: "alert" }).props.children)
      .toBe("Clipboard API 失败：fixture-clipboard-rejected；兼容复制失败：浏览器拒绝复制文本");
    expect(mounted.attach()).toHaveLength(1);
    expect(mounted.calls).toEqual([]);
  });
});

describe("工作区 Browser 的精确 Thread 关联", () => {
  test.each([
    { label: "其它 Session 的相同 Thread ID", retention_scope: "workspace", association: { session_id: "ses_other", thread_id: currentThread.thread_id } },
    { label: "当前 Session 已关联的 child", retention_scope: "thread", association: { session_id: currentThread.session_id, thread_id: "thr_child" } },
  ] as const)("$label 不吞掉当前 Thread 的关联入口", async ({ retention_scope, association }) => {
    const item = resource({ retention_scope, thread_associations: [association] });
    const mounted = mount(item);
    expect(mounted.attach()).toHaveLength(1);
    expect(mounted.attach()[0]!.props.disabled).toBe(false);
    await act(async () => { mounted.attach()[0]!.props.onClick(); });
    expect(mounted.calls).toEqual([{ resource: item, action: "attach", thread: currentThread }]);
    expect(mounted.calls[0]!.resource.owner_revision).toBe(7);
  });

  test("完整 Session/Thread pair 已关联时不再提供关联入口", () => {
    const mounted = mount(resource({ thread_associations: [currentThread] }));
    expect(mounted.attach()).toHaveLength(0);
    expect(mounted.calls).toEqual([]);
  });

  test("其它来源的临时 Browser 不允许关联当前 Thread", () => {
    const mounted = mount(resource({ retention_scope: "thread", source_thread: { session_id: "ses_other", thread_id: currentThread.thread_id } }));
    expect(mounted.attach()).toHaveLength(0);
    expect(mounted.calls).toEqual([]);
  });

  test("当前原版编辑器没有 Thread 时不提供关联入口", () => {
    const mounted = mount(resource(), null);
    expect(mounted.attach()).toHaveLength(0);
    expect(mounted.calls).toEqual([]);
  });
});
