import { formatNumber, t } from "../../../../../shared/core/i18n/index.js";
import React from "react";
import type { ConversationView } from "../../../types/frontend";
import { TERMINAL_TURN_STATUSES } from "../../../state/conversations/conversationMerge";
import type { RenderGroup, WorkItem } from "../../../state/timeline/chatResponseGroups";
import { historicalBoundaryStatus } from "../../../state/timeline/chatResponseGroups";
import { nodeKey, nodeAttributes, partNode, contextNode, type ProcessRef, type TranscriptNodeKey } from "../../../state/timeline/transcriptNodes";
import { useTranscriptDisclosure, useDisclosureFocus } from "../TranscriptViewContext";
import MarkdownContent from "../MarkdownContent";
import ToolRow from "../ToolRow";
import { errorMessage } from "../../../utils/errorMessage";
import { failureSummary } from "../../../../../shared/core/failurePresentation";
import type { LoadTurnDetails } from "./types";
import { ResponsePart } from "./responseStatusParts";
import { MessageSearchLocationContext } from "../messageSearchLocation";
import { copyItemAction, responseItemMenuActions, responseItemTarget, useChatItemMenu, useChatHover } from "../itemContextMenu";
import ResponseUsage, { responseUsageDetails, type ResponseUsageProps } from "./ResponseUsage";

export function formatActivityDuration(durationMs: number | null | undefined): string {
  if (durationMs === null || durationMs === undefined) return t("chat.activity.durationUnknown");
  if (durationMs < 1000) return t("chat.activity.ms", formatNumber(durationMs));
  return t("chat.activity.seconds", formatNumber(durationMs / 1000, { maximumFractionDigits: durationMs >= 10_000 ? 0 : 1 }));
}

/** 运行中只从原统计向前计时；真实终态立即使用权威值，不累计并行活动。 */
function useProcessDuration(scope: string, settled: boolean, duration: number | null | undefined) {
  const [clock, setClock] = React.useState({scope, duration});
  const current = React.useRef(clock); current.current = clock;
  React.useEffect(() => {
    if (settled || duration == null || typeof window === "undefined") return;
    const previous = current.current;
    const baseline = previous.scope === scope ? Math.max(duration, previous.duration ?? duration) : duration;
    const started = performance.now();
    setClock({scope, duration: baseline});
    const timer = window.setInterval(() => setClock({scope, duration: baseline + performance.now() - started}), 1000);
    return () => window.clearInterval(timer);
  }, [scope, settled, duration]);
  return settled || duration == null || clock.scope !== scope ? duration : Math.max(duration, clock.duration ?? duration);
}

function itemNode(item: WorkItem, slot = "body"): TranscriptNodeKey {
  const part = item.responseSources?.[0];
  return part ? partNode(part, slot) : nodeKey({ kind: "stream", id: item.id }, slot);
}

function ActivityItem({ item, load, onForkItem, parent }: {
  item: Extract<WorkItem,{kind:"aggregated_text"}>; load?: (item: WorkItem) => Promise<void>;
  onForkItem?: (itemId: string) => void; parent: TranscriptNodeKey;
}): React.ReactNode {
  const key = itemNode(item);
  const [open, setOpen] = useTranscriptDisclosure(itemNode(item, "header"));
  const focus = useDisclosureFocus(open);
  const [loading, setLoading] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);
  const menu = useChatItemMenu(() => responseItemMenuActions(item, onForkItem), true, () => responseItemTarget(item));
  const { item: locatedItem, tool, message, close } = React.useContext(MessageSearchLocationContext);
  const navigationTarget = locatedItem ?? tool ?? message;
  const targetId = locatedItem?.item_id ?? tool?.item_id ?? message?.reference?.item_id;
  const containsTarget = Boolean(targetId && item.responseSources?.some(part => part.source.item_id === targetId));
  React.useEffect(() => {
    if (containsTarget) setOpen(true);
  }, [navigationTarget, containsTarget, setOpen]);
  React.useEffect(() => {
    if (!open || loading || error || item.redacted || !load
      || !item.responseSources?.some(part => part.projection === "summary")) return;
    setLoading(true);
    void load(item).catch(cause => setError(errorMessage(cause))).finally(() => setLoading(false));
  }, [open, item, load, error]);
  const toggle = () => { if (open && containsTarget) close(); setError(null); setOpen(!open); };
  return <section ref={focus.root} className="chat-activity-item" data-chat-item-id={item.id} {...menu}>
    <button ref={focus.header} type="button" className="chat-thinking-toggle" {...nodeAttributes(itemNode(item, "header"), parent)}
      data-transcript-members={JSON.stringify(item.responseSources?.map(part => partNode(part).source_ref) ?? [key.source_ref])}
      aria-expanded={open} aria-busy={loading} onClick={toggle}>
      <span className={`codicon ${item.redacted ? "codicon-lock" : "codicon-lightbulb"}`} aria-hidden="true" />
      <span className="chat-activity-summary">{t("chat.process.thinking")}</span>
      <span className={`codicon codicon-chevron-${open ? "down" : "right"}`} aria-hidden="true" />
    </button>
    {open ? <div {...nodeAttributes(key, itemNode(item, "header"))}>
      {error ? <div role="alert" className="chat-inline-error">{failureSummary(error)}</div> : item.redacted ? <span>{t("chat.thinking.redacted")}</span>
        : loading ? <span role="status">{t("chat.activity.loading")}</span>
          : <MarkdownContent value={item.text} responseSources={JSON.stringify(item.responseSources)} />}
    </div> : null}
  </section>;
}

