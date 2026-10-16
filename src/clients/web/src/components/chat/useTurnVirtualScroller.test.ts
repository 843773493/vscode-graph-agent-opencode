import { afterEach, describe, expect, test } from "bun:test";
import React from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import type { VirtuosoHandle } from "react-virtuoso";
import type { ConversationView } from "../../types/frontend";
import type { ChatTranscriptItem } from "./transcriptItems";
import { restoreGlobalDescriptor } from "../../tests/testGlobals";
import {
  requestTurnFollowLatestFrame,
  turnFollowOutput,
  useTurnVirtualScroller,
} from "./useTurnVirtualScroller";

const windowDescriptor = Object.getOwnPropertyDescriptor(globalThis, "window");
const elementDescriptor = Object.getOwnPropertyDescriptor(globalThis, "HTMLElement");
const observerDescriptor = Object.getOwnPropertyDescriptor(globalThis, "ResizeObserver");
let mounted: ReactTestRenderer | null = null;

afterEach(() => {
  if (mounted) act(() => mounted?.unmount());
  mounted = null;
  restoreGlobalDescriptor("window", windowDescriptor);
  restoreGlobalDescriptor("HTMLElement", elementDescriptor);
  restoreGlobalDescriptor("ResizeObserver", observerDescriptor);
});

function turn(id: string, ordinal: number, pending = false): Extract<ChatTranscriptItem, { kind: "turn" }> {
  return { kind: "turn", key: id, conversation: {
    conversationId: id, sessionId: "session", userMessage: null, events: [], jobId: id, source: "turn",
    turnId: id, turnOrdinal: ordinal, displayMode: "history", pending, status: "done",
  } satisfies ConversationView };
}

