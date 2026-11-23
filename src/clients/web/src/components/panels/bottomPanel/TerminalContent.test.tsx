import { PanelToolbar, type PanelToolbarControl, type RenderPanelToolbar } from "./PanelToolbar";
import { afterEach, describe, expect, jest, test } from "bun:test";
import React from "react";
import { act, create as createRenderer, type ReactTestRenderer, type TestRendererOptions } from "react-test-renderer";
import type { WorkspaceResourceDTO } from "../../../api/workspaceResources";
import { TerminalContent } from "./TerminalContent";
import { TerminalOutput, TerminalThemeContext } from "./TerminalOutput";
import { restoreGlobalDescriptor } from "../../../tests/testGlobals";
import { gatewayWorkspaceKey } from "../../../api/workspaceIdentity";

const originalWindowDescriptor = Object.getOwnPropertyDescriptor(globalThis, "window");

// 内容测试不挂真实 DOM；快照渲染器的释放仍需要一个明确的宿主句柄。
function create(element: React.ReactElement, options?: TestRendererOptions) {
  const contentWindow = { postMessage: jest.fn() };
  return createRenderer(element, { createNodeMock: node => node.type === "iframe"
    ? { src: node.props.src, contentWindow } : { replaceChildren() {} }, ...options });
}

function publishFrameReady(renderer: ReactTestRenderer, overrides: Record<string, unknown> = {}): void {
  const frame = renderer.root.findByType("iframe");
  const event = new Event("message");
  for (const [key, value] of Object.entries({
    origin: window.location.origin, source: frame.instance.contentWindow,
    data: { type: "boxteam.terminal.ready", terminalId: new URL(frame.props.src).searchParams.get("terminalId") },
    ...overrides,
  })) Object.defineProperty(event, key, { value });
  window.dispatchEvent(event);
}

afterEach(() => {
  jest.useRealTimers();
  restoreGlobalDescriptor("window", originalWindowDescriptor);
});

