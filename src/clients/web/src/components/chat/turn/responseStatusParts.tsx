import { useTranscriptDisclosure, useDisclosureFocus } from "../TranscriptViewContext";
import { nodeAttributes, responseNode, responseNodeMembers, nodeKey, type TranscriptNodeKey } from "../../../state/timeline/transcriptNodes";
import { t } from "../../../../../shared/core/i18n/index.js";
import React from "react";
import type { ConversationView } from "../../../types/frontend";
import type { MessageStreamActivity } from "../../../state/messageStream/index";
import type { TimelineItem } from "../../../state/timeline/timelineTypes";
import { historicalBoundaryStatus } from "../../../state/timeline/chatResponseGroups";
import { activityRendererRegistry } from "./activityRenderers";
import MarkdownContent from "../MarkdownContent";
import ContextNotice from "../ContextNotice";
import { copyItemAction, responseItemMenuActions, responseItemTarget, useChatItemMenu } from "../itemContextMenu";
import type { ChatTurnActions } from "./useChatTurnActions";
import { MessageSearchLocationContext } from "../messageSearchLocation";
import { errorMessage } from "../../../utils/errorMessage";
import { failureSummary } from "../../../../../shared/core/failurePresentation";

export function ErrorPart({ item }: { item: Extract<TimelineItem, { kind: "trace" }> }) {
  const message = [item.payload.error, item.payload.message, item.payload.detail]
    .find((value): value is string => typeof value === "string" && value.trim().length > 0);
  const menu = useChatItemMenu(() => message ? [copyItemAction(message)] : [], Boolean(message));
  return (
    <div className="chat-inline-error" role="alert" {...menu}>
      <span className="codicon codicon-error" aria-hidden="true" />
      <span>{failureSummary(message ?? t("chat.status.failed"))}</span>
    </div>
  );
}

export function CancelledPart({
  userInitiated,
  label,
}: {
  userInitiated: boolean;
  label?: string;
}) {
  return (
    <div className="chat-inline-cancelled" role="status">
      <span className="codicon codicon-debug-stop" aria-hidden="true" />
      <span>{label ?? (userInitiated ? t("chat.status.interrupted") : t("chat.status.cancelled"))}</span>
    </div>
  );
}

export function RewindStatusPart({ conversation }: { conversation: ConversationView }) {
  const action = conversation.userMessage?.metadata?.replay_action;
  if (
    action !== "retry_failed"
    && action !== "regenerate"
    && action !== "edit_and_continue"
  ) {
    return null;
  }
  const label = action === "edit_and_continue"
    ? t("chat.status.editRewind")
    : action === "regenerate"
      ? t("chat.status.regenerateRewind")
      : t("chat.status.retryRewind");
  return (
    <div className="chat-inline-rewind" role="status" data-status-kind="rewind">
      <span className="codicon codicon-history" aria-hidden="true" />
      <span>{label}</span>
      <span className="chat-working-detail">{t("chat.status.filesKept")}</span>
    </div>
  );
}

function activityIcon(status: MessageStreamActivity["status"]): string {
  if (status === "completed") return "codicon-check";
  if (status === "failed") return "codicon-error";
  if (status === "unknown") return "codicon-warning";
  return "codicon-sync codicon-modifier-spin";
}

function activityRole(status: string): "status" | "alert" {
  return status === "failed" || status === "unknown" ? "alert" : "status";
}

export function ActivityStatusPart({
  activity,
}: {
  activity: MessageStreamActivity;
}) {
  return (
    <div
      className={`chat-inline-activity is-${activity.status}`}
      role={activityRole(activity.status)}
      data-activity-id={activity.activity_id}
    >
      <span className={`codicon ${activityIcon(activity.status)}`} aria-hidden="true" />
      <span>{activityRendererRegistry.render(activity)}</span>
    </div>
  );
}

export function ActivityHistory({
  activities,
}: {
  activities: MessageStreamActivity[] | undefined;
}) {
  const visibleActivities = (activities ?? []).filter(
    (activity) => ["completed", "failed", "unknown"].includes(activity.status),
  );
  if (visibleActivities.length === 0) return null;
  return (
    <div className="chat-activity-history" aria-label={t("chat.status.activity")}>
      {visibleActivities.map((activity) => (
        <ActivityStatusPart key={activity.activity_id} activity={activity} />
      ))}
    </div>
  );
}

