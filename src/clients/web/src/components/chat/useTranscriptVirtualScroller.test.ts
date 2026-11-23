import { nodeKey, encodeNode, partNode, contextNode } from "../../state/timeline/transcriptNodes";
import { findTranscriptNode } from "./transcriptGeometry";
import { HttpRequestError } from "../../api/http";
import type { TranscriptViewportPosition } from "./useTranscriptNavigation";
import { afterEach, describe, expect, mock, test } from "bun:test";
import React from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import type { VirtuosoHandle } from "react-virtuoso";
import { setLocaleResources } from "../../../../shared/core/i18n/index.js";
import english from "../../../../shared/core/i18n/messages/en.json";
import chinese from "../../../../shared/core/i18n/messages/zh-cn.json";
import type { ConversationView } from "../../types/frontend";
import type { ChatTranscriptItem } from "./transcriptItems";
import type { ThreadItemLocationDTO } from "../../types/protocol_generated/boxteam/workspace/v2/public";
import type { LoadAroundTurn } from "../../api/session/sessionTurnHistory";
import { restoreGlobalDescriptor } from "../../tests/testGlobals";
import {
  requestTurnFollowLatestFrame,
  turnFollowOutput,
  useTranscriptVirtualScroller,
  type TranscriptViewportController,
} from "./useTranscriptVirtualScroller";

const windowDescriptor = Object.getOwnPropertyDescriptor(globalThis, "window");
const elementDescriptor = Object.getOwnPropertyDescriptor(globalThis, "HTMLElement");
const mutationDescriptor = Object.getOwnPropertyDescriptor(globalThis, "MutationObserver");
const rafDescriptor = Object.getOwnPropertyDescriptor(globalThis, "requestAnimationFrame");
const cancelRafDescriptor = Object.getOwnPropertyDescriptor(globalThis, "cancelAnimationFrame");
const observerDescriptor = Object.getOwnPropertyDescriptor(globalThis, "ResizeObserver");
let mounted: ReactTestRenderer | null = null;

afterEach(() => {
  if (mounted) act(() => mounted?.unmount());
  mounted = null;
  restoreGlobalDescriptor("window", windowDescriptor);
  restoreGlobalDescriptor("HTMLElement", elementDescriptor);
  restoreGlobalDescriptor("ResizeObserver", observerDescriptor);
  restoreGlobalDescriptor("MutationObserver", mutationDescriptor);
  restoreGlobalDescriptor("requestAnimationFrame", rafDescriptor);
  restoreGlobalDescriptor("cancelAnimationFrame", cancelRafDescriptor);
  setLocaleResources("zh-cn", chinese);
});

setLocaleResources("zh-cn", chinese);

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