function ToolActivityItem({item, showRawDetails, onForkItem, parent, load}: {
  item: Extract<WorkItem, {kind:"aggregated_tool"}>; showRawDetails:boolean;
  onForkItem?: (itemId:string) => void; parent:TranscriptNodeKey; load?: (item:WorkItem) => Promise<void>;
}) {
  const menu = useChatItemMenu(() => responseItemMenuActions(item, onForkItem), true, () => responseItemTarget(item));
  const loadDetails = React.useCallback(async () => { if (load) await load(item); }, [load,item]);
  return <div className="chat-activity-item" data-chat-item-id={item.id} {...menu}><ToolRow item={item} showRawDetails={showRawDetails}
    onLoadDetails={load ? loadDetails : undefined} parentNode={parent} /></div>;
}

function ActivityDetails({ items, showRawDetails, onForkItem, parent, load }: {
  items: WorkItem[]; showRawDetails: boolean;
  onForkItem?: (itemId: string) => void; parent: TranscriptNodeKey; load?: (item: WorkItem) => Promise<void>;
}): React.ReactNode {
  return <>{items.map(item => item.kind === "aggregated_tool"
    ? <ToolActivityItem key={item.id} item={item} showRawDetails={showRawDetails}
      load={load} parent={parent} onForkItem={onForkItem} />
    : <ActivityItem key={item.id} item={item} parent={parent} load={load} onForkItem={onForkItem} />)}</>;
}

function ActivityGroup({ group, parent, showRawDetails, load, onForkItem }: {
  group: Extract<RenderGroup, {kind: "work"}>; parent: TranscriptNodeKey; showRawDetails: boolean;
  load?: (item: WorkItem) => Promise<void>; onForkItem?: (id: string) => void;
}) {
  const source = group.items[0].responseSources?.[0];
  const key = source?.source.activity_group_item_id
    ? nodeKey({ kind: "item", id: source.source.activity_group_item_id, part: source.source.activity_group_part_ordinal ?? 0 }, "group")
    : itemNode(group.items[0], "group");
  const [open, setOpen] = useTranscriptDisclosure(key);
  const focus = useDisclosureFocus(open);
  const location = React.useContext(MessageSearchLocationContext);
  const navigationTarget = location.item ?? location.tool ?? location.message;
  const target = location.item?.item_id ?? location.tool?.item_id ?? location.message?.reference?.item_id;
  const containsTarget = Boolean(target && group.items.some(item => item.responseSources?.some(part => part.source.item_id === target)));
  React.useEffect(() => { if (containsTarget) setOpen(true); }, [navigationTarget, containsTarget, setOpen]);
  const thinking = group.items.filter(item => item.kind === "aggregated_text").length;
  const tools = group.items.length - thinking;
  return <section ref={focus.root} className="chat-activity-group">
    <button ref={focus.header} className="chat-thinking-toggle" type="button" {...nodeAttributes(key, parent)} data-transcript-members={JSON.stringify(group.items.flatMap(item => item.responseSources?.map(part => partNode(part).source_ref) ?? [itemNode(item).source_ref]))} aria-expanded={open} onClick={() => { if (open && containsTarget) location.close(); setOpen(!open); }}>
      <span className="codicon codicon-list-tree" aria-hidden="true" />
      <span className="chat-activity-summary">{t("chat.process.group", formatNumber(thinking), formatNumber(tools))}</span>
      <span className={`codicon codicon-chevron-${open ? "down" : "right"}`} aria-hidden="true" />
    </button>
    {open ? <div className="chat-thinking-body"><ActivityDetails items={group.items} parent={key} showRawDetails={showRawDetails} load={load} onForkItem={onForkItem} /></div> : null}
  </section>;
}