function deferred() {
  let resolve!: () => void;
  let reject!: (error: Error) => void;
  const promise = new Promise<void>((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

function navigationHarness(initial: ChatTranscriptItem[], loader: (id: string, signal?: AbortSignal) => Promise<void> = async () => {}, mountOnScroll = true) {
  let items = initial;
  let scopeKey = "workspace/session/thread";
  let viewState: { turn_anchor: string | null; scroll_offset: number; follow_latest: boolean } | null = null;
  let current!: ReturnType<typeof useTurnVirtualScroller>;
  let nextFrame = 0;
  const frames = new Map<number, FrameRequestCallback>();
  const heights = new Map(initial.map((item) => [item.key, 150]));
  const scrolls: Array<{ index: number | "LAST"; align?: string; behavior?: string; offset?: number }> = [];
  const positions: Array<{ turn_anchor: string | null; scroll_offset: number; follow_latest: boolean }> = [];
  const statuses: string[] = [];
  let resize!: () => void;
  const windowListeners = new Map<string, (event: { type: string; button: number; buttons: number; clientX: number; clientY: number; target: Scroller }) => void>();
  class Scroller {
    scrollTop = 0;
    clientHeight = 200;
    mountedId: string | null = null;
    parentElement: Scroller | null = null;
    onwheel: unknown;
    ontouchstart: (() => void) | null = null;
    onscroll: (() => void) | null = null;
    listeners = new Map<string, Set<(event: { deltaY: number }) => void>>();
    get scrollHeight() { return items.reduce((sum, item) => sum + (heights.get(item.key) ?? 150), 0); }
    addEventListener(name: string, callback: (event: { deltaY: number }) => void) {
      const listeners = this.listeners.get(name) ?? new Set();
      listeners.add(callback);
      this.listeners.set(name, listeners);
    }
    removeEventListener(name: string, callback: (event: { deltaY: number }) => void) { this.listeners.get(name)?.delete(callback); }
    getBoundingClientRect() { return { top: 0, bottom: this.clientHeight, right: 800, left: 0 }; }
    querySelectorAll() {
      const itemIndex = items.findIndex((item) => item.key === this.mountedId);
      if (itemIndex < 0) return [];
      return [{ dataset: { turnId: this.mountedId }, getBoundingClientRect: () => {
        const top = items.slice(0, itemIndex).reduce((sum, item) => sum + (heights.get(item.key) ?? 150), 0) - this.scrollTop;
        return { top, bottom: top + (heights.get(items[itemIndex].key) ?? 150) };
      } }];
    }
  }
  const scroller = new Scroller();
  const childButton = new Scroller();
  const childIcon = new Scroller();
  childButton.parentElement = scroller;
  childIcon.parentElement = childButton;
  Object.defineProperty(globalThis, "HTMLElement", { configurable: true, value: Scroller });
  Object.defineProperty(globalThis, "ResizeObserver", { configurable: true, value: class {
    constructor(callback: () => void) { resize = callback; }
    observe() {}
    disconnect() {}
  } });
  Object.defineProperty(globalThis, "window", { configurable: true, value: {
    addEventListener(name: string, callback: (event: { type: string; button: number; buttons: number; clientX: number; clientY: number; target: Scroller }) => void) { windowListeners.set(name, callback); },
    removeEventListener(name: string) { windowListeners.delete(name); },
    requestAnimationFrame(callback: FrameRequestCallback) { const id = ++nextFrame; frames.set(id, callback); return id; },
    cancelAnimationFrame(id: number) { frames.delete(id); },
    setTimeout: globalThis.setTimeout.bind(globalThis), clearTimeout: globalThis.clearTimeout.bind(globalThis),
  } });
  function Harness() {
    current = useTurnVirtualScroller({
      items, scopeKey, onLoadAroundTurn: loader, onLoadNewerMessages: async () => {}, onLoadOlderMessages: async () => {},
      hasNewerMessages: false, hasOlderMessages: false, loadingNewerMessages: false, loadingOlderMessages: false,
      historyLoading: false, viewState, onViewStateChange: (position) => positions.push(position),
      onViewStateRestoreStatus: (message) => statuses.push(message),
    });
    current.streamRef.current = { scrollToIndex: (location: { index: number | "LAST"; align?: string }) => {
      scrolls.push(location);
      const index = location.index === "LAST" ? items.length - 1 : location.index;
      if (mountOnScroll) scroller.mountedId = items[index]?.key ?? null;
      const top = items.slice(0, index).reduce((sum, item) => sum + (heights.get(item.key) ?? 150), 0);
      scroller.scrollTop = location.align === "end" ? Math.max(0, scroller.scrollHeight - scroller.clientHeight) : top;
    } } as unknown as VirtuosoHandle;
    return null;
  }
  act(() => { mounted = create(React.createElement(Harness)); });
  act(() => current.bindScroller(scroller as unknown as HTMLElement));
  const dispatchScrollbarEvent = (type: string, target: Scroller, buttons: number) => act(() => {
    windowListeners.get(type)?.({ type, button: 0, buttons, clientX: 798, clientY: 100, target });
  });
  const flush = () => {
    act(() => { const queued = [...frames.values()]; frames.clear(); for (const callback of queued) callback(16); });
  };
  const settle = () => { flush(); act(() => current.scheduleAnchorAlignment()); flush(); };
  return {
    state: () => current, scroller, scrolls, positions, statuses, heights, settle, flush,
    update(nextItems = items, nextScopeKey = scopeKey) {
      items = nextItems; scopeKey = nextScopeKey;
      act(() => mounted!.update(React.createElement(Harness)));
    },
    restore(position: NonNullable<typeof viewState>) {
      viewState = position;
      act(() => mounted!.update(React.createElement(Harness)));
    },
    resize: () => { act(() => resize()); settle(); },
    scrollbarDown: () => dispatchScrollbarEvent("pointerdown", scroller, 1),
    scrollbarUp: () => dispatchScrollbarEvent("pointerup", scroller, 0),
    scrollbarMouseDown: () => dispatchScrollbarEvent("mousedown", scroller, 1),
    scrollbarMouseUp: () => dispatchScrollbarEvent("mouseup", scroller, 0),
    childInput: (type: "pointerdown" | "mousedown" | "pointermove" | "mousemove", target: "button" | "icon") =>
      dispatchScrollbarEvent(type, target === "button" ? childButton : childIcon, 1),
    userScroll(top: number, input?: "wheel" | "keydown" | "touchstart") {
      act(() => {
        if (input) for (const listener of [...scroller.listeners.get(input) ?? []]) listener({ deltaY: -10 });
        scroller.scrollTop = top;
        scroller.onscroll?.();
      });
    },
  };
}

describe("Turn 虚拟列表滚动跟随", () => {
  test("用户离开底部后终态更新不得重新抢占滚动位置", () => {
    expect(turnFollowOutput(true, true)).toBe("auto");
    expect(turnFollowOutput(false, true)).toBe(false);
    expect(turnFollowOutput(true, false)).toBe(false);
  });

  test("已排队的自动跟随帧在用户上滚后失效", () => {
    let pendingFrame: FrameRequestCallback | null = null;
    let followsLatest = true;
    let scrollCount = 0;
    requestTurnFollowLatestFrame(
      (callback) => {
        pendingFrame = callback;
        return 1;
      },
      () => followsLatest,
      () => {
        scrollCount += 1;
      },
    );

    followsLatest = false;
    (pendingFrame as FrameRequestCallback | null)?.(16);

    expect(scrollCount).toBe(0);
  });

});

describe("正式轮次定位与位置恢复", () => {
  test.each(["pointerdown", "mousedown", "pointermove", "mousemove"] as const)(
    "滚动区右侧按钮及图标在 %s 时保留定位锚点且不冻结前插",
    (type) => {
      const harness = navigationHarness([turn("turn-1", 1), turn("turn-2", 2), turn("turn-3", 3)]);
      act(() => harness.state().navigateToTurn("turn-2"));
      harness.settle();
      expect(harness.state().navigation?.phase).toBe("located");
      const anchorPosition = harness.positions[harness.positions.length - 1];
      const firstItemIndex = harness.state().firstItemIndex;

      harness.childInput(type, "button");
      harness.childInput(type, "icon");
      expect(harness.state().navigation?.phase).toBe("located");
      expect(harness.positions[harness.positions.length - 1]).toEqual(anchorPosition);
      harness.update([turn("turn-0", 0), turn("turn-1", 1), turn("turn-2", 2), turn("turn-3", 3)]);
      expect(harness.state().firstItemIndex).toBe(firstItemIndex - 1);
    },
  );

  test("从最新开始拖原生滚动条立即退出跟随，后继内容不抢回位置", () => {
    const harness = navigationHarness([turn("turn-1", 1), turn("turn-2", 2), turn("turn-3", 3)]);
    harness.settle();
    harness.scrolls.length = 0;
    harness.scrollbarDown();
    act(() => harness.state().handleAtBottomChange(true));
    act(() => { harness.scroller.scrollTop = 40; harness.scroller.onscroll?.(); });
    harness.scrollbarUp();
    expect(harness.state().followOutput(true)).toBe(false);
    expect(harness.state().showJumpToLatest).toBe(true);
    harness.update([turn("turn-1", 1), turn("turn-2", 2), turn("turn-3", 3), turn("turn-4", 4)]);
    harness.settle();
    expect(harness.scrolls).toEqual([]);
  });

  test("原生滚动条按下后未移动即释放，保留原本的最新跟随意图", () => {
    const harness = navigationHarness([turn("turn-1", 1), turn("turn-2", 2), turn("turn-3", 3)]);
    harness.settle();
    const top = harness.scroller.scrollTop;
    harness.scrolls.length = 0;
    harness.scrollbarDown();
    expect(harness.state().followOutput(true)).toBe(false);
    harness.scrollbarUp();
    expect(harness.state().followOutput(true)).toBe("auto");
    expect(harness.state().showJumpToLatest).toBe(false);
    expect(harness.scroller.scrollTop).toBe(top);
    expect(harness.scrolls).toEqual([]);
  });

  test("仅鼠标事件且target为滚动容器时仍按原生滚动条拖动", () => {
    const harness = navigationHarness([turn("turn-1", 1), turn("turn-2", 2), turn("turn-3", 3)]);
    harness.settle();
    harness.scrollbarMouseDown();
    expect(harness.state().followOutput(true)).toBe(false);
    harness.userScroll(40);
    harness.scrollbarMouseUp();
    expect(harness.state().followOutput(true)).toBe(false);
    expect(harness.state().showJumpToLatest).toBe(true);
    expect(harness.scroller.scrollTop).toBe(40);
  });

  test.each(["wheel", "keydown", "touchstart", undefined] as const)("拖动释放后 %s 改变位置，后继流式布局不得重放旧位置", (input) => {
    const harness = navigationHarness([turn("turn-1", 1), turn("turn-2", 2), turn("turn-3", 3)]);
    harness.settle();
    harness.scrolls.length = 0;
    harness.scrollbarDown();
    harness.userScroll(60);
    harness.scrollbarUp();
    harness.userScroll(25, input);
    const streamingTurn = turn("turn-3", 3);
    streamingTurn.conversation.turnRevision = 2;
    harness.update([turn("turn-1", 1), turn("turn-2", 2), streamingTurn]);
    harness.settle();
    expect(harness.scroller.scrollTop).toBe(25);
    harness.update([turn("turn-1", 1), turn("turn-2", 2), turn("turn-3", 3), turn("turn-4", 4)]);
    harness.settle();
    expect(harness.scroller.scrollTop).toBe(25);
    expect(harness.scrolls).toEqual([]);
    expect(harness.state().followOutput(true)).toBe(false);
  });

  test("拖动期间前插冻结索引，释放时只补偿一次，后继手动滚动不受影响", () => {
    const harness = navigationHarness([turn("turn-2", 2), turn("turn-3", 3), turn("turn-4", 4)]);
    harness.settle();
    const originalIndex = harness.state().firstItemIndex;
    harness.scrollbarDown();
    harness.userScroll(60);
    harness.update([turn("turn-1", 1), turn("turn-2", 2), turn("turn-3", 3), turn("turn-4", 4)]);
    expect(harness.state().firstItemIndex).toBe(originalIndex);
    harness.scrollbarUp();
    expect(harness.state().firstItemIndex).toBe(originalIndex - 1);
    harness.scroller.scrollTop = 210;
    harness.flush();
    expect(harness.scroller.scrollTop).toBe(60);
    harness.userScroll(25);
    harness.update([turn("turn-1", 1), turn("turn-2", 2), turn("turn-3", 3), turn("turn-4", 4), turn("turn-5", 5)]);
    harness.settle();
    expect(harness.scroller.scrollTop).toBe(25);
    expect(harness.state().followOutput(true)).toBe(false);
  });

  test.each(["keydown", "layout"] as const)("前插释放补偿尚未执行时，新的 %s 意图会取消旧帧", (next) => {
    const harness = navigationHarness([turn("turn-2", 2), turn("turn-3", 3), turn("turn-4", 4)]);
    harness.settle();
    harness.scrollbarDown();
    harness.userScroll(60);
    harness.update([turn("turn-1", 1), turn("turn-2", 2), turn("turn-3", 3), turn("turn-4", 4)]);
    harness.scrollbarUp();
    harness.userScroll(25, next === "keydown" ? "keydown" : undefined);
    if (next === "layout") harness.update([turn("turn-1", 1), turn("turn-2", 2), turn("turn-3", 3), turn("turn-4", 4), turn("turn-5", 5)]);
    harness.settle();
    expect(harness.scroller.scrollTop).toBe(25);
  });

  test("自动跟随之后才到达的保存位置仍会恢复，用户跳最新则覆盖旧位置", () => {
    const harness = navigationHarness([turn("turn-1", 1), turn("turn-2", 2), turn("turn-3", 3)]);
    harness.settle();
    expect(harness.scrolls[harness.scrolls.length - 1]?.index).toBe("LAST");
    harness.restore({ turn_anchor: "turn-2", scroll_offset: 20, follow_latest: false });
    harness.settle();
    expect(harness.scroller.scrollTop).toBe(170);
    expect(harness.state().navigation?.phase).toBe("located");
    act(() => harness.state().scrollToLatest());
    harness.restore({ turn_anchor: "turn-1", scroll_offset: 0, follow_latest: false });
    harness.settle();
    expect(harness.state().navigation).toBeNull();
    expect(harness.state().followOutput(true)).toBe("auto");
  });

  test("重复跳同一稳定 Turn，前插与高度变化仍保持目标且新输出不抢位置", () => {
    const harness = navigationHarness([turn("turn-2", 2), turn("turn-3", 3), turn("turn-4", 4)]);
    act(() => harness.state().navigateToTurn("turn-3"));
    harness.settle();
    expect(harness.scrolls[0].index).toBe(1);
    expect(harness.state().navigation?.message).toBe("已定位第 3 轮");
    expect(harness.state().followOutput(true)).toBe(false);
    act(() => harness.state().handleAtBottomChange(true));
    expect(harness.state().followOutput(true)).toBe(false);
    act(() => harness.state().navigateToTurn("turn-3"));
    harness.settle();
    expect(harness.positions).toHaveLength(2);
    harness.update([turn("turn-1", 1), turn("turn-2", 2), turn("turn-3", 3), turn("turn-4", 4), turn("turn-5", 5)]);
    harness.settle();
    expect(harness.scroller.scrollTop).toBe(300);
    harness.heights.set("turn-1", 250);
    harness.heights.set("turn-3", 300);
    harness.resize();
    expect(harness.scroller.scrollTop).toBe(400);
    expect(harness.state().navigation?.turnId).toBe("turn-3");
    act(() => harness.state().scrollToLatest());
    expect(harness.scrolls[harness.scrolls.length - 1]?.index).toBe("LAST");
    expect(harness.state().followOutput(true)).toBe("auto");
    expect(harness.state().navigation).toBeNull();
  });

  test("未加载轮次只请求 around，加载后按目标当前索引定位", async () => {
    const pending = deferred();
    const calls: string[] = [];
    const harness = navigationHarness([turn("turn-8", 8)], async (id) => { calls.push(id); await pending.promise; });
    act(() => harness.state().navigateToTurn("turn-2"));
    expect(harness.state().navigation?.phase).toBe("loading");
    harness.update([turn("turn-1", 1), turn("turn-2", 2), turn("turn-3", 3), turn("turn-8", 8)]);
    await act(async () => { pending.resolve(); await pending.promise; });
    harness.settle();
    expect(calls).toEqual(["turn-2"]);
    expect(harness.scrolls[harness.scrolls.length - 1]?.index).toBe(1);
    expect(harness.state().navigation?.turnId).toBe("turn-2");
  });

  test("目标异步挂载后由真实render回调继续定位，无须变更items或navigation", () => {
    const harness = navigationHarness([turn("turn-1", 1), turn("turn-2", 2), turn("turn-3", 3)], undefined, false);
    act(() => harness.state().navigateToTurn("turn-2"));
    harness.flush();
    expect(harness.state().navigation?.phase).toBe("positioning");
    expect(harness.scrolls).toEqual([{ index: 1, align: "start", behavior: "auto", offset: 0 }]);
    const positioning = harness.state().navigation;
    harness.flush();
    expect(harness.scrolls).toHaveLength(1);
    expect(harness.state().navigation).toBe(positioning);
    harness.scroller.mountedId = "turn-2";
    act(() => harness.state().scheduleAnchorAlignment());
    harness.flush();
    expect(harness.state().navigation?.phase).toBe("located");
    expect(harness.state().navigation?.turnId).toBe("turn-2");
    expect(harness.positions).toEqual([{ turn_anchor: "turn-2", scroll_offset: 0, follow_latest: false }]);
    expect(harness.scrolls).toHaveLength(1);
  });

  test("异步render回调在用户取消后不恢复旧定位", () => {
    const harness = navigationHarness([turn("turn-1", 1), turn("turn-2", 2)], undefined, false);
    act(() => harness.state().navigateToTurn("turn-1"));
    harness.flush();
    act(() => harness.state().scrollToLatest());
    harness.scroller.mountedId = "turn-1";
    const top = harness.scroller.scrollTop;
    act(() => harness.state().scheduleAnchorAlignment());
    harness.flush();
    expect(harness.state().navigation).toBeNull();
    expect(harness.positions).toEqual([]);
    expect(harness.scroller.scrollTop).toBe(top);
    expect(harness.state().followOutput(true)).toBe("auto");
  });

  test("切换线程后迟到加载与定位帧丢弃，不污染新 owner", async () => {
    const pending = deferred();
    let signal: AbortSignal | undefined;
    const harness = navigationHarness([turn("turn-8", 8)], async (_id, requestSignal) => { signal = requestSignal; await pending.promise; });
    act(() => harness.state().navigateToTurn("turn-2"));
    harness.update([turn("turn-new", 1)], "workspace/session/other-thread");
    expect(signal?.aborted).toBe(true);
    await act(async () => { pending.resolve(); await pending.promise; });
    harness.settle();
    expect(harness.state().navigation).toBeNull();
    expect(harness.positions).toEqual([]);
    expect(harness.scrolls.every((location) => location.index === "LAST")).toBe(true);
  });

  test("pending和internal不伪装为Turn，缺失与后端错误可原ID重试", async () => {
    let failed = false;
    const internal: ChatTranscriptItem = { kind: "internal_execution", key: "internal:item-report", executionId: "execution-report",
      displayPart: { kind: "generated_session_result", text: "内部执行结果", item_id: "item-report", item_sequence: 2,
        logical_item_ordinal: 2, created_at: "2026-10-15T00:00:00Z", provenance: {} } };
    const harness = navigationHarness([turn("pending", 1, true), internal], async () => { if (failed) throw new Error("历史索引损坏"); });
    await act(async () => harness.state().navigateToTurn("pending"));
    harness.settle();
    expect(harness.state().navigation?.phase).toBe("error");
    expect(harness.state().navigation?.message).toContain("未找到指定轮次");
    await act(async () => harness.state().navigateToTurn("internal:item-report"));
    harness.settle();
    expect(harness.state().navigation?.phase).toBe("error");
    expect(harness.state().navigation?.message).toContain("未找到指定轮次");
    failed = true;
    await act(async () => harness.state().navigateToTurn("pending"));
    expect(harness.state().navigation?.message).toContain("历史索引损坏");
    expect(harness.state().navigation?.turnId).toBe("pending");
    expect(harness.positions).toEqual([]);
  });

  test("保存的Turn与裁切偏移复用定位", () => {
    const harness = navigationHarness([turn("turn-1", 1), turn("turn-2", 2), turn("turn-3", 3)]);
    harness.restore({ turn_anchor: "turn-2", scroll_offset: 35, follow_latest: false });
    harness.settle();
    expect(harness.scroller.scrollTop).toBe(185);
    expect(harness.positions[harness.positions.length - 1]).toEqual({ turn_anchor: "turn-2", scroll_offset: 35, follow_latest: false });
  });

  test("当前 owner 的保存锚点失效时明确提示并回到最新", async () => {
    const harness = navigationHarness([turn("turn-current", 1)]);
    harness.restore({ turn_anchor: "turn-missing", scroll_offset: 35, follow_latest: false });
    await act(async () => {});
    harness.settle();
    expect(harness.state().navigation?.phase).toBe("error");
    expect(harness.statuses[harness.statuses.length - 1]).toContain("历史位置已失效");
    expect(harness.state().followOutput(true)).toBe("auto");
  });
});