function navigationHarness(initial: ChatTranscriptItem[], loader: LoadAroundTurn = async () => {}, mountOnScroll = true,
  messageTarget?: { dataset: { messageItemId?: string; toolResultItemId?: string; chatItemId?: string; responseSources?: string }; animate: HTMLElement["animate"]; scrollIntoView: (options: ScrollIntoViewOptions) => void; getBoundingClientRect: () => { top: number; bottom: number }; closest?: () => HTMLElement | null },
  historyLoaders: { hasOlderMessages?: boolean; hasNewerMessages?: boolean; loadOlderMessages?: () => Promise<void>; loadNewerMessages?: () => Promise<void>; loadLatestMessages?: () => Promise<void>; restoredPosition?: TranscriptViewportPosition } = {}) {
  let items = initial;
  let scopeKey = "workspace/session/thread";
  let viewState: TranscriptViewportPosition | null = null;
  let current!: ReturnType<typeof useTranscriptVirtualScroller>;
  let viewport: TranscriptViewportController | undefined;
  let restoredPosition = historyLoaders.restoredPosition;
  const onViewportReady = (controller: TranscriptViewportController | undefined) => {
    viewport = controller;
    if (controller && restoredPosition) {
      const position = restoredPosition; restoredPosition = undefined;
      controller.navigate(position, true);
    }
  };
  let nextFrame = 0;
  const frames = new Map<number, FrameRequestCallback>();
  const heights = new Map(initial.map((item) => [item.key, 150]));
  const scrolls: Array<{ index: number | "LAST"; align?: string; behavior?: string; offset?: number }> = [];
  const positions: TranscriptViewportPosition[] = [];
  const statuses: string[] = [];
  const turnAnimation = mock(() => ({} as Animation));
  let resize = () => {};
  let measuredNodes: HTMLElement[] | undefined;
  const windowListeners = new Map<string, (event: { type: string; button: number; buttons: number; clientX: number; clientY: number; target: Scroller }) => void>();
  class Scroller {
    scrollTop = 0;
    clientHeight = 200;
    mountedId: string | null = null;
    parentElement: Scroller | null = null;
    onwheel: unknown;
    ontouchstart: (() => void) | null = null;
    onkeydown: ((event: { key: string; defaultPrevented: boolean }) => void) | null = null;
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
    querySelectorAll(): HTMLElement[] {
      if (measuredNodes) return measuredNodes;
      const itemIndex = items.findIndex((item) => item.key === this.mountedId);
      if (itemIndex < 0) return [];
      return [{ dataset: {transcriptNode: encodeNode(nodeKey({kind:"turn",id:this.mountedId!},"header"))}, animate: turnAnimation, getAttribute: () => null, querySelectorAll: () => [], getBoundingClientRect: () => {
        const top = items.slice(0, itemIndex).reduce((sum, item) => sum + (heights.get(item.key) ?? 150), 0) - this.scrollTop;
        return { top, bottom: top + (heights.get(items[itemIndex].key) ?? 150), height: heights.get(items[itemIndex].key) ?? 150 };
      } } as unknown as HTMLElement];
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
  Object.defineProperty(globalThis,"MutationObserver",{configurable:true,value:class {observe(){} disconnect(){}}});
  Object.defineProperty(globalThis,"requestAnimationFrame",{configurable:true,value:window.requestAnimationFrame});
  Object.defineProperty(globalThis,"cancelAnimationFrame",{configurable:true,value:window.cancelAnimationFrame});
  function Harness() {
    current = useTranscriptVirtualScroller({
      items, scopeKey, onLoadAroundTurn: loader, onLoadNewerMessages: historyLoaders.loadNewerMessages ?? (async () => {}),
      onLoadLatestMessages: historyLoaders.loadLatestMessages ?? (async () => {}),
      onLoadOlderMessages: historyLoaders.loadOlderMessages ?? (async () => {}),
      hasNewerMessages: historyLoaders.hasNewerMessages ?? false, hasOlderMessages: historyLoaders.hasOlderMessages ?? false,
      loadingNewerMessages: false, loadingOlderMessages: false,
      historyLoading: false, viewState, onViewStateChange: (position) => positions.push(position),
      onViewStateRestoreStatus: (message) => statuses.push(message),
      onViewportReady,
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
  const settle = () => {
    flush(); act(() => current.scheduleAnchorAlignment()); flush();
    // 模拟 Virtuoso 在滚动／布局完成后发布的实际到底状态。
    act(() => current.handleAtBottomChange(scroller.scrollHeight - scroller.clientHeight - scroller.scrollTop <= 4));
  };
  return {
    state: () => current, viewport: () => viewport!, scroller, scrolls, positions, statuses, heights, settle, flush, turnAnimation,
    setNodes(nodes: HTMLElement[]) { measuredNodes = nodes; },
    update(nextItems = items, nextScopeKey = scopeKey) {
      items = nextItems; scopeKey = nextScopeKey;
      act(() => mounted!.update(React.createElement(Harness)));
    },
    restore(position: NonNullable<typeof viewState>) {
      viewState = position;
      act(() => mounted!.update(React.createElement(Harness)));
    },
    resize: () => { act(() => resize()); settle(); },
    toggleTurn(id: string) {
      scroller.mountedId = id;
      const heading = Object.assign(new Scroller(), {
        isConnected: true,
        closest: (selector: string) => selector === "button[aria-expanded], summary" ? heading : scroller.querySelectorAll()[0],
        getBoundingClientRect: () => {
          const bounds = scroller.querySelectorAll()[0].getBoundingClientRect();
          return { ...scroller.getBoundingClientRect(), top: bounds.top + 20, bottom: bounds.top + 44 };
        },
      });
      act(() => current.handleTranscriptClickCapture({ target: heading } as unknown as React.MouseEvent<HTMLElement>));
      return heading;
    },
    scrollbarDown: () => dispatchScrollbarEvent("pointerdown", scroller, 1),
    scrollbarUp: () => dispatchScrollbarEvent("pointerup", scroller, 0),
    scrollbarMouseDown: () => dispatchScrollbarEvent("mousedown", scroller, 1),
    scrollbarMouseUp: () => dispatchScrollbarEvent("mouseup", scroller, 0),
    childInput: (type: "pointerdown" | "mousedown" | "pointermove" | "mousemove", target: "button" | "icon") =>
      dispatchScrollbarEvent(type, target === "button" ? childButton : childIcon, 1),
    userScroll(top: number, input?: "wheel" | "keydown" | "touchstart") {
      act(() => {
        if (input === "wheel") for (const listener of [...scroller.listeners.get(input) ?? []]) listener({ deltaY: -10 });
        if(input === "keydown") scroller.onkeydown?.({key:"PageDown",defaultPrevented:false});
        if(input === "touchstart") scroller.ontouchstart?.();
        scroller.scrollTop = top;
        scroller.onscroll?.();
        current.handleAtBottomChange(scroller.scrollHeight - scroller.clientHeight - top <= 4);
      });
    },
  };
}


function position(id: string, offset = 0): TranscriptViewportPosition {
  return {reading_anchor:{node_key:nodeKey({kind:"turn",id},"header"),
    content_position:{block:0,offset_px:offset},viewport_offset_px:0},follow_latest:false};
}

describe("语义节点导航与虚拟列表", () => {
  test("同一 Item 的折叠祖先保留 Part 身份，不命中兄弟部位或同名其它来源", () => {
    const part0 = nodeKey({kind:"item",id:"shared",part:0});
    const part1 = nodeKey({kind:"item",id:"shared",part:1});
    const header = (key: typeof part0) => ({dataset:{transcriptNode:encodeNode({...key,slot:"header"}),
      transcriptMembers:JSON.stringify([key.source_ref])},getAttribute:()=>"false",
      getBoundingClientRect:()=>({height:20})}) as unknown as HTMLElement;
    const first = header(part0), second = header(part1);
    const scroller = {querySelectorAll:()=>[first,second]} as unknown as HTMLElement;
    expect(findTranscriptNode(scroller,part0)).toEqual({element:first,ancestor:true});
    expect(findTranscriptNode(scroller,part1)).toEqual({element:second,ancestor:true});
    expect(findTranscriptNode(scroller,nodeKey({kind:"item",id:"shared",part:9}))).toBeUndefined();
    expect(findTranscriptNode(scroller,nodeKey({kind:"context",id:"shared"}))).toBeUndefined();
  });

  test("上下文和临时流节点的祖先也核对真实过程及 producer 坐标", () => {
    const context = nodeKey({kind:"context",id:"shared",process:{kind:"execution",id:"exec-1"}});
    const stream = nodeKey({kind:"stream",id:"temporary",model:"model-1",block:0,part_kind:"text"});
    const element = {dataset:{transcriptNode:encodeNode(nodeKey({kind:"execution",id:"exec-1"},"header")),
      transcriptMembers:JSON.stringify([context.source_ref,stream.source_ref])},getAttribute:()=>"false",
      getBoundingClientRect:()=>({height:20})} as unknown as HTMLElement;
    const scroller = {querySelectorAll:()=>[element]} as unknown as HTMLElement;
    expect(findTranscriptNode(scroller,context)?.element).toBe(element);
    expect(findTranscriptNode(scroller,stream)?.element).toBe(element);
    expect(findTranscriptNode(scroller,nodeKey({kind:"context",id:"shared",process:{kind:"turn",id:"exec-1"}}))).toBeUndefined();
    expect(findTranscriptNode(scroller,nodeKey({kind:"stream",id:"temporary",model:"model-2",block:0,part_kind:"text"}))).toBeUndefined();
  });

  test.each([true,false])("流式正文结算 final 只按唯一真实 producer 和 block 保位，匹配=%s", sameProducer => {
    const item = turn("one",1);
    const stream = {part_id:"temporary",kind:"text" as const,projection:"detail" as const,source:{message_sequence:1,model_call_id:"model-1",content_block_index:0}};
    item.conversation.responseParts = [stream];
    const h = navigationHarness([item,turn("two",2),turn("three",3)]);
    const streamKey = partNode(stream);
    const element = (key: typeof streamKey, parent?: typeof streamKey, top = 30) => ({dataset:{transcriptNode:encodeNode(key),
      ...(parent ? {transcriptParent:encodeNode(parent)} : {})},getAttribute:()=>null,querySelectorAll:()=>[],
      getBoundingClientRect:()=>({top:top-h.scroller.scrollTop,bottom:top+50-h.scroller.scrollTop,height:50})}) as unknown as HTMLElement;
    const process = nodeKey({kind:"turn",id:"one"},"header");
    h.setNodes([element(process),element(streamKey,process)]);
    h.userScroll(30,"wheel");
    h.toggleTurn("one");
    const canonical = {...stream,part_id:"canonical",kind:"final_text" as const,
      source:{...stream.source,item_id:"final-item",part_ordinal:2,model_call_id:sameProducer ? "model-1" : "model-2"}};
    h.setNodes([element(process),element(partNode(canonical),undefined,70)]);
    h.update([{...item,conversation:{...item.conversation,responseParts:[canonical]}},turn("two",2),turn("three",3)]);
    h.settle();
    // 对 final 正文后续增高继续保位，无法关联则只落到过程标题并明确告知。
    expect(h.statuses).toHaveLength(sameProducer ? 0 : 1);
    if (!sameProducer) expect(h.statuses[0]).toContain("位置");
    expect(h.scroller.scrollTop).toBe(sameProducer ? 70 : 30);
    act(() => h.viewport().cancel());
  });
  test("Workbench 首次取得控制器时恢复的位置不被挂载初始化取消", () => {
    const h = navigationHarness([turn("one",1),turn("two",2),turn("three",3)], undefined, true, undefined,
      {restoredPosition:position("two",35)});
    h.settle();
    expect(h.scroller.scrollTop).toBe(185);
    expect(h.state().followOutput(true)).toBe(false);
    expect(h.positions).toEqual([]);
  });
  test("只有跟随意图及实际到底同时成立时自动跟随", () => {
    expect(turnFollowOutput(true,true)).toBe("auto");
    expect(turnFollowOutput(true,false)).toBe(false);
    expect(turnFollowOutput(false,true)).toBe(false);
    let follow = true; let calls = 0; let callback!: FrameRequestCallback;
    requestTurnFollowLatestFrame(cb => {callback=cb; return 1;},()=>follow,()=>calls++);
    follow=false; callback(0); expect(calls).toBe(0);
  });
  test("手动展开不改变跟随意图，返回同一 owner 仍恢复其阅读记录", () => {
    const items = [turn("one",1),turn("two",2),turn("three",3)];
    const h = navigationHarness(items);
    h.toggleTurn("three");
    expect(h.state().followOutput(true)).toBe("auto");
    h.restore(position("two",35)); h.settle();
    h.update(items,"another-thread"); h.settle();
    h.update(items,"workspace/session/thread"); h.settle();
    expect(h.scroller.scrollTop).toBe(185);
    expect(h.positions).toEqual([]);
  });
  test("单屏内容不因退出跟随而显示跳最新", () => {
    const h=navigationHarness([turn("only",1)]);
    h.restore(position("only")); h.settle();
    expect(h.state().showJumpToLatest).toBe(false);
    expect(h.state().followOutput(true)).toBe(false);
  });
  test("明确定位持有节点，前插与上方增高不改变屏幕位置", () => {
    const h=navigationHarness([turn("one",1),turn("two",2),turn("three",3)]);
    act(()=>h.state().navigateToTurn("two")); h.settle();
    expect(h.state().navigation?.phase).toBe("located");
    expect(h.scroller.scrollTop).toBe(150);
    const key=h.positions.at(-1)?.reading_anchor?.node_key;
    h.update([turn("zero",0),turn("one",1),turn("two",2),turn("three",3)]); h.settle();
    expect(h.scroller.scrollTop).toBe(300);
    h.heights.set("one",250); h.resize();
    expect(h.scroller.scrollTop).toBe(400);
    expect(h.positions.at(-1)?.reading_anchor?.node_key).toEqual(key);
    expect(h.state().followOutput(true)).toBe(false);
  });
  test("手动折叠结束显式定位，但保留点击捕获的布局锚点", () => {
    const h = navigationHarness([turn("one",1),turn("two",2),turn("three",3)]);
    act(() => h.state().navigateToTurn("two")); h.settle();
    h.toggleTurn("two");
    act(() => h.state().cancelMessageNavigation());
    expect(h.state().viewportAnchored).toBe(true);
    h.heights.set("one",250); h.resize();
    expect(h.scroller.scrollTop).toBe(250);
    expect(h.state().navigation).toBeNull();
  });
  test("恢复节点内偏移，布局变化不写持久默认位置", () => {
    const h=navigationHarness([turn("one",1),turn("two",2),turn("three",3)]);
    h.restore(position("two",35)); h.settle();
    expect(h.scroller.scrollTop).toBe(185);
    expect(h.positions).toEqual([]);
    h.heights.set("one",180);h.resize();
    expect(h.scroller.scrollTop).toBe(215);
    expect(h.positions).toEqual([]);
  });
  test("虚拟列表临时高度不足时钳制，真实尺寸提交后仍持有原阅读点", () => {
    const h = navigationHarness([turn("one",1),turn("two",2),turn("three",3)]);
    h.scroller.clientHeight = 400;
    h.restore(position("two")); h.settle();
    expect(h.scroller.scrollTop).toBe(50);
    h.heights.set("three",400); h.resize();
    expect(h.scroller.scrollTop).toBe(150);
    expect(h.positions).toEqual([]);
  });
  test("加载失败不跳最新，不把请求错误解释成删除", async () => {
    const h=navigationHarness([turn("current",9)],async()=>{throw new Error("索引损坏 request_id=req-7");});
    h.restore(position("missing"));
    await act(async()=>{});h.settle();
    expect(h.state().navigation?.phase).toBe("error");
    expect(h.statuses.at(-1)).toContain("req-7");
    expect(h.state().followOutput(true)).toBe(false);
    expect(h.positions).toEqual([]);
  });
  test.each([true, false])("只有来源明确的 Item 不可用才结束普通恢复，带来源=%s", async verified => {
    const error = new HttpRequestError(404, "Not Found", "Item 不在当前视图", "/history", "req-missing",
      verified ? JSON.stringify({source:{component:"workspace_backend"}, stage:"history_locate", error_type:"HistoryItemUnavailableError",
        target:{item_id:"missing"}}) : null);
    const h = navigationHarness([turn("current",9)],async()=>{throw error;});
    h.restore({reading_anchor:{node_key:nodeKey({kind:"item",id:"missing",part:0}),content_position:{block:0,offset_px:0},viewport_offset_px:0},follow_latest:false});
    await act(async()=>{});h.settle();
    expect(h.state().navigation?.phase).toBe(verified ? "unavailable" : "error");
    expect(h.statuses.at(-1)).toContain("req-missing");
    expect(h.positions).toEqual([]);
    expect(h.scrolls).toEqual([]);
  });
  test("未挂载不会解释成删除，挂载回调继续定位", () => {
    const h=navigationHarness([turn("one",1),turn("two",2),turn("three",3)],undefined,false);
    act(()=>h.state().navigateToTurn("two"));h.flush();
    expect(h.state().navigation?.phase).not.toBe("error");
    h.scroller.mountedId="two";act(()=>h.state().scheduleAnchorAlignment());h.flush();
    expect(h.state().navigation?.phase).toBe("located");
  });
  test("工具 Item 定位使用参数正文的真实 Part，不能等待不存在的 body", async () => {
    const part = {part_id:"tool-part",kind:"tool_call" as const,source:{message_sequence:1,item_id:"tool-item",part_ordinal:3}};
    const location: ThreadItemLocationDTO = {item_id:"tool-item",semantic_kind:"tool_call",turn_id:"one",execution_id:"exec",content_hash:"hash",byte_length:20};
    const h = navigationHarness([turn("one",1)],async () => location);
    const animate = mock(() => ({} as Animation));
    const key = partNode(part,"tool_input");
    h.setNodes([{dataset:{transcriptNode:encodeNode(key)},getAttribute:()=>null,querySelectorAll:()=>[],animate,
      getBoundingClientRect:()=>({top:20,bottom:60,height:40})} as unknown as HTMLElement]);
    act(() => h.state().navigateToTurn("one",false,undefined,undefined,location));
    await act(async()=>{});h.settle();
    expect(h.state().navigation?.phase).toBe("located");
    expect(h.positions.at(-1)?.reading_anchor?.node_key).toEqual(key);
    expect(animate).toHaveBeenCalledTimes(1);
  });
  test("无 Turn 的独立回报定位使用真实 notice 部位", async () => {
    const location: ThreadItemLocationDTO = {item_id:"notice-item",semantic_kind:"runtime_notice",execution_id:"exec",content_hash:"hash",byte_length:20};
    const h = navigationHarness([turn("one",1)],async () => location);
    const key = nodeKey({kind:"item",id:"notice-item",part:0},"notice");
    h.setNodes([{dataset:{transcriptNode:encodeNode(key)},getAttribute:()=>null,querySelectorAll:()=>[],
      getBoundingClientRect:()=>({top:20,bottom:60,height:40})} as unknown as HTMLElement]);
    act(() => h.state().navigateToTurn("",false,undefined,undefined,location));
    await act(async()=>{}); h.settle();
    expect(h.state().navigation?.phase).toBe("located");
    expect(h.positions.at(-1)?.reading_anchor?.node_key).toEqual(key);
  });
  test.each(["tail", "notice", "header"] as const)("同 execution 的 %s 不替代早期分段，按真实段首 Item 恢复", async initialKind => {
    const segment = (id: string): Extract<ChatTranscriptItem, {kind: "execution_response"}> => ({
      kind: "execution_response", key: `response:${id}`, executionId: "exec", itemId: id, bodyEvicted: false,
      parts: [{part_id:`part:${id}`,kind:"reasoning",projection:"detail",text:id,
        source:{message_sequence:1,item_id:id,part_ordinal:0,execution_id:"exec"}}],
    });
    const tail = segment("tail-item");
    const notice: ChatTranscriptItem = {kind:"internal_execution",key:"notice",executionId:"exec",displayPart:{
      kind:"generated_session_result",text:"回报",item_id:"notice-item",item_sequence:100,logical_item_ordinal:100,created_at:"2026-11-23T00:00:00Z",provenance:{},
    }};
    const pending = deferred();
    const loader = mock<LoadAroundTurn>(async () => pending.promise);
    const initial: ChatTranscriptItem = initialKind === "tail" ? tail : initialKind === "notice" ? notice
      : {kind:"execution_header",key:"execution:exec",executionId:"exec",status:"completed_empty",empty:true};
    const h = navigationHarness([initial],loader);
    const key = nodeKey({kind:"execution",id:"exec"},"header:early-item");
    h.restore({reading_anchor:{node_key:key,content_position:{block:0,offset_px:0},viewport_offset_px:0},follow_latest:false});
    h.settle();
    expect(loader).toHaveBeenCalledTimes(1);
    expect(loader.mock.calls[0]).toEqual(["",expect.any(AbortSignal),undefined,undefined,{item_id:"early-item"},undefined,undefined]);
    expect(h.scrolls).toEqual([]);
    h.update([segment("early-item"),tail,notice]);
    await act(async () => { pending.resolve(); });
    h.settle();
    expect(h.scrolls[0]?.index).toBe(0);
    h.setNodes([{dataset:{transcriptNode:encodeNode(key)},getAttribute:()=>null,querySelectorAll:()=>[],
      getBoundingClientRect:()=>({top:20,bottom:60,height:40})} as unknown as HTMLElement]);
    h.settle();
    expect(h.scrolls.every(scroll => scroll.index === 0)).toBe(true);
    expect(h.scroller.scrollTop).toBe(20);
    expect(h.positions).toEqual([]);
    expect(h.statuses).toEqual([]);
    expect(loader).toHaveBeenCalledTimes(1);
  });
  test("原 owner 补读确认 Part 取消后结束恢复，不再等待 DOM 或覆盖原记录", async () => {
    const item = turn("one",1);
    item.conversation.responseParts = [{part_id:"current",kind:"final_text",projection:"detail",text:"保留的正文",source:{message_sequence:1,item_id:"item",part_ordinal:0}}];
    const loader = mock(async()=>{});
    const h = navigationHarness([item],loader);
    h.restore({reading_anchor:{node_key:nodeKey({kind:"item",id:"item",part:9}),content_position:{block:0,offset_px:0},viewport_offset_px:0},follow_latest:false});
    await act(async()=>{});h.settle();
    expect(loader).toHaveBeenCalledTimes(1);
    expect(h.state().navigation?.phase).toBe("unavailable");
    expect(h.positions).toEqual([]);
    h.resize();h.settle();
    expect(h.state().navigation?.phase).toBe("unavailable");
    expect(loader).toHaveBeenCalledTimes(1);
  });
  test("已存在的执行分段标题不因正文被释放而补读隐藏正文", () => {
    const segment: ChatTranscriptItem = {kind:"execution_response",key:"response:segment",executionId:"exec",itemId:"segment",parts:[],bodyEvicted:true};
    const loader = mock<LoadAroundTurn>(async () => {});
    const h = navigationHarness([segment],loader);
    const key = nodeKey({kind:"execution",id:"exec"},"header:segment");
    h.setNodes([{dataset:{transcriptNode:encodeNode(key)},getAttribute:()=>null,querySelectorAll:()=>[],
      getBoundingClientRect:()=>({top:20,bottom:60,height:40})} as unknown as HTMLElement]);
    h.restore({reading_anchor:{node_key:key,content_position:{block:0,offset_px:0},viewport_offset_px:0},follow_latest:false});
    h.settle();
    expect(loader).not.toHaveBeenCalled();
    expect(h.scroller.scrollTop).toBe(0);
    expect(h.positions).toEqual([]);
    expect(h.statuses).toEqual([]);
  });
  test.each(["turn", "execution"] as const)("%s 的页外上下文标记按消费请求定点恢复，不遍历元数据续页", async kind => {
    const notice = {part_id:"late-context",kind:"context_notice" as const,projection:"summary" as const,
      source:{message_sequence:0,...(kind === "turn" ? {turn_id:"one"} : {execution_id:"exec"})},
      context_model_call_id:"late-call",context_anchor:"before_model_call" as const,context_before_item_id:"later-item"};
    const key = contextNode(notice);
    const pending = deferred();
    const loader = mock<LoadAroundTurn>(async () => pending.promise);
    const h = navigationHarness([turn("one",1)],loader);
    h.restore({reading_anchor:{node_key:key,content_position:{block:0,offset_px:0},viewport_offset_px:0},follow_latest:false});
    expect(loader.mock.calls[0]?.[0]).toBe(kind === "turn" ? "one" : "");
    expect(loader.mock.calls[0]?.[5]).toBe(kind === "execution" ? "exec" : undefined);
    expect(loader.mock.calls[0]?.[6]).toEqual({model_call_id:"late-call",anchor:"before_model_call"});
    const target = turn("one",1); target.conversation.responseParts = [notice];
    h.update(kind === "turn" ? [target] : [{kind:"context_event",key:"late-notice",notices:[notice]}]);
    h.setNodes([{dataset:{transcriptNode:encodeNode(key)},getAttribute:()=>null,querySelectorAll:()=>[],
      getBoundingClientRect:()=>({top:20,bottom:60,height:40})} as unknown as HTMLElement]);
    await act(async () => { pending.resolve(); }); h.settle();
    expect(h.state().viewportAnchored).toBe(true);
    expect(h.positions).toEqual([]);expect(h.statuses).toEqual([]);
    expect(loader).toHaveBeenCalledTimes(1);
  });
  test("上下文输出从占位序号结算成真实 Item 后仍是同一展示节点", () => {
    const pending = {part_id:"notice",kind:"context_notice" as const,projection:"summary" as const,
      source:{message_sequence:0,execution_id:"exec"},context_model_call_id:"call",context_anchor:"before_model_call" as const,
      context_anchor_ordinal:12};
    expect(contextNode({...pending,context_before_item_id:"committed-output",context_anchor_ordinal:18})).toEqual(contextNode(pending));
    expect(contextNode({...pending,context_model_call_id:"another-call"})).not.toEqual(contextNode(pending));
  });
  test("上下文 owner 确认标记不可用时结束恢复，保留记录和请求来源", async () => {
    const id = JSON.stringify(["gone-call","before_model_call"]);
    const failure = new HttpRequestError(404,"Not Found","标记不可见","/history","request-context-gone",
      JSON.stringify({source:{component:"workspace_backend"},stage:"history_locate",error_type:"HistoryContextUnavailableError",
        target:{model_call_id:"gone-call",anchor:"before_model_call"}}));
    const h = navigationHarness([turn("one",1)],async () => { throw failure; });
    h.restore({reading_anchor:{node_key:nodeKey({kind:"context",id,process:{kind:"turn",id:"one"}},"marker"),
      content_position:{block:0,offset_px:0},viewport_offset_px:0},follow_latest:false});
    await act(async () => {});h.settle();
    expect(h.state().navigation?.phase).toBe("unavailable");
    expect(h.statuses[0]).toContain("request-context-gone");
    expect(h.positions).toEqual([]);expect(h.scrolls).toEqual([]);
  });
  test("空执行标题已被真实分段替换时结束恢复，保留原记录且不误选尾段", async () => {
    const segment: ChatTranscriptItem = {kind:"execution_response",key:"response:segment",executionId:"exec",itemId:"segment",parts:[],bodyEvicted:false};
    const loader = mock<LoadAroundTurn>(async () => {});
    const h = navigationHarness([segment],loader);
    h.restore({reading_anchor:{node_key:nodeKey({kind:"execution",id:"exec"},"header"),content_position:{block:0,offset_px:0},viewport_offset_px:0},follow_latest:false});
    await act(async () => {}); h.settle();
    expect(loader.mock.calls[0]?.[5]).toBe("exec");
    expect(h.state().navigation?.phase).toBe("unavailable");
    expect(h.scrolls).toEqual([]);
    expect(h.positions).toEqual([]);
    expect(h.statuses).toHaveLength(1);
    h.settle();
    expect(loader).toHaveBeenCalledTimes(1);
    expect(h.statuses).toHaveLength(1);
  });
  test("切换 Thread 后取消旧请求，迟到响应不移动新编辑器", async () => {
    const wait=deferred();let signal:AbortSignal|undefined;
    const h=navigationHarness([turn("current",9)],async(_id,abort)=>{signal=abort;await wait.promise;});
    act(()=>h.state().navigateToTurn("older"));
    h.update([turn("new",1)],"other-thread");
    expect(signal?.aborted).toBe(true);
    await act(async()=>{wait.resolve();await wait.promise;});h.settle();
    expect(h.state().navigation).toBeNull();expect(h.positions).toEqual([]);
  });
  test("最新请求失败保留错误及退出跟随意图", async () => {
    const h=navigationHarness([turn("one",1)],undefined,true,undefined,{hasNewerMessages:true,loadLatestMessages:async()=>{throw new Error("网络中断");}});
    await act(async()=>h.state().scrollToLatest());
    expect(h.statuses.at(-1)).toContain("网络中断");
    expect(h.state().followOutput(true)).toBe(false);
    expect(h.positions).toEqual([]);
  });
  test("主动跳最新更新宿主位置，程序恢复不抢写默认记录", async () => {
    const h=navigationHarness([turn("one",1),turn("two",2)]);
    act(()=>h.state().navigateToTurn("one"));h.settle();
    await act(async()=>h.state().scrollToLatest());
    expect(h.positions.at(-1)).toEqual({reading_anchor:null,follow_latest:true});
    const count=h.positions.length;
    act(()=>h.viewport().navigate({reading_anchor:null,follow_latest:true},true));
    h.settle();
    expect(h.positions.length).toBe(count);
  });
  test.each(["wheel","keydown","touchstart"] as const)("%s 释放锁定，后续布局不重放旧位置", input=>{
    const h=navigationHarness([turn("one",1),turn("two",2),turn("three",3)]);
    act(()=>h.state().navigateToTurn("two"));h.settle();
    h.userScroll(25,input);h.heights.set("one",180);h.resize();
    expect(h.scroller.scrollTop).toBe(25);expect(h.state().viewportAnchored).toBe(false);
  });
  test.each(["pointerdown","mousedown","pointermove","mousemove"] as const)("%s 发生在按钮或图标时不误判原生滚动条", type=>{
    const h=navigationHarness([turn("one",1),turn("two",2),turn("three",3)]);
    act(()=>h.state().navigateToTurn("two"));h.settle();
    h.childInput(type,"button");h.childInput(type,"icon");
    expect(h.state().viewportAnchored).toBe(true);
  });
  test("原生滚动条按下未移动释放后保持跟随；移动后退出",()=>{
    const h=navigationHarness([turn("one",1),turn("two",2),turn("three",3)]);h.settle();
    h.scrollbarDown();expect(h.state().followOutput(true)).toBe(false);
    h.scrollbarUp();expect(h.state().followOutput(true)).toBe("auto");
    h.scrollbarDown();h.userScroll(40);h.scrollbarUp();
    expect(h.state().followOutput(true)).toBe(false);
  });
  test("拖动期间前插冻结索引，释放只补偿一次",()=>{
    const h=navigationHarness([turn("two",2),turn("three",3),turn("four",4)]);h.settle();
    const index=h.state().firstItemIndex;h.scrollbarDown();h.userScroll(60);
    h.update([turn("one",1),turn("two",2),turn("three",3),turn("four",4)]);
    expect(h.state().firstItemIndex).toBe(index);h.scrollbarUp();
    expect(h.state().firstItemIndex).toBe(index-1);
    h.scroller.scrollTop=210;h.flush();expect(h.scroller.scrollTop).toBe(60);
    h.userScroll(25,"wheel");h.resize();expect(h.scroller.scrollTop).toBe(25);
  });
  test("预览与布局不发布持久位置，用户接受才发布",()=>{
    const h=navigationHarness([turn("one",1),turn("two",2),turn("three",3)]);
    act(()=>h.viewport().navigate(position("one"),true));h.settle();expect(h.positions).toEqual([]);
    act(()=>h.state().navigateToTurn("two"));h.settle();
    expect(h.positions.at(-1)?.reading_anchor?.node_key.source_ref.id).toBe("two");
  });
});