export function MessageStreamStatusPart({ conversation }: { conversation: ConversationView }) {
  const stream = conversation.messageStream;
  const menu = useChatItemMenu(() => stream?.failure ? [copyItemAction(stream.failure.message)] : [], Boolean(stream?.failure));
  if (!stream) return null;
  const activityHistory = <ActivityHistory activities={stream.activities} />;
  if (stream.streamStatus === "interrupted") {
    return historicalBoundaryStatus(conversation)
      ? activityHistory
      : <>{activityHistory}<CancelledPart userInitiated /></>;
  }
  if (stream.streamStatus === "failed" && stream.failure) {
    if (stream.failure.code === "job_timeout" || conversation.turnStatus === "timed_out") {
      return (
        <>
          {activityHistory}
          <div className="chat-inline-error" role="alert" {...menu}>
            <span className="codicon codicon-watch" aria-hidden="true" />
            <span>{t("chat.status.timedOut")}</span>
            <span>{failureSummary(stream.failure.message)}</span>
          </div>
        </>
      );
    }
    const failureTitle = stream.failure.code === "execution_lost"
      ? t("chat.status.lost")
      : stream.failure.code === "tool_dispatch_timeout"
        ? t("chat.status.dispatchTimeout")
        : stream.failure.code === "execution_cancelled"
          ? t("chat.status.internalCancel")
          : t("chat.status.failed");
    return (
      <>
        {activityHistory}
        <div className="chat-inline-error" role="alert" {...menu}>
          <span className="codicon codicon-error" aria-hidden="true" />
          <span>{failureTitle}</span>
          <span>{failureSummary(stream.failure.message)}</span>
        </div>
      </>
    );
  }
  if (stream.streamStatus === "failed") {
    return (
      <>
        {activityHistory}
        <div className="chat-inline-error" role="alert">
          <span className="codicon codicon-error" aria-hidden="true" />
          <span>{t("chat.status.noFailureDetail")}</span>
        </div>
      </>
    );
  }
  // 实时连接及执行阶段统一由输入区状态栏展示，正文只保留本轮的终态结果。
  return activityHistory;
}

function needsExecutionRecovery(conversation: ConversationView): boolean {
  if (
    conversation.messageStream?.failure?.code === "execution_lost"
    || conversation.messageStream?.failure?.code === "execution_cancelled"
    || conversation.messageStream?.failure?.code === "tool_dispatch_timeout"
  ) {
    return true;
  }
  return conversation.events.some((event) => {
    if (event.type !== "session_interrupted") return false;
    const payload = event.raw?.payload ?? event.payload ?? {};
    return payload.code === "execution_lost"
      || payload.code === "execution_cancelled"
      || payload.code === "tool_dispatch_timeout"
      || payload.phase === "process_exit";
  });
}

export function ExecutionLostRecoveryPart({
  conversation,
  actions,
  running,
}: {
  conversation: ConversationView;
  actions: ChatTurnActions;
  running: boolean;
}): React.ReactNode {
  if (
    running
    || conversation.pending
    || !conversation.userMessage
    || !needsExecutionRecovery(conversation)
    || actions.confirmAction !== null
  ) {
    return null;
  }
  return (
    <div
      className="chat-turn-action-confirmation chat-execution-recovery"
      role="group"
      aria-label={t("chat.status.recoverLost")}
      data-status-kind="execution-lost-recovery"
    >
      <div className="chat-turn-action-warning">
        {t("chat.status.stoppedLoop")}
      </div>
      <div className="chat-request-edit-actions">
        <button
          type="button"
          disabled={actions.actionRunning}
          onClick={() => actions.setConfirmAction("retry_failed")}
        >
          {t("chat.status.retryTurn")}
        </button>
      </div>
    </div>
  );
}

export function ResponsePart({
  item,
  onForkItem, parentNode, load,
}: {
  parentNode?: TranscriptNodeKey;
  item: TimelineItem;
  load?: (item: Extract<TimelineItem, {kind: "aggregated_text" | "aggregated_tool"}>) => Promise<void>;
  onForkItem?: (itemId: string) => void;
}): React.ReactNode {
  const menu = useChatItemMenu(() => responseItemMenuActions(item, onForkItem), true, () => responseItemTarget(item));
  if (item.kind === "context_event") return <ContextNotice notices={item.notices} />;
  if (item.kind === "aggregated_text" && item.partKind === "content") {
    return <OutputContentPart item={item} parentNode={parentNode} menu={menu} load={load} />;
  }
  if (item.kind === "aggregated_text" && item.partKind === "markdown") {
    return (
      <div {...nodeAttributes(item.responseSources?.[0] ? responseNode(item.responseSources[0]) : nodeKey({ kind: "stream", id: item.id }), parentNode)}><MarkdownContent
        value={item.text}
        className={`chat-response-bubble${item.active ? " is-streaming" : ""}`}
        streaming={item.active}
        preview="tail"
        responseSources={item.responseSources ? JSON.stringify(item.responseSources) : undefined}
        contextMenu={{ ...menu, "aria-label": t("chat.itemMenu.response") }}
      /></div>
    );
  }
  if (item.kind === "aggregated_text" && item.partKind === "compaction") {
    return <CompactionPart item={item} parentNode={parentNode} menu={menu} />;
  }
  if (
    item.kind === "trace"
    && ["job_cancelled", "session_interrupted"].includes(item.eventType)
  ) {
    return <CancelledPart userInitiated={item.eventType === "session_interrupted"} />;
  }
  if (
    item.kind === "trace"
    && ["error", "job_failed"].includes(item.eventType)
  ) {
    return <ErrorPart item={item} />;
  }
  return null;
}