/** 普通 Turn 与内部 execution 使用同一过程展示；阶段只取所属过程的真实状态。 */
export function ProcessActivity({ process, groups, settled, stats, usage, status, binding, loadDetails, showRawDetails, onForkItem, segment, outline }: {
  process: ProcessRef | {kind: "stream"; id: string}; groups: RenderGroup[]; settled: boolean; stats?: ConversationView["activityStats"];
  usage?: ResponseUsageProps; status?: {title:string;detail?:string;icon:string;kind?:string;role?:"status"|"alert"} | null; showRawDetails: boolean;
  binding?: { sourceViewId: string; sourceViewRevision: number }; loadDetails?: LoadTurnDetails;
  onForkItem?: (id: string) => void; segment?: string;
  outline?: {beforeCursor: string | null};
}): React.ReactNode {
  const header = nodeKey(process, segment ? `header:${segment}` : "header");
  const [open, setOpen] = useTranscriptDisclosure(header, !settled, settled ? "settled" : "live");
  const focus = useDisclosureFocus(open);
  const [loading, setLoading] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);
  const { item, tool, message, close } = React.useContext(MessageSearchLocationContext);
  const navigationTarget = item ?? tool ?? message;
  const target = item?.item_id ?? tool?.item_id ?? message?.reference?.item_id;
  const isFinal = (group: RenderGroup) => group.kind === "response" && group.item.responseSources?.some(part => part.kind === "final_text");
  const processGroups = groups.filter(group => !isFinal(group));
  const containsTarget = Boolean(target && processGroups.some(group => (group.kind === "work" ? group.items : [group.item]).some(item => item.responseSources?.some(part => part.source.item_id === target))));
  React.useEffect(() => {
    if (containsTarget) setOpen(true);
  }, [navigationTarget, containsTarget, setOpen]);
  const fetchOutline = async (cursor?: string | null) => {
    if (!loadDetails || !binding || loading || process.kind === "stream") return;
    setLoading(true); setError(null);
    try {
      await loadDetails(process.kind === "turn" ? [process.id] : [], `outline:${process.id}:${cursor ?? "tail"}`, false,
        process.kind === "execution" ? ["internal", "activity_outline", "assistant_text"] : ["activity_outline", "assistant_text"], undefined,
        { ...binding, executionId: process.kind === "execution" ? process.id : undefined, beforeCursor: cursor ?? undefined });
    } catch (cause) { setError(errorMessage(cause)); }
    finally { setLoading(false); }
  };
  React.useEffect(() => {
    if (open && settled && !outline && !loading && !error && !target) void fetchOutline();
  }, [open, settled, outline, error, target, binding?.sourceViewId, binding?.sourceViewRevision]);
  const load = React.useCallback(async (entry: WorkItem) => {
    if (!loadDetails || !binding) return;
    const ids = [...new Set(entry.responseSources?.flatMap(part => part.source.item_id ? [part.source.item_id] : []) ?? [])];
    if (!ids.length) return;
    await loadDetails(process.kind === "turn" ? [process.id] : [], `items:${ids.join(",")}`, false,
      ["text", "reasoning_detail", "reasoning_summary", "encrypted_reasoning_meta", "tool_call", "tool_result", ...(process.kind === "execution" ? ["internal" as const] : [])], undefined,
      { ...binding, executionId: process.kind === "execution" ? process.id : undefined, itemIds: ids });
  }, [loadDetails, binding?.sourceViewId, binding?.sourceViewRevision, process.kind, process.id]);
  const hasContents = processGroups.length > 0 || (stats?.item_count ?? 0) > 0;
  const toggle = () => { if (open && containsTarget) close(); if (!open) setError(null); setOpen(!open); };
  const active = !settled ? processGroups.flatMap(group => group.kind === "work" ? group.items : [group.item])
    .filter((entry): entry is WorkItem => (entry.kind === "aggregated_tool" || entry.kind === "aggregated_text") && entry.active).at(-1) : undefined;
  // 没有目录本地化名称时使用通用活动描述，不读工具参数推测状态。
  const activity = active ? t(active.kind === "aggregated_tool" ? "chat.process.toolRunning" : "chat.process.thinkingRunning") : undefined;
  const elapsed = useProcessDuration(JSON.stringify(process), settled, stats?.duration_ms);
  const duration = elapsed == null ? (!settled ? t("chat.activity.running") : undefined) : formatActivityDuration(elapsed);
  const statusDetail = status?.detail ? failureSummary(status.detail) : undefined;
  const statusMenu = useChatItemMenu(() => status?.detail ? [copyItemAction(errorMessage(status.detail))] : [], status?.role === "alert" && Boolean(status.detail));
  const details = [status?.title, statusDetail, activity, duration, stats ? t("chat.activity.items", formatNumber(stats.item_count)) : undefined, usage ? responseUsageDetails(usage) : undefined].filter(Boolean).join("\n");
  const hover = useChatHover([
    ...(status ? [{label:t("chat.process.status"),value:status.title}, ...(statusDetail ? [{label:t("chat.process.detail"),value:statusDetail}] : [])] : []),
    ...(duration ? [{label:t("chat.process.duration"),value:duration}] : []),
    ...(stats ? [{label:t("chat.process.items"),value:formatNumber(stats.item_count)}] : []),
    ...(activity ? [{label:t("chat.process.activity"),value:activity}] : []),
    ...(usage ? [{label:t("chat.process.usage"),value:responseUsageDetails(usage)}] : []),
  ]);
  const bindHeader = React.useCallback((element: HTMLButtonElement | null) => { focus.header.current = element; hover(element); }, [hover]);
  return <>
    {hasContents || status || duration || usage ? <section ref={focus.root} data-status-kind={status?.kind} data-item-count={stats?.item_count} data-duration-ms={stats?.duration_ms ?? undefined} className={`chat-thinking chat-turn-activity ${open ? "is-open" : "is-complete"}`}>
      <button ref={bindHeader} className="chat-thinking-toggle" type="button" {...nodeAttributes(header)} data-transcript-members={JSON.stringify(processGroups.flatMap(group => (group.kind === "work" ? group.items : [group.item]).flatMap(item => item.kind === "context_event"
        ? item.notices.map(notice => contextNode(notice).source_ref)
        : item.responseSources?.map(part => partNode(part).source_ref) ?? [])))} aria-expanded={hasContents ? open : undefined}
        aria-description={details} aria-busy={loading} onClick={hasContents ? toggle : undefined} disabled={!hasContents}>
        <span className={`codicon ${loading || !settled && !open ? "codicon-loading codicon-modifier-spin" : !settled ? "codicon-circle-filled" : status?.icon ?? "codicon-check"}`} aria-hidden="true" />
        <span className="chat-activity-summary">
          {duration ? <span className="chat-activity-duration">{duration}</span> : null}
          {stats ? <span className="chat-activity-items">{t("chat.activity.items", formatNumber(stats.item_count))}</span> : null}
          {activity ? <span className="chat-activity-current">{activity}</span> : null}
          {status ? <span className="chat-activity-status">{status.title}</span> : null}
          {usage ? <ResponseUsage {...usage} /> : null}
        </span>
        {hasContents ? <span className={`codicon codicon-chevron-${open ? "down" : "right"}`} aria-hidden="true" /> : null}
      </button>
      {statusDetail ? <div role={status?.role ?? "status"} className="chat-working-detail" {...statusMenu} {...nodeAttributes(nodeKey(process, "status"), header)}>{statusDetail}</div> : null}
      {error ? <div role="alert" className="chat-inline-error">{failureSummary(error)}</div> : null}
      {open && hasContents ? <div className="chat-thinking-body">
        {outline?.beforeCursor ? <button type="button" disabled={loading} onClick={() => void fetchOutline(outline.beforeCursor)}>{t("chat.process.loadEarlier")}</button> : null}
        {processGroups.map(group => group.kind === "work" ? <ActivityGroup key={group.id} group={group} parent={header} showRawDetails={showRawDetails} load={load} onForkItem={onForkItem} />
          : <ResponsePart key={group.id} item={group.item} parentNode={header} onForkItem={onForkItem} />)}
      </div> : null}
    </section> : null}
    {groups.filter(isFinal).map(group => group.kind === "response" ? <ResponsePart key={group.id} item={group.item} onForkItem={onForkItem} /> : null)}
  </>;
}

export function TurnActivitySummary({ conversation, groups, usage, showRawDetails, onLoadTurnDetails, onForkItem }: {
  conversation: ConversationView; groups: RenderGroup[]; usage?: ResponseUsageProps;
  showRawDetails: boolean; onLoadTurnDetails?: LoadTurnDetails; onForkItem?: (id: string) => void;
}): React.ReactNode {
  const settled = TERMINAL_TURN_STATUSES.has(conversation.turnStatus ?? "");
  return <ProcessActivity process={{ kind: conversation.turnId ? "turn" : "stream", id: conversation.turnId ?? conversation.conversationId }} settled={settled}
    groups={groups} stats={conversation.activityStats} status={historicalBoundaryStatus(conversation)} usage={usage}
    outline={conversation.activityOutline}
    binding={conversation.sourceViewId && conversation.sourceViewRevision !== undefined ? { sourceViewId: conversation.sourceViewId, sourceViewRevision: conversation.sourceViewRevision } : undefined}
    loadDetails={onLoadTurnDetails} showRawDetails={showRawDetails} onForkItem={onForkItem} />;
}
