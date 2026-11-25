import { anchorContentPoint, captureReadingAnchor, findTranscriptNode, persistentReadingAnchor } from "./transcriptGeometry";
import type { LoadAroundTurn, TurnNavigationTarget, TurnItemNavigation } from "../../api/session/sessionTurnHistory";
import { StaleTurnReferenceHttpError } from "../../api/session/sessionTurnHistory";
import { HttpRequestError } from "../../api/http";
import React from "react";
import type { VirtuosoHandle } from "react-virtuoso";
import { t } from "../../../../shared/core/i18n/index.js";
import { errorMessage } from "../../utils/errorMessage";
import type { ChatTranscriptItem } from "./transcriptItems";
import type { TurnToolResultDTO, TurnMessageLocationDTO, TurnMessageReferenceDTO, ThreadItemLocationDTO } from "../../types/protocol_generated/boxteam/workspace/v2/public";
import { nodeKey, responseNode, contextNode, contextAnchorReference, encodeNode, type ReadingAnchor, type DisclosureOverrides } from "../../state/timeline/transcriptNodes";

export interface TranscriptViewportPosition { reading_anchor: ReadingAnchor | null; follow_latest: boolean; disclosures?: DisclosureOverrides }
export type TranscriptViewportChangeReason = "scroll" | "navigation" | "layout" | "disclosure";
interface Navigation { scopeKey: string; turnId: string; phase: "loading" | "positioning" | "located" | "unavailable" | "error"; message: string }
interface AnchorTask {
  scopeKey: string; anchor: ReadingAnchor; controller: AbortController; restoring: boolean;
  loading: boolean; located: boolean; preservingViewport?: true;
  itemTarget?: string;
  fallback?: ReadingAnchor | null;
  sourceLoaded?: boolean;
}

function itemContains(item: ChatTranscriptItem, anchor: ReadingAnchor): boolean {
  const source = anchor.node_key.source_ref;
  if (item.kind === "turn") return source.kind === "context"
    ? item.conversation.responseItems?.some(part => part.kind === "context_notice" && encodeNode(contextNode(part)) === encodeNode(anchor.node_key)) === true
    : source.kind === "turn" ? item.conversation.turnId === source.id
    : item.conversation.responseItems?.some(part => part.source.item_id === source.id) === true;
  if (item.kind === "internal_execution") return source.kind === "item" && item.displayPart.item_id === source.id;
  if (item.kind === "execution_response") return source.kind === "execution"
    ? item.executionId === source.id && anchor.node_key.slot === `header:${item.itemId}`
    : item.parts.some(part => part.source.item_id === source.id);
  if (item.kind === "execution_header") return source.kind === "execution" && item.executionId === source.id && anchor.node_key.slot === "header";
  return item.notices.some(part => encodeNode(contextNode(part)) === encodeNode(anchor.node_key));
}

