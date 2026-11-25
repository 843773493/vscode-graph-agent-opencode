import { useTranscriptDisclosure, useDisclosureFocus } from "./TranscriptViewContext";
import { contextNode, nodeAttributes } from "../../state/timeline/transcriptNodes";
import React from "react";
import { formatNumber, t } from "../../../../shared/core/i18n/index.js";
import type { TurnResponseItem } from "../../types/backend";
import { ContextNoticeContinuationContext, useChatItemMenu } from "./itemContextMenu";
import { errorMessage } from "../../utils/errorMessage";
import { failureSummary } from "../../../../shared/core/failurePresentation";

function noticeLabel(notice: TurnResponseItem): string {
  const kind = notice.context_kind ?? "";
  switch (kind) {
    case "csm_protocol": return t("chat.contextNotice.sourceProtocol");
    case "agent_instructions": return t("chat.contextNotice.agentInstructions");
    case "runtime_identity": return t("chat.contextNotice.runtimeIdentity");
    case "todo-list":
    case "todo_list": return t("chat.contextNotice.todoRules");
    case "filesystem-rules":
    case "filesystem_rules": return t("chat.contextNotice.filesystemRules");
    case "compact-tool-usage":
    case "compact_tool_usage": return t("chat.contextNotice.compactionRules");
    case "skill_catalog_metadata": return t("chat.contextNotice.skillCatalog");
    case "mcp_tool_catalog": return t("chat.contextNotice.toolCatalog");
    case "workspace_agents": return t("chat.contextNotice.workspaceInstructions");
    case "team_coordination_policy": return t("chat.contextNotice.teamRules");
  }
  if (kind === "checkpoint_reminder") return t("chat.contextNotice.interruption");
  if (kind.endsWith("_retry")) return t("chat.contextNotice.retry");
  if (kind.startsWith("goal_")) return t("chat.contextNotice.goal");
  if (kind.startsWith("persistent_task")) return t("chat.contextNotice.task");
  if (kind === "terminal_execution_completed" || kind === "mcp_connection_completed") return t("chat.contextNotice.connection");
  if (kind.startsWith("team_") || kind === "session_message") return t("chat.contextNotice.team");
  return notice.context_kind && notice.context_kind !== "runtime_notice"
    ? notice.context_kind : notice.context_source_id ?? t("chat.contextNotice.title");
}

/** 悬停、菜单及展开仅消费元数据；只有明确复制／查看动作读取冻结正文。 */
export default function ContextNotice({ notices }: { notices: readonly TurnResponseItem[] }): React.ReactNode {
  const first = notices[0];
  const key = contextNode(first);
  const [expanded, setExpanded] = useTranscriptDisclosure(key);
  const focus = useDisclosureFocus(expanded);
  const loadMore = React.useContext(ContextNoticeContinuationContext);
  const [loading, setLoading] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);
  const cursor = notices.find(notice => notice.context_cursor)?.context_cursor;
  const sources = notices.map(notice => {
    if (!notice.context_content_ref) throw new Error(t("chat.contextNotice.unavailable"));
    return { reference: notice.context_content_ref, name: noticeLabel(notice) };
  });
  const menu = useChatItemMenu(() => [
    { id: "toggleContext", label: t(expanded ? "chat.contextNotice.collapse" : "chat.contextNotice.expand"),
      tooltip: t(expanded ? "chat.contextNotice.collapseHint" : "chat.contextNotice.expandHint"), run: () => setExpanded(!expanded) },
  ], true, () => ({ contextSources: sources }));
  const label = t("chat.contextNotice.hint", [...new Set(sources.map(source => source.name))].join(" · "), formatNumber(sources.length));
  return <section ref={focus.root} className={`chat-context-notice${expanded ? " is-expanded" : ""}`} {...menu}
    data-context-marker={notices.map(notice => notice.id).join(" ")}>
    <button ref={focus.header} type="button" className="chat-context-marker" {...menu} {...nodeAttributes(key)} aria-label={label} title={label}
      aria-expanded={expanded} onClick={() => setExpanded(!expanded)}>
      <span className="chat-context-rule" aria-hidden="true" />
    </button>
    {expanded ? <div className="chat-context-details" role="region" aria-label={t("chat.contextNotice.title")}>
      {notices.map((notice, index) => <div className="chat-context-source" key={notice.id}>
        <span className="chat-context-heading">{t("chat.contextNotice.sourceTitle", index + 1, sources[index].name)}</span>
      </div>)}
    </div> : null}
    {cursor ? <button type="button" disabled={!loadMore || loading} onClick={async () => {
      if (!loadMore) return;
      setLoading(true); setError(null);
      try { await loadMore(cursor); }
      catch (cause) { setError(errorMessage(cause)); }
      finally { setLoading(false); }
    }}>{t(loading ? "chat.contextNotice.loadingMore" : "chat.contextNotice.loadMore")}</button> : null}
    {error ? <div className="failure-summary" role="alert">{failureSummary(error)}</div> : null}
  </section>;
}
