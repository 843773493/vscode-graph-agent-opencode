import { afterEach, describe, expect, test } from "bun:test";
import React from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import {
  DEFAULT_GATEWAY_PANEL_HEIGHT,
  GATEWAY_PANEL_RESIZING_CLASS,
  MAX_GATEWAY_PANEL_HEIGHT,
  MIN_GATEWAY_PANEL_HEIGHT,
} from "../../layout/workbenchLayout";
import type { WorkspaceBottomPanelState } from "../../state/workspaceBottomPanel";
import { useBottomPanelResize } from "./useBottomPanelResize";

type PointerListener = (event: PointerEvent) => void;

interface FakePointerHost {
  classes: Set<string>;
  listenerCount: (type: string) => number;
  dispatch: (type: "pointermove" | "pointerup" | "pointercancel", clientY: number) => void;
}

const originalWindowDescriptor = Object.getOwnPropertyDescriptor(globalThis, "window");
const originalDocumentDescriptor = Object.getOwnPropertyDescriptor(globalThis, "document");

/** 用假 window/document 替换全局，沿用 useSessionGoalController.test.tsx 的打桩方式。 */
function installFakePointerHost(): FakePointerHost {
  const classes = new Set<string>();
  const listeners = new Map<string, Set<PointerListener>>();

  Object.defineProperty(globalThis, "window", {
    configurable: true,
    value: {
      addEventListener(type: string, listener: PointerListener) {
        let registered = listeners.get(type);
        if (!registered) {
          registered = new Set<PointerListener>();
          listeners.set(type, registered);
        }
        registered.add(listener);
      },
      removeEventListener(type: string, listener: PointerListener) {
        listeners.get(type)?.delete(listener);
      },
    },
  });
  Object.defineProperty(globalThis, "document", {
    configurable: true,
    value: {
      body: {
        classList: {
          add: (name: string) => classes.add(name),
          remove: (name: string) => classes.delete(name),
        },
      },
    },
  });

  return {
    classes,
    listenerCount: (type) => listeners.get(type)?.size ?? 0,
    dispatch: (type, clientY) => {
      const event = { type, clientY } as unknown as PointerEvent;
      for (const listener of [...(listeners.get(type) ?? [])]) {
        listener(event);
      }
    },
  };
}

function restoreGlobal(name: "window" | "document", descriptor?: PropertyDescriptor): void {
  if (descriptor) {
    Object.defineProperty(globalThis, name, descriptor);
    return;
  }
  Reflect.deleteProperty(globalThis, name);
}

function panelState(overrides: Partial<WorkspaceBottomPanelState> = {}): WorkspaceBottomPanelState {
  return {
    visible: true,
    height: 300,
    tab: "terminal",
    terminalId: "terminal-1",
    ...overrides,
  };
}

interface MountOptions {
  workspaceId?: string | null;
  panelState?: WorkspaceBottomPanelState;
}

interface MountedHook {
  hook: ReturnType<typeof useBottomPanelResize>;
  unmount: () => void;
  states: () => Record<string, WorkspaceBottomPanelState>;
  updates: Array<Partial<WorkspaceBottomPanelState>>;
}

const mountedRenderers: ReactTestRenderer[] = [];

/** 用 react-test-renderer 的 Probe 组件挂载 hook，与 useGatewayWorkspaceMutations.test.tsx 一致。 */
async function mountHook(options: MountOptions = {}): Promise<MountedHook> {
  const workspaceId = "workspaceId" in options ? options.workspaceId! : "ws-resize";
  const state = options.panelState ?? panelState();
  let currentStates: Record<string, WorkspaceBottomPanelState> = {};
  const updates: Array<Partial<WorkspaceBottomPanelState>> = [];
  let hook: ReturnType<typeof useBottomPanelResize> | undefined;

  function Probe(): React.ReactNode {
    hook = useBottomPanelResize({
      workspaceId,
      panelState: state,
      setWorkspaceBottomPanelStates: (update) => {
        currentStates = typeof update === "function" ? update(currentStates) : update;
      },
      updateBottomPanelState: (patch) => {
        updates.push(patch);
      },
    });
    return null;
  }

  let renderer: ReactTestRenderer | undefined;
  await act(async () => {
    renderer = create(<Probe />);
  });
  mountedRenderers.push(renderer!);

  return {
    hook: hook!,
    unmount: () => {
      act(() => renderer!.unmount());
    },
    states: () => currentStates,
    updates,
  };
}