/** 当前编辑器唯一阅读控制器；DOM 未挂载不代表业务来源不存在。 */
export function useTranscriptNavigation({ items, scopeKey, streamRef, scrollerRef, followsLatestRef,
  onLoadAroundTurn, onLoadLatestMessages, hasNewerMessages, historyLoading, viewState,
  onViewStateChange, onViewStateRestoreStatus, messageNavigation }: {
  items: readonly ChatTranscriptItem[]; scopeKey: string;
  streamRef: React.MutableRefObject<VirtuosoHandle | null>; scrollerRef: React.MutableRefObject<HTMLElement | null>;
  followsLatestRef: React.MutableRefObject<boolean>; onLoadAroundTurn: LoadAroundTurn; onLoadLatestMessages: () => Promise<void>;
  hasNewerMessages: boolean; historyLoading: boolean; viewState?: TranscriptViewportPosition | null;
  onViewStateChange?: (position: TranscriptViewportPosition, reason: TranscriptViewportChangeReason) => void;
  onViewStateRestoreStatus?: (message: string) => void; messageNavigation?: TurnNavigationTarget;
}) {
  const anchorRef = React.useRef<AnchorTask | null>(null);
  const restoredScope = React.useRef<string>();
  const frame = React.useRef<number>();
  const handledMessage = React.useRef<TurnNavigationTarget>();
  const [viewportAnchored, setViewportAnchored] = React.useState(false);
  const [navigation, setNavigation] = React.useState<Navigation | null>(null);
  const [locatedMessage, setLocatedMessage] = React.useState<TurnMessageLocationDTO>();
  const [locatedTool, setLocatedTool] = React.useState<TurnToolResultDTO>();
  const [locatedItem, setLocatedItem] = React.useState<ThreadItemLocationDTO>();
  const current = React.useRef({items, scopeKey, onViewStateChange, onViewStateRestoreStatus});
  current.current = {items, scopeKey, onViewStateChange, onViewStateRestoreStatus};
  const cancelAnchor = React.useCallback((clear = false) => {
    anchorRef.current?.controller.abort(); anchorRef.current = null;
    setViewportAnchored(false); setNavigation(null);
    if (clear) { setLocatedMessage(undefined); setLocatedTool(undefined); setLocatedItem(undefined); }
  }, []);
  // 先切换 owner，再向 Workbench 发布控制器；否则初次恢复会被稍后的初始化 effect 取消。
  React.useLayoutEffect(() => {
    cancelAnchor(true); followsLatestRef.current = true;
    restoredScope.current = undefined; handledMessage.current = undefined;
    return () => cancelAnchor();
  }, [scopeKey, cancelAnchor, followsLatestRef]);
  const fail = React.useCallback((task: AnchorTask, cause: unknown) => {
    if (anchorRef.current !== task || current.current.scopeKey !== task.scopeKey) return;
    const message = errorMessage(cause);
    const source = task.anchor.node_key.source_ref;
    const unavailable = source.kind === "turn" && cause instanceof StaleTurnReferenceHttpError
      && cause.detail.turn_ids.includes(source.id)
      || source.kind === "item" && cause instanceof HttpRequestError && cause.status === 404
      && cause.context?.stage === "history_locate" && cause.context.error_type === "HistoryItemUnavailableError"
      && cause.context.target?.item_id === source.id;
    const context = source.kind === "context" ? contextAnchorReference(source.id) : undefined;
    const contextUnavailable = context && cause instanceof HttpRequestError && cause.status === 404
      && cause.context?.stage === "history_locate" && cause.context.error_type === "HistoryContextUnavailableError"
      && cause.context.target?.model_call_id === context.model_call_id && cause.context.target?.anchor === context.anchor;
    cancelAnchor();
    // 普通恢复确认不可用时保留当前有界初始窗口与原记录；不拉取最新、不扫描邻页。
    setNavigation({scopeKey: task.scopeKey, turnId: source.id, phase: task.restoring && (unavailable || contextUnavailable) ? "unavailable" : "error", message});
    current.current.onViewStateRestoreStatus?.(message);
  }, [cancelAnchor]);
  const scrollToLatest = React.useCallback((behavior: "auto" | "smooth" = "auto", rememberIntent = true, restoring = false) => {
    if (!rememberIntent && anchorRef.current) return;
    if (rememberIntent) cancelAnchor(true);
    followsLatestRef.current = true;
    const publish = () => {
      if (rememberIntent && !restoring && followsLatestRef.current && current.current.scopeKey === scopeKey)
        current.current.onViewStateChange?.({reading_anchor:null, follow_latest:true}, "navigation");
    };
    if (hasNewerMessages && rememberIntent) {
      void onLoadLatestMessages().then(publish).catch(cause => {
        if (current.current.scopeKey === scopeKey) { followsLatestRef.current = false; current.current.onViewStateRestoreStatus?.(errorMessage(cause)); }
      });
    } else {
      streamRef.current?.scrollToIndex({index:"LAST", align:"end", behavior});
      publish();
    }
  }, [cancelAnchor, followsLatestRef, hasNewerMessages, onLoadLatestMessages, scopeKey, streamRef]);
  const align = React.useCallback(() => {
    const task = anchorRef.current, scroller = scrollerRef.current;
    if (!task || task.loading || !scroller || task.scopeKey !== current.current.scopeKey) return;
    if (task.itemTarget) {
      const nodes = [...scroller.querySelectorAll<HTMLElement>("[data-transcript-node]")];
      const target = nodes.filter(element => {
        const key = JSON.parse(element.dataset.transcriptNode!);
        return key.source_ref.kind === "item" && key.source_ref.id === task.itemTarget && !["header","group"].includes(key.slot) && element.getBoundingClientRect().height > 0;
      }).sort((left, right) => Number(JSON.parse(left.dataset.transcriptNode!).slot !== task.anchor.node_key.slot) - Number(JSON.parse(right.dataset.transcriptNode!).slot !== task.anchor.node_key.slot))[0];
      if (target) task.anchor.node_key = JSON.parse(target.dataset.transcriptNode!);
    }
    const source = task.anchor.node_key.source_ref;
    if (source.kind === "stream" && !findTranscriptNode(scroller, task.anchor.node_key)) {
      const parts = current.current.items.flatMap(item => item.kind === "turn" ? item.conversation.responseItems ?? [] : item.kind === "execution_response" ? item.parts : []);
      // text 结算成 final_text 可以换容器，但仍须唯一对应真实 producer／内容部位。
      const candidates = parts.filter(part => part.source.item_id
        && (part.kind === source.part_kind || source.part_kind === "text" && part.kind === "final_text") && (
        source.tool ? part.tool_call_id === source.tool : source.model && source.block !== undefined
          && part.source.model_call_id === source.model && part.source.content_block_index === source.block));
      if (candidates.length === 1) task.anchor.node_key = responseNode(candidates[0], task.anchor.node_key.slot);
      else if (task.fallback) {
        task.anchor = task.fallback;
        current.current.onViewStateRestoreStatus?.(t("chat.process.positionLost"));
      }
    }
    const node = findTranscriptNode(scroller, task.anchor.node_key);
    if (!node) {
      // 空执行后来产生分段后，原通用标题已被投影明确替换；不能一直等待它重新挂载。
      if (task.sourceLoaded && source.kind === "execution" && task.anchor.node_key.slot === "header"
        && current.current.items.some(item => item.kind === "execution_response" && item.executionId === source.id)) {
        const message = t("chat.process.positionLost");
        cancelAnchor();
        setNavigation({scopeKey:task.scopeKey,turnId:source.id,phase:task.restoring ? "unavailable" : "error",message});
        current.current.onViewStateRestoreStatus?.(message);
        return;
      }
      if (task.sourceLoaded && source.kind === "item") {
        const parts = current.current.items.flatMap(item => item.kind === "turn" ? item.conversation.responseItems ?? [] : item.kind === "execution_response" ? item.parts : []);
        const sourceParts = parts.filter(part => part.source.item_id === source.id);
        // 只有原 owner 已补读这个 Item，才能确认部位取消；DOM 缺失本身不参与此判断。
        const slotExists = sourceParts.some(part => responseNode(part).slot === task.anchor.node_key.slot
          || ["header", "group"].includes(task.anchor.node_key.slot));
        if (sourceParts.length && !slotExists) {
          const message = t("chat.process.positionLost");
          if (!task.restoring) { fail(task,new Error(message)); return; }
          current.current.onViewStateRestoreStatus?.(message);
          // 仍存在的同一 Item 标题是有界替代，保留“未恢复”结果，不改写原记录。
          const header = sourceParts.map(part => findTranscriptNode(scroller,responseNode(part,"header")))
            .find(node => node && !node.ancestor);
          cancelAnchor();
          if (header) {
            const delta = header.element.getBoundingClientRect().top - scroller.getBoundingClientRect().top - task.anchor.viewport_offset_px;
            scroller.scrollTop = Math.max(0,Math.min(scroller.scrollHeight-scroller.clientHeight,scroller.scrollTop+delta));
          }
          setNavigation({scopeKey:task.scopeKey,turnId:source.id,phase:"unavailable",message});
          return;
        }
      }
      const index = current.current.items.findIndex(item => itemContains(item, task.anchor));
      if (index >= 0) streamRef.current?.scrollToIndex({ index, align:"start", behavior:"auto" });
      return;
    }
    // 折叠隐藏的阅读内容移到真实可见标题，不把标题冒充显式 Item 命中。
    if (node.ancestor && !task.restoring && !task.preservingViewport) return;
    if (node.ancestor) task.anchor = { node_key: JSON.parse(node.element.dataset.transcriptNode!), content_position:{block:0,offset_px:0}, viewport_offset_px: task.anchor.viewport_offset_px };
    const point = anchorContentPoint(node.element, task.anchor.content_position);
    const delta = point - scroller.getBoundingClientRect().top - task.anchor.viewport_offset_px;
    const next = Math.max(0, Math.min(Math.max(0, scroller.scrollHeight-scroller.clientHeight), scroller.scrollTop+delta));
    if (Math.abs(next-scroller.scrollTop)>0.5) scroller.scrollTop=next;
    // 只在合法范围内移动；保留原阅读目标，虚拟列表尚未完成测量时不能把临时钳制写成新位置。
    // 同一几何下 next 不再变化，不触发额外滚动；后续真实尺寸改变仍按原内容点保位。
    if (task.located) return;
    task.located=true;
    if (!task.restoring && !task.preservingViewport) {
      node.element.animate?.([{backgroundColor:"var(--vscode-editor-findMatchBackground)"},{backgroundColor:"transparent"}],{duration:1200});
      setNavigation({scopeKey:task.scopeKey, turnId:task.anchor.node_key.source_ref.id,phase:"located",message:t("chat.turnNavigation.locatedTurn")});
      current.current.onViewStateChange?.({reading_anchor:task.anchor,follow_latest:false},"navigation");
    }
  }, [cancelAnchor, fail, scrollerRef, streamRef]);
  const scheduleAnchorAlignment = React.useCallback(() => {
    if (!anchorRef.current || frame.current !== undefined) return;
    frame.current=requestAnimationFrame(()=>{frame.current=undefined;align();});
  }, [align]);
  const start = React.useCallback((anchor: ReadingAnchor, restoring: boolean, selection?: {turnId?:string;message?: TurnMessageReferenceDTO;tool?:TurnToolResultDTO;item?:TurnItemNavigation}) => {
    cancelAnchor(true); followsLatestRef.current=false; restoredScope.current=scopeKey;
    const task:AnchorTask={scopeKey,anchor,controller:new AbortController(),restoring,loading:false,located:false};
    anchorRef.current=task; setViewportAnchored(true);
    const source=anchor.node_key.source_ref;
    const loaded=current.current.items.some(item=>itemContains(item,anchor) && !(item.kind === "turn" && item.conversation.bodyEvicted)
      && !(item.kind === "execution_response" && item.bodyEvicted && source.kind !== "execution"));
    task.itemTarget = !restoring ? selection?.item?.item_id ?? selection?.tool?.item_id ?? selection?.message?.item_id : undefined;
    if (loaded && !selection && (source.kind !== "item" || scrollerRef.current && findTranscriptNode(scrollerRef.current,anchor.node_key))) { scheduleAnchorAlignment(); return; }
    const context = source.kind === "context" ? contextAnchorReference(source.id) : undefined;
    if (source.kind === "context" && (!source.process || !context)) {
      fail(task, new Error(t("chat.process.positionLost")));
      return;
    }
    task.loading=true;
    setNavigation({scopeKey,turnId:source.id,phase:"loading",message:t("chat.turnNavigation.loading")});
    // 分段标题的 slot 已固定真实段首 Item；恢复早期段不能只读取同一 execution 的尾页。
    const segmentItemId = source.kind === "execution" && anchor.node_key.slot.startsWith("header:")
      ? anchor.node_key.slot.slice("header:".length) : undefined;
    const item = selection?.item ?? (selection?.message || selection?.tool ? undefined
      : source.kind==="item" ? {item_id:source.id} : segmentItemId ? {item_id:segmentItemId} : undefined);
    void onLoadAroundTurn(source.kind==="turn"?source.id:source.kind === "context" && source.process?.kind === "turn" ? source.process.id : selection?.turnId??selection?.tool?.turn_id??"",task.controller.signal,
      selection?.message,selection?.tool?.item_id,item,source.kind==="execution" && !segmentItemId ? source.id:source.kind === "context" && source.process?.kind === "execution" ? source.process.id : undefined,context).then(result=>{
      if(task.controller.signal.aborted||anchorRef.current!==task||current.current.scopeKey!==scopeKey)return;
      task.loading=false;
      task.sourceLoaded = true;
      if(!restoring && result) {
        if("semantic_kind" in result) {
          setLocatedItem(result);
          if (result.semantic_kind === "tool_call" || result.semantic_kind === "tool_result")
            task.anchor.node_key = {...task.anchor.node_key,slot:result.semantic_kind === "tool_call" ? "tool_input" : "tool_output"};
          if (result.semantic_kind === "user_input" && result.turn_id) {
            task.anchor.node_key = nodeKey({kind:"turn",id:result.turn_id}, "user");
            task.itemTarget = undefined;
          }
        }
        else if("reference" in result)setLocatedMessage(result);
        else {
          setLocatedTool(result);
          task.anchor.node_key = {...task.anchor.node_key,slot:"tool_output"};
        }
      }
      setNavigation({scopeKey,turnId:source.id,phase:"positioning",message:t("chat.turnNavigation.positioning")});
      scheduleAnchorAlignment();
    },cause=>fail(task,cause));
  }, [cancelAnchor, fail, followsLatestRef, onLoadAroundTurn, scheduleAnchorAlignment, scopeKey, scrollerRef]);
  const navigateToTurn = React.useCallback((turnId:string,restoring=false,message?:TurnMessageReferenceDTO,tool?:TurnToolResultDTO,item?:TurnItemNavigation)=>{
    const source=item?.item_id??tool?.item_id??message?.item_id;
    start({node_key:source?nodeKey({kind:"item",id:source}):nodeKey({kind:"turn",id:turnId},"header"),content_position:{block:0,offset_px:0},viewport_offset_px:0},restoring, message||tool||item?{turnId,message,tool,item}:undefined);
  }, [start]);
  const preserveViewport = React.useCallback(() => {
    if (followsLatestRef.current || anchorRef.current) return;
    const scroller=scrollerRef.current;
    const anchor=scroller&&captureReadingAnchor(scroller);
    if(!anchor)return;
    const task:AnchorTask={scopeKey:current.current.scopeKey,anchor,controller:new AbortController(),restoring:true,loading:false,located:true,preservingViewport:true,
      fallback:persistentReadingAnchor(scroller, anchor)};
    anchorRef.current=task;setViewportAnchored(true);
    return ()=>{if(anchorRef.current===task)cancelAnchor();};
  },[cancelAnchor,followsLatestRef,scrollerRef]);
  React.useLayoutEffect(scheduleAnchorAlignment,[items,navigation,scheduleAnchorAlignment]);
  React.useEffect(()=>{
    const scroller=scrollerRef.current;
    if(!scroller||!anchorRef.current||typeof ResizeObserver==="undefined")return;
    const observer=new ResizeObserver(scheduleAnchorAlignment);
    observer.observe(scroller);
    scroller.querySelectorAll<HTMLElement>(".chat-virtual-item").forEach(element=>observer.observe(element));
    const mutations=new MutationObserver(scheduleAnchorAlignment);
    mutations.observe(scroller,{childList:true,subtree:true});
    return()=>{observer.disconnect();mutations.disconnect();};
  },[items,viewportAnchored,scrollerRef,scheduleAnchorAlignment]);
  React.useEffect(()=>{
    const task=anchorRef.current;
    if(!task||task.located||task.loading)return;
    const timer=setTimeout(()=>fail(task,new Error(t("chat.turnNavigation.positioningFailed"))),5000);
    return()=>clearTimeout(timer);
  },[navigation,fail]);
  React.useEffect(()=>{
    if(!messageNavigation||historyLoading||handledMessage.current===messageNavigation)return;
    handledMessage.current=messageNavigation;
    if("semantic_kind" in messageNavigation)navigateToTurn(messageNavigation.turn_id??"",false,undefined,undefined,messageNavigation);
    else if("item_id" in messageNavigation)navigateToTurn(messageNavigation.turn_id,false,undefined,messageNavigation);
    else if(messageNavigation.reference)navigateToTurn(messageNavigation.turn_id,false,messageNavigation.reference);
  },[historyLoading,messageNavigation,navigateToTurn]);
  React.useEffect(()=>{
    if(!viewState||historyLoading||restoredScope.current===scopeKey)return;
    restoredScope.current=scopeKey;
    if(viewState.follow_latest)scrollToLatest("auto", true, true);
    else if(viewState.reading_anchor)start(viewState.reading_anchor,true);
  },[viewState,historyLoading,scopeKey,scrollToLatest,start]);
  React.useEffect(()=>()=>{anchorRef.current?.controller.abort();if(frame.current!==undefined)cancelAnimationFrame(frame.current);},[]);
  const closeMessageNavigation = React.useCallback(() => {
    setLocatedMessage(undefined); setLocatedTool(undefined); setLocatedItem(undefined);
    // 点击捕获阶段已为折叠取得阅读锚点；结束显式定位不能顺带取消这次布局保位。
    if (!anchorRef.current?.preservingViewport) cancelAnchor();
  }, [cancelAnchor]);
  return {anchorRef,viewportAnchored,cancelAnchor,navigateToTurn,navigateToAnchor:start,preserveViewport,scheduleAnchorAlignment,
    handleAnchorHeightChanged:scheduleAnchorAlignment,scrollToLatest,closeMessageNavigation,
    locatedMessage,locatedTool,locatedItem,navigation:navigation?.scopeKey===scopeKey&&(!anchorRef.current?.restoring||navigation.phase==="error"||navigation.phase==="unavailable")?navigation:null};
}