/** buildGatewayAttachUrl 依赖 window.location.origin；测试环境没有 DOM。 */
function installWindow(): void {
  Object.defineProperty(globalThis, "window", {
    configurable: true,
    value: Object.assign(new EventTarget(), {
      location: { origin: "http://127.0.0.1:8011", port: "8011" },
      setTimeout: globalThis.setTimeout.bind(globalThis),
      clearTimeout: globalThis.clearTimeout.bind(globalThis),
    }),
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
    apiPort: 0,
    controller: {
      items: entries, loading: false, mutating: false, error: null, refresh: async () => {},
      control: async () => entries[0]!, create: async () => entries[0]!,
    },
    currentThread: null,
    threadTargetUnavailable,
    workspaceId: entries[0]?.workspace_id ? gatewayWorkspaceKey({ gatewayId: "gateway_test", workspaceId: entries[0].workspace_id }) : "",
    workspaceName: "测试工作区",
    selectedTerminalId: null,
    onSelectTerminal: () => {},
    renderToolbar: (controls => <PanelToolbar controls={controls} />) satisfies RenderPanelToolbar,
    showTerminalMenu: jest.fn(),
  };
}

function renderPanel(entries: WorkspaceResourceDTO[], threadTargetUnavailable = false): ReactTestRenderer {
  let renderer!: ReactTestRenderer;
  act(() => {
    renderer = create(<TerminalContent {...panelProps(entries, threadTargetUnavailable)} />);
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

describe("TerminalContent 终端连接兜底", () => {
  test("临时终端使用已有来源关联连接；没有焦点 Thread 或切换 Thread 不改变连接", () => {
    installWindow();
    const source = { session_id: "ses_owner", thread_id: "thr_owner" };
    const terminal = { ...entry("workspace_test", "term_temporary"), retention_scope: "thread",
      source_thread: source, thread_associations: [source] };
    const props = panelProps([terminal]);
    let renderer!: ReactTestRenderer;
    act(() => { renderer = create(<TerminalContent {...props} />); });
    const url = renderer.root.findByType("iframe").props.src;
    expect(new URL(url).searchParams.get("sessionId")).toBe(source.session_id);
    expect(new URL(url).searchParams.get("threadId")).toBe(source.thread_id);
    expect(new URL(url).searchParams.get("embedded")).toBe("1");
    act(() => renderer.update(<TerminalContent {...props} currentThread={{ session_id: "ses_other", thread_id: "thr_other" }} />));
    expect(renderer.root.findByType("iframe").props.src).toBe(url);
    expect(findFrameFallback(renderer)).toBeUndefined();
    act(() => renderer.unmount());
  });

  test("工作区终端没有焦点 Thread 或关联仍可连接，不附带 Thread 授权参数", () => {
    installWindow();
    const renderer = renderPanel([entry("workspace_test", "term_independent")]);
    const url = new URL(renderer.root.findByType("iframe").props.src);
    expect(url.searchParams.has("threadId")).toBe(false);
    expect(url.searchParams.has("sessionId")).toBe(false);
    expect(findFrameFallback(renderer)).toBeUndefined();
    act(() => renderer.unmount());
  });

  test("右侧更多菜单沿 iframe 执行搜索，沿 owner 停止；失效的帧不接收旧资源操作", async () => {
    installWindow();
    const terminal = { ...entry("workspace_test", "term_menu"), available_actions: ["stop"] };
    const props = panelProps([terminal]);
    const postMessage = jest.fn();
    const control = jest.fn(async () => ({ ...terminal, status: "terminated" }));
    let renderer!: ReactTestRenderer;
    const frame = { src: "", contentWindow: { postMessage } };
    act(() => { renderer = create(<TerminalContent {...props} controller={{ ...props.controller, control }} />,
      { createNodeMock: node => {
        if (node.type !== "iframe") return { replaceChildren() {} };
        frame.src = node.props.src;
        return frame;
      } }); });
    act(() => publishFrameReady(renderer));
    expect(postMessage).toHaveBeenCalledWith({ type: "boxteam.terminal.theme", terminalId: terminal.resource_id, theme: {} }, window.location.origin);
    const more = renderer.root.findByProps({ className: "terminal-panel-list-action terminal-panel-list-more" });
    act(() => more.props.onClick({ currentTarget: { getBoundingClientRect: () => ({ left: 100, bottom: 200 }) } }));
    const [anchor, actions] = props.showTerminalMenu.mock.calls[0]! as unknown as [
      { x: number; y: number }, readonly Extract<PanelToolbarControl, { kind: "action" }>[]];
    expect(anchor).toEqual({ x: 100, y: 200 });
    expect(control).not.toHaveBeenCalled();
    const search = actions.find(action => action.id === "search")!;
    expect(search.disabled).toBe(false);
    search.run();
    expect(postMessage).toHaveBeenCalledWith({ type: "boxteam.terminal.action", terminalId: terminal.resource_id, action: "search" }, window.location.origin);
    await act(async () => { await actions.find(action => action.id === "stop")!.run(); });
    expect(control).toHaveBeenCalledWith(terminal, "stop", undefined);
    frame.src = frame.src.replace("term_menu", "term_other");
    expect(() => search.run()).toThrow();
    expect(postMessage).toHaveBeenCalledTimes(2);
    act(() => renderer.unmount());
  });
  test("主题变更更新当前帧的完整配色，不重新连接终端或读取资源", () => {
    installWindow();
    const terminal = entry("workspace_test", "term_theme");
    const props = panelProps([terminal]);
    const postMessage = jest.fn();
    const light = { background: "#ffffff", foreground: "#222222", green: "#008000" };
    const dark = { background: "#181818", foreground: "#eeeeee", green: "#11ff11" };
    const render = (theme: typeof light) => <TerminalThemeContext.Provider value={theme}><TerminalContent {...props} /></TerminalThemeContext.Provider>;
    const contentWindow = { postMessage };
    let renderer!: ReactTestRenderer;
    act(() => { renderer = create(render(light), { createNodeMock: node => node.type === "iframe"
      ? { src: node.props.src, contentWindow } : { replaceChildren() {} } }); });
    const url = renderer.root.findByType("iframe").props.src;
    act(() => publishFrameReady(renderer));
    expect(postMessage).toHaveBeenLastCalledWith({ type: "boxteam.terminal.theme", terminalId: terminal.resource_id, theme: light }, window.location.origin);
    act(() => renderer.update(render(dark)));
    expect(postMessage).toHaveBeenLastCalledWith({ type: "boxteam.terminal.theme", terminalId: terminal.resource_id, theme: dark }, window.location.origin);
    expect(renderer.root.findByType("iframe").props.src).toBe(url);
    expect(postMessage).toHaveBeenCalledTimes(2);
    act(() => renderer.unmount());
  });
  test("行内垃圾桶停止对应的非选中终端，不切换当前选择", async () => {
    installWindow();
    const selected = entry("workspace_test", "terminal_selected");
    const other = { ...entry("workspace_test", "terminal_other"), owner_revision: 7, available_actions: ["stop"] };
    const control = jest.fn(async () => ({ ...other, status: "exited" }));
    const select = jest.fn();
    const props = panelProps([selected, other]);
    let renderer!: ReactTestRenderer;
    act(() => { renderer = create(<TerminalContent {...props} selectedTerminalId={selected.resource_id}
      onSelectTerminal={select} controller={{ ...props.controller, control }} />); });
    const row = renderer.root.findByProps({ "data-resource-id": other.resource_id });
    const action = row.findByProps({ className: "terminal-panel-list-action" });
    expect(action.props.disabled).toBe(false);
    await act(async () => { await action.props.onClick(); });
    expect(control).toHaveBeenCalledWith(other, "stop", undefined);
    expect(select).not.toHaveBeenCalled();
    expect(renderer.root.findByType("iframe").props.src).toContain(selected.resource_id);
    act(() => renderer.unmount());
  });

  test("Delete 键删除当前终端成功后选相邻项，删除最后一项清空选择", async () => {
    installWindow();
    const deleted = { ...entry("workspace_test", "terminal_delete"), status: "exited", available_actions: ["delete"] };
    const sibling = entry("workspace_test", "terminal_keep");
    const select = jest.fn();
    for (const items of [[deleted, sibling], [deleted]]) {
      const props = panelProps(items);
      const control = jest.fn(async () => ({ ...deleted, status: "deleted" }));
      let renderer!: ReactTestRenderer;
      act(() => { renderer = create(<TerminalContent {...props} selectedTerminalId={deleted.resource_id}
        onSelectTerminal={select} controller={{ ...props.controller, control }} />); });
      const row = renderer.root.findByProps({ "data-resource-id": deleted.resource_id });
      const main = row.findAllByType("button")[0]!;
      await act(async () => { await main.props.onKeyDown({ key: "Delete", preventDefault() {} }); });
      expect(control).toHaveBeenLastCalledWith(deleted, "delete", undefined);
      expect(select).toHaveBeenLastCalledWith(items.length === 2 ? sibling.resource_id : null);
      act(() => renderer.unmount());
    }
  });

  test("删除等待期间换到其它终端，不覆盖新选择；失败不伪造删除", async () => {
    installWindow();
    const target = { ...entry("workspace_test", "terminal_delete"), status: "exited", available_actions: ["delete"] };
    const sibling = entry("workspace_test", "terminal_keep");
    for (const failed of [false, true]) {
      const pending = Promise.withResolvers<WorkspaceResourceDTO>();
      const select = jest.fn();
      const props = panelProps([target, sibling]);
      const controller = { ...props.controller, control: async () => pending.promise };
      let renderer!: ReactTestRenderer;
      act(() => { renderer = create(<TerminalContent {...props} selectedTerminalId={target.resource_id}
        onSelectTerminal={select} controller={controller} />); });
      const action = renderer.root.findByProps({ "data-resource-id": target.resource_id }).findByProps({ className: "terminal-panel-list-action" });
      act(() => { action.props.onClick(); });
      if (!failed) act(() => renderer.update(<TerminalContent {...props} selectedTerminalId={sibling.resource_id}
        onSelectTerminal={select} controller={controller} />));
      await act(async () => {
        if (failed) pending.reject(new Error("删除失败"));
        else pending.resolve({ ...target, status: "deleted" });
      });
      expect(select).not.toHaveBeenCalled();
      act(() => renderer.unmount());
    }
  });

  test("仍有关联或 owner 未允许的终端禁用行内删除，不调用 writer", () => {
    installWindow();
    const target = { ...entry("workspace_test", "terminal_associated"), status: "exited",
      available_actions: ["stop"], thread_associations: [{ session_id: "ses_owner", thread_id: "thr_owner" }] };
    const props = panelProps([target]);
    const control = jest.fn();
    let renderer!: ReactTestRenderer;
    act(() => { renderer = create(<TerminalContent {...props} controller={{ ...props.controller, control }} />); });
    const row = renderer.root.findByProps({ "data-resource-id": target.resource_id });
    const action = row.findByProps({ className: "terminal-panel-list-action" });
    expect(action.props.disabled).toBe(true);
    expect(action.props.title).toContain("请先解除所有线程关联");
    row.findAllByType("button")[0]!.props.onKeyDown({ key: "Delete", preventDefault() {} });
    expect(control).not.toHaveBeenCalled();
    act(() => renderer.unmount());
  });
  test("右侧终端列表向左拖动扩大，键盘沿分隔线方向调整并限制两端", () => {
    installWindow();
    const renderer = renderPanel([entry("workspace_test", "terminal_ready")]);
    const sash = renderer.root.findByProps({ role: "separator" });
    const list = renderer.root.findByProps({ className: "terminal-panel-list" });
    const target = {
      parentElement: { clientWidth: 1000, getBoundingClientRect: () => ({ left: 100, right: 1100, width: 1000 }) },
      setPointerCapture() {}, hasPointerCapture: () => true, releasePointerCapture() {},
    };
    act(() => sash.props.onPointerDown({ preventDefault() {}, currentTarget: target, pointerId: 1 }));
    act(() => sash.props.onPointerMove({ currentTarget: target, clientX: 900 }));
    expect(list.props.style.flex).toBe("0 1 200px");
    act(() => sash.props.onKeyDown({ preventDefault() {}, currentTarget: target, key: "ArrowLeft" }));
    expect(list.props.style.flex).toBe("0 1 220px");
    act(() => sash.props.onKeyDown({ preventDefault() {}, currentTarget: target, key: "ArrowRight" }));
    expect(list.props.style.flex).toBe("0 1 200px");
    act(() => sash.props.onPointerMove({ currentTarget: target, clientX: 0 }));
    expect(list.props.style.flex).toBe("0 1 460px");
    act(() => sash.props.onPointerMove({ currentTarget: target, clientX: 1200 }));
    expect(list.props.style.flex).toBe("0 1 40px");
    act(() => sash.props.onPointerUp({ currentTarget: target, pointerId: 1 }));
    act(() => sash.props.onPointerMove({ currentTarget: target, clientX: 900 }));
    expect(list.props.style.flex).toBe("0 1 40px");
    act(() => renderer.unmount());
  });

  test("新建分体菜单向原 writer 传递明确的保留范围，选择器按 ID 切换", async () => {
    installWindow();
    const first = entry("workspace_test", "terminal_first");
    const second = entry("workspace_test", "terminal_second");
    const props = panelProps([first, second]);
    const currentThread = { session_id: "ses_owner", thread_id: "thr_owner" };
    const createTerminal = jest.fn(async () => second);
    const select = jest.fn();
    let controls: readonly PanelToolbarControl[] = [];
    let renderer!: ReactTestRenderer;
    act(() => { renderer = create(<TerminalContent {...props} currentThread={currentThread}
      controller={{ ...props.controller, create: createTerminal }} onSelectTerminal={select}
      renderToolbar={items => { controls = items; return null; }} />); });
    const createControl = controls.find(control => control.id === "create");
    if (createControl?.kind !== "action") throw new Error("缺少新建终端操作");
    await act(async () => { await createControl.alternatives![1]!.run(); });
    expect(createTerminal).toHaveBeenLastCalledWith(currentThread, "workspace");
    expect(select).toHaveBeenLastCalledWith("terminal_second");
    await act(async () => { await createControl.alternatives![0]!.run(); });
    expect(createTerminal).toHaveBeenLastCalledWith(currentThread, "thread");
    const selector = controls.find(control => control.id === "terminal");
    if (selector?.kind !== "select") throw new Error("缺少终端选择器");
    act(() => selector.onChange("terminal_first"));
    expect(select).toHaveBeenLastCalledWith("terminal_first");
    act(() => renderer.unmount());
  });

  test("同名终端按明确 ID 连接第二项", () => {
    installWindow();
    const items = [entry("workspace_test", "terminal_first"), entry("workspace_test", "terminal_second")];
    let renderer!: ReactTestRenderer;
    act(() => { renderer = create(<TerminalContent {...panelProps(items)} selectedTerminalId="terminal_second" />); });
    expect(renderer.root.findByType("iframe").props.src).toContain("terminal_second");
    expect(renderer.root.findByType("iframe").props.src).not.toContain("terminal_first");
    act(() => renderer.unmount());
  });

  test("同名旧终端明确显示退出状态和历史，不被运行中终端替代或当成连接故障", () => {
    installWindow();
    const items = [entry("workspace_test", "terminal_live"), {
      ...entry("workspace_test", "terminal_old"), status: "exited", reconciliation_status: "reconcile_required",
      error: "Node 运行时 身份或连接尚待核实", metadata: { title: "主终端", exit_code: 0 },
    }];
    let renderer!: ReactTestRenderer;
    act(() => { renderer = create(<TerminalContent {...panelProps(items)} selectedTerminalId="terminal_old" />); });
    expect(renderer.root.findAllByType("iframe")).toHaveLength(0);
    expect(findFrameFallback(renderer)).toBeUndefined();
    expect(renderer.root.findByType(TerminalOutput).props.resourceId).toBe("terminal_old");
    expect(renderer.root.findAllByProps({ className: "terminal-panel-history-status" })).toHaveLength(0);
    const list = renderer.root.findByProps({ className: "terminal-panel-list" });
    const rows = list.findAll(node => node.type === "button" && String(node.props.className).split(" ").includes("terminal-panel-list-item"));
    expect(textOf(rows[0]!.props.children)).toContain("运行中");
    expect(textOf(rows[1]!.props.children)).toContain("已退出");
    expect(rows[1]!.props.title).toContain("退出码 0");
    // 仍显示后端真实错误；历史展示不修改或伪造 owner 的核实结果。
    expect(textOf(renderer.root.findByProps({ className: "terminal-panel-error-slot" }).props.children)).toContain("身份或连接尚待核实");
    expect(textOf(renderer.root.findByType("summary").props.children)).toBe("终端已退出，当前显示历史输出；资源恢复状态待确认");
    act(() => renderer.update(<TerminalContent {...panelProps(items)} selectedTerminalId="terminal_live" />));
    expect(renderer.root.findByType("iframe").props.src).toContain("terminal_live");
    expect(renderer.root.findAllByType("details")).toHaveLength(0);
    expect(renderer.root.findAllByType(TerminalOutput)).toHaveLength(0);
    act(() => renderer.unmount());
  });

  test("运行中但身份未核实的终端仍拒绝连接", () => {
    installWindow();
    const renderer = renderPanel([{ ...entry("workspace_test", "terminal_unknown"), reconciliation_status: "unknown" }]);
    expect(renderer.root.findAllByType("iframe")).toHaveLength(0);
    expect(textOf(findFrameFallback(renderer)!.props.children)).toContain("终端运行状态尚待核实");
    act(() => renderer.unmount());
  });
  test("明确选择的终端消失后不连接清单中的其它终端", () => {
    installWindow();
    let renderer!: ReactTestRenderer;
    act(() => {
      renderer = create(<TerminalContent {...panelProps([entry("workspace_test", "terminal_other")])} selectedTerminalId="terminal_deleted" />);
    });
    expect(renderer.root.findAllByType("iframe")).toHaveLength(0);
    expect(textOf(renderer.root.findByProps({ role: "alert" }).props.children)).toContain("terminal_deleted");
    act(() => renderer.unmount());
  });

  test("终端 iframe 长时间未就绪时给出超时说明与重试入口", () => {
    installWindow();
    jest.useFakeTimers();
    const renderer = renderPanel([entry("workspace_test", "terminal_ready")]);

    // 刚挂载时只显示 iframe，不应立刻报超时。
    expect(findFrameFallback(renderer)).toBeUndefined();

    // 模拟每两秒取得内容相同的新列表对象，不能因此无限延长 iframe 的期限。
    for (let elapsed = 0; elapsed < 16_000; elapsed += 2_000) {
      act(() => { jest.advanceTimersByTime(2_000); });
      act(() => renderer.update(<TerminalContent {...panelProps([entry("workspace_test", "terminal_ready")])} />));
    }

    const fallback = findFrameFallback(renderer);
    expect(fallback).toBeDefined();
    expect(textOf(fallback!.props.children)).toContain("15 秒仍未就绪");
    renderer.unmount();
  });

  test("HTML 加载完成但模块未初始化仍会超时，不能误报就绪", () => {
    installWindow();
    jest.useFakeTimers();
    const renderer = renderPanel([entry("workspace_test", "terminal_ready")]);
    act(() => renderer.root.findByType("iframe").props.onLoad?.());
    act(() => { jest.advanceTimersByTime(16_000); });
    expect(findFrameFallback(renderer)).toBeDefined();
    act(() => renderer.unmount());
  });

  test("只有当前终端同源 iframe 的初始化确认才能结束就绪等待", () => {
    installWindow();
    jest.useFakeTimers();
    const renderer = renderPanel([entry("workspace_test", "terminal_ready")]);
    act(() => {
      publishFrameReady(renderer, { origin: "http://another-host" });
      publishFrameReady(renderer, { source: {} });
      publishFrameReady(renderer, { data: { type: "boxteam.terminal.ready", terminalId: "terminal_other" } });
      jest.advanceTimersByTime(16_000);
    });
    expect(findFrameFallback(renderer)).toBeDefined();
    act(() => publishFrameReady(renderer));
    act(() => renderer.update(<TerminalContent {...panelProps([entry("workspace_test", "terminal_ready")])} />));
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

  test("资源错误和操作错误归属左侧终端内容，不进入工具栏或右侧列表", () => {
    installWindow();
    const props = panelProps([{
      ...entry("workspace_test", "terminal_error"),
      error: "终端运行状态尚待核实",
    }]);
    const renderer = create(
      <TerminalContent
        {...props}
        controller={{ ...props.controller, error: "刷新终端失败" }}
      />,
    );

    const toolbar = renderer.root.findByType(PanelToolbar);
    const errorSlot = renderer.root.findByProps({ className: "terminal-panel-error-slot" });
    const alert = errorSlot.findByProps({ role: "alert" });
    expect(toolbar.findAll((node) => node.props.role === "alert")).toHaveLength(0);
    expect(errorSlot.parent?.props.className).toBe("terminal-panel-viewer");
    expect(alert.props.title).toBe("终端运行状态尚待核实 · 刷新终端失败");
    expect(textOf(alert.props.children)).toBe("终端运行状态尚待核实 · 刷新终端失败");
    expect(renderer.root.findAllByProps({ className: "terminal-panel-frame-fallback" })).toHaveLength(0);
    renderer.unmount();
  });

  test("临时孤儿不发起无授权 iframe，显示重新关联与保留入口", () => {
    installWindow();
    const orphan: WorkspaceResourceDTO = { ...entry("workspace_test", "term_orphan"), retention_scope: "thread", available_actions: ["promote", "attach"], thread_associations: [] };
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