function pointerDownEvent(clientY: number): React.PointerEvent<HTMLButtonElement> {
  return {
    clientY,
    preventDefault: () => undefined,
  } as unknown as React.PointerEvent<HTMLButtonElement>;
}

afterEach(() => {
  for (const renderer of mountedRenderers.splice(0)) {
    act(() => renderer.unmount());
  }
  restoreGlobal("window", originalWindowDescriptor);
  restoreGlobal("document", originalDocumentDescriptor);
});

describe("useBottomPanelResize 拖拽落盘契约", () => {
  test("按下时进入拖拽态并注册三类指针监听", async () => {
    const host = installFakePointerHost();
    const { hook, updates } = await mountHook();

    act(() => hook.startBottomPanelResize(pointerDownEvent(600)));

    expect(host.classes.has(GATEWAY_PANEL_RESIZING_CLASS)).toBe(true);
    expect(host.listenerCount("pointermove")).toBe(1);
    expect(host.listenerCount("pointerup")).toBe(1);
    expect(host.listenerCount("pointercancel")).toBe(1);
    expect(updates).toEqual([]);
  });

  test("组件卸载时解绑全部指针监听并移除拖拽态 class", async () => {
    const host = installFakePointerHost();
    const { hook, unmount, updates } = await mountHook();

    act(() => hook.startBottomPanelResize(pointerDownEvent(600)));
    expect(host.classes.has(GATEWAY_PANEL_RESIZING_CLASS)).toBe(true);

    unmount();

    expect(host.listenerCount("pointermove")).toBe(0);
    expect(host.listenerCount("pointerup")).toBe(0);
    expect(host.listenerCount("pointercancel")).toBe(0);
    expect(host.classes.has(GATEWAY_PANEL_RESIZING_CLASS)).toBe(false);
    // 卸载清理会同步调用 onFinish，但未移动过，因此不落盘。
    expect(updates).toEqual([]);
  });

  test("指针未移动（deltaY 为 0）时不更新面板高度、松手也不落盘", async () => {
    const host = installFakePointerHost();
    const { hook, states, updates } = await mountHook();

    act(() => hook.startBottomPanelResize(pointerDownEvent(600)));
    host.dispatch("pointermove", 600);
    host.dispatch("pointerup", 600);

    expect(states()).toEqual({});
    expect(updates).toEqual([]);
  });

  test("无激活工作区时拖拽只更新内部高度，不写入工作区面板状态", async () => {
    const host = installFakePointerHost();
    const { hook, states, updates } = await mountHook({ workspaceId: null });

    act(() => hook.startBottomPanelResize(pointerDownEvent(600)));
    host.dispatch("pointermove", 520);

    // workspaceId 为假分支：setWorkspaceBottomPanelStates 从未被调用。
    expect(states()).toEqual({});

    host.dispatch("pointerup", 520);
    // latestHeight 仍然被内部记录并落盘。
    expect(updates).toEqual([{ height: 380 }]);
  });

  test("按下后未移动即松手不落盘", async () => {
    const host = installFakePointerHost();
    const { hook, updates } = await mountHook();

    act(() => hook.startBottomPanelResize(pointerDownEvent(600)));
    host.dispatch("pointerup", 600);

    // moved 为假分支：updateBottomPanelState 不得被调用。
    expect(updates).toEqual([]);
  });

  test("拖拽中按工作区写入实时高度，且以最新 panelState 为基准", async () => {
    const host = installFakePointerHost();
    const state = panelState({ height: 300, tab: "output", terminalId: "terminal-9" });
    const { hook, states } = await mountHook({ panelState: state });

    act(() => hook.startBottomPanelResize(pointerDownEvent(600)));
    host.dispatch("pointermove", 520);

    expect(states()).toEqual({
      "ws-resize": { visible: true, height: 380, tab: "output", terminalId: "terminal-9" },
    });
  });

  test("拖过上限时落盘 clamp 后的上限高度", async () => {
    const host = installFakePointerHost();
    const { hook, states, updates } = await mountHook();

    act(() => hook.startBottomPanelResize(pointerDownEvent(600)));
    // 向上拖动 400 像素，计算高度 700 后必须被 clamp 到上限。
    host.dispatch("pointermove", 200);
    expect(states()["ws-resize"].height).toBe(MAX_GATEWAY_PANEL_HEIGHT);

    host.dispatch("pointerup", 200);
    expect(updates).toEqual([{ height: MAX_GATEWAY_PANEL_HEIGHT }]);
  });

  test("拖过下限时落盘 clamp 后的下限高度", async () => {
    const host = installFakePointerHost();
    const { hook, states, updates } = await mountHook();

    act(() => hook.startBottomPanelResize(pointerDownEvent(600)));
    // 向下拖动 300 像素，计算高度 0 后必须被 clamp 到下限。
    host.dispatch("pointermove", 900);
    expect(states()["ws-resize"].height).toBe(MIN_GATEWAY_PANEL_HEIGHT);

    host.dispatch("pointerup", 900);
    expect(updates).toEqual([{ height: MIN_GATEWAY_PANEL_HEIGHT }]);
  });

  test("pointercancel 与 pointerup 一样落盘并解绑监听", async () => {
    const host = installFakePointerHost();
    const { hook, states, updates } = await mountHook();

    act(() => hook.startBottomPanelResize(pointerDownEvent(600)));
    host.dispatch("pointermove", 520);
    expect(states()["ws-resize"].height).toBe(380);

    host.dispatch("pointercancel", 520);

    expect(updates).toEqual([{ height: 380 }]);
    expect(host.listenerCount("pointermove")).toBe(0);
    expect(host.listenerCount("pointerup")).toBe(0);
    expect(host.listenerCount("pointercancel")).toBe(0);
    expect(host.classes.has(GATEWAY_PANEL_RESIZING_CLASS)).toBe(false);
  });

  test("resetBottomPanelHeight 写入默认面板高度", async () => {
    installFakePointerHost();
    const { hook, updates } = await mountHook();

    act(() => hook.resetBottomPanelHeight());

    expect(updates).toEqual([{ height: DEFAULT_GATEWAY_PANEL_HEIGHT }]);
  });

  test("拖拽未结束就再次 pointerdown 时先解绑旧监听，旧链路不再落盘也不覆盖已落盘高度", async () => {
    const host = installFakePointerHost();
    const { hook, updates } = await mountHook();

    // 第一次拖拽：移动到 520 但故意不松手，保持拖拽态。
    act(() => hook.startBottomPanelResize(pointerDownEvent(600)));
    const firstMove = host.listenerCount("pointermove");
    expect(firstMove).toBe(1);
    host.dispatch("pointermove", 520);

    // 再次按下：旧监听必须先被解绑，随后重新注册同一组监听。
    act(() => hook.startBottomPanelResize(pointerDownEvent(600)));
    expect(host.listenerCount("pointermove")).toBe(1);
    expect(host.listenerCount("pointerup")).toBe(1);
    expect(host.listenerCount("pointercancel")).toBe(1);
    expect(host.classes.has(GATEWAY_PANEL_RESIZING_CLASS)).toBe(true);

    // 旧链路收尾时已移动过，落盘 380；新链路未移动，不产生额外覆盖。
    expect(updates).toEqual([{ height: 380 }]);
    host.dispatch("pointerup", 600);
    expect(updates).toEqual([{ height: 380 }]);
  });
});