function OutputContentPart({item, parentNode, menu, load}: {
  item: Extract<TimelineItem, {kind: "aggregated_text"}>; parentNode?: TranscriptNodeKey;
  menu: ReturnType<typeof useChatItemMenu>; load?: (item: Extract<TimelineItem, {kind: "aggregated_text"}>) => Promise<void>;
}) {
  const [loading, setLoading] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);
  const [mediaError, setMediaError] = React.useState(false);
  React.useEffect(() => setMediaError(false), [item.media?.source]);
  const key = item.responseSources?.[0] ? responseNode(item.responseSources[0]) : nodeKey({kind: "stream", id: item.id});
  const pending = item.responseSources?.some(source => source.projection === "summary") === true;
  const read = async () => {
    if (!load || loading) return;
    setLoading(true); setError(null);
    try { await load(item); }
    catch (cause) { setError(errorMessage(cause)); }
    finally { setLoading(false); }
  };
  return <section className="chat-response-bubble chat-output-content" {...nodeAttributes(key, parentNode)} {...menu} aria-busy={loading}>
    {pending ? <button type="button" disabled={!load || loading} onClick={() => void read()}>
      {t(loading ? "chat.activity.loading" : "chat.output.load")}</button>
      : item.truncated ? <p role="status">{t("chat.output.tooLarge")}</p>
        : item.media?.kind === "image" ? <img src={item.media.source} alt={t("chat.output.image")} loading="lazy" onError={() => setMediaError(true)} />
          : item.media?.kind === "audio" ? <audio src={item.media.source} controls preload="none" onError={() => setMediaError(true)} />
            : item.media?.kind === "video" ? <video src={item.media.source} controls preload="metadata" onError={() => setMediaError(true)} />
              : item.content ? <details><summary>{t("chat.output.structured")}</summary><pre>{JSON.stringify(item.content, null, 2)}</pre></details>
                : <p role="status">{t("chat.output.pending")}</p>}
    {error ? <p className="chat-inline-error failure-summary" role="alert">{failureSummary(error)}</p> : null}
    {mediaError ? <p className="chat-inline-error" role="alert">{t("chat.output.loadFailed")}</p> : null}
  </section>;
}

function CompactionPart({item, parentNode, menu}: {
  item: Extract<TimelineItem, {kind: "aggregated_text"}>; parentNode?: TranscriptNodeKey;
  menu: ReturnType<typeof useChatItemMenu>;
}) {
  const key = item.responseSources?.[0] ? responseNode(item.responseSources[0], "header") : nodeKey({kind: "stream", id: item.id}, "header");
  const [open, setOpen] = useTranscriptDisclosure(key);
  const focus = useDisclosureFocus(open);
  const location = React.useContext(MessageSearchLocationContext);
  const navigationTarget = location.item ?? location.tool ?? location.message;
  const target = location.item?.item_id ?? location.tool?.item_id ?? location.message?.reference?.item_id;
  const containsTarget = Boolean(target && item.responseSources?.some(part => part.source.item_id === target));
  React.useEffect(() => { if (containsTarget) setOpen(true); }, [navigationTarget, containsTarget, setOpen]);
  return <section ref={focus.root} className="chat-compaction-summary" {...menu}>
    <button ref={focus.header} type="button" {...nodeAttributes(key, parentNode)} data-transcript-members={JSON.stringify(item.responseSources?.flatMap(responseNodeMembers) ?? [key])}
      aria-expanded={open} onClick={() => { if (open && containsTarget) location.close(); setOpen(!open); }}>{t("chat.status.compactionSummary")}</button>
    {open ? <div {...nodeAttributes({...key, slot: "body"}, key)}><MarkdownContent value={item.text} renderMode="plain" /></div> : null}
  </section>;
}
