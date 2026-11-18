import React from "react";
import { t } from "../../../../shared/core/i18n/index.js";
import type { TurnResponsePart } from "../../types/backend";
import { errorMessage } from "../../utils/errorMessage";
import { copyTextToClipboard } from "../../utils/clipboard";
import { ChatItemContentContext, ChatItemOwnerContext, useChatItemMenu } from "./itemContextMenu";

function noticeLabel(notice: TurnResponsePart): string {
  const kind = notice.context_kind ?? "";
  if (kind === "checkpoint_reminder") return t("chat.contextNotice.interruption");
  if (kind.endsWith("_retry")) return t("chat.contextNotice.retry");
  if (kind.startsWith("goal_")) return t("chat.contextNotice.goal");
  if (kind.startsWith("persistent_task")) return t("chat.contextNotice.task");
  if (kind === "terminal_execution_completed" || kind === "mcp_connection_completed") return t("chat.contextNotice.connection");
  if (kind.startsWith("team_") || kind === "session_message") return t("chat.contextNotice.team");
  return notice.context_kind && notice.context_kind !== "runtime_notice"
    ? notice.context_kind : notice.context_source_id ?? t("chat.contextNotice.title");
}

/** 折叠状态只挂载标记；正文预览只在展开期间存活。 */
export default function ContextNotice({ notices }: { notices: readonly TurnResponsePart[] }): React.ReactNode {
  const [expanded, setExpanded] = React.useState(false);
  const header = React.useRef<HTMLButtonElement>(null);
  const host = React.useContext(ChatItemContentContext);
  const owner = React.useContext(ChatItemOwnerContext);
  const label = t("chat.contextNotice.hint", [...new Set(notices.map(noticeLabel))].join(" · "));
  const collapse = () => {
    setExpanded(false);
    // 从详情末尾收起时返回同一条标记，保留键盘焦点和阅读位置。
    header.current?.focus();
  };
  const toggle = () => expanded ? collapse() : setExpanded(true);
  const menu = useChatItemMenu(() => [
    { id: "toggleContext", label: t(expanded ? "chat.contextNotice.collapse" : "chat.contextNotice.expand"),
      tooltip: t(expanded ? "chat.contextNotice.collapseHint" : "chat.contextNotice.expandHint"),
      run: toggle },
    { id: "copyContext", label: t("chat.itemMenu.copy"), tooltip: t("chat.contextNotice.copyHint"),
      run: async () => {
        if (!host || !owner) throw new Error(t("chat.contextNotice.unavailable"));
        const texts: string[] = [];
        for (const notice of notices) {
          if (!notice.source.item_id) throw new Error(t("chat.contextNotice.unavailable"));
          texts.push((await host.read(owner, notice.source.item_id)).map(content => content.content ?? "").join("\n\n"));
        }
        await copyTextToClipboard(texts.join("\n\n"));
      } },
  ], true, () => ({ itemIds: notices.flatMap(notice => notice.source.item_id
    ? [{ id: notice.source.item_id, kind: noticeLabel(notice) }] : []) }));
  return <section className={`chat-context-notice${expanded ? " is-expanded" : ""}`} {...menu} tabIndex={-1}
    data-response-sources={JSON.stringify(notices.map(notice => ({ source: { item_id: notice.source.item_id } })))}>
    <button ref={header} type="button" className={expanded ? "chat-context-toggle" : "chat-context-marker"}
      {...menu} aria-label={expanded ? t("chat.contextNotice.groupTitle", notices.length) : label}
      title={expanded ? t("chat.contextNotice.collapseHint") : label} aria-expanded={expanded} onClick={toggle}>
      {expanded ? <><span className="codicon codicon-chevron-down" aria-hidden="true" />
        <span>{t("chat.contextNotice.groupTitle", notices.length)}</span></>
        : <span className="chat-context-rule" aria-hidden="true" />}
    </button>
    {expanded ? <div className="chat-context-details" role="region" aria-label={t("chat.contextNotice.title")}>
      {notices.map((notice, index) => <ContextNoticeSource key={`${owner?.sessionId}/${owner?.threadId}/${notice.part_id}`}
        notice={notice} index={index} defaultExpanded={notices.length === 1} onCollapseGroup={collapse} />)}
      <button type="button" className="chat-context-link chat-context-collapse" onClick={collapse}>
        {t("chat.contextNotice.collapse")}
      </button>
    </div> : null}
  </section>;
}

function ContextNoticeSource({ notice, index, defaultExpanded, onCollapseGroup }: {
  notice: TurnResponsePart; index: number; defaultExpanded: boolean; onCollapseGroup: () => void;
}): React.ReactNode {
  const [expanded, setExpanded] = React.useState(defaultExpanded);
  const host = React.useContext(ChatItemContentContext);
  const owner = React.useContext(ChatItemOwnerContext);
  const toggle = () => setExpanded(value => !value);
  const menu = useChatItemMenu(() => [
    { id: "toggleContextSource", label: t(expanded ? "chat.contextNotice.collapseSource" : "chat.contextNotice.expandSource"),
      tooltip: t(expanded ? "chat.contextNotice.collapseHint" : "chat.contextNotice.expandHint"), run: toggle },
    { id: "copyContextSource", label: t("chat.itemMenu.copy"), tooltip: t("chat.contextNotice.copyHint"), run: async () => {
      if (!host || !owner || !notice.source.item_id) throw new Error(t("chat.contextNotice.unavailable"));
      await copyTextToClipboard((await host.read(owner, notice.source.item_id)).map(content => content.content ?? "").join("\n\n"));
    } },
    { id: "collapseContext", label: t("chat.contextNotice.collapse"), tooltip: t("chat.contextNotice.collapseHint"), run: onCollapseGroup },
  ], true, () => ({ itemIds: notice.source.item_id ? [{ id: notice.source.item_id, kind: noticeLabel(notice) }] : [] }));
  return <div className="chat-context-source" {...menu} tabIndex={-1}>
    <button type="button" className="chat-context-source-toggle" aria-expanded={expanded}
      title={notice.context_source_id ?? noticeLabel(notice)} onClick={toggle}>
      <span className={`codicon codicon-chevron-${expanded ? "down" : "right"}`} aria-hidden="true" />
      <span className="chat-context-heading">{t("chat.contextNotice.sourceTitle", index + 1, noticeLabel(notice))}</span>
    </button>
    {expanded ? <ContextNoticeBody notice={notice} /> : null}
  </div>;
}

/** 单项展开才读正文；仅保留有限预览，不把大段提示挂入聊天 DOM。 */
function ContextNoticeBody({ notice }: { notice: TurnResponsePart }): React.ReactNode {
  const host = React.useContext(ChatItemContentContext);
  const owner = React.useContext(ChatItemOwnerContext);
  const sessionId = owner?.sessionId, threadId = owner?.threadId, itemId = notice.source.item_id;
  const [preview, setPreview] = React.useState<string | null>(null);
  const [error, setError] = React.useState<string | null>(null);
  const [opening, setOpening] = React.useState(false);
  React.useEffect(() => {
    const controller = new AbortController();
    const load = async () => {
      if (!host || !sessionId || !threadId || !itemId) throw new Error(t("chat.contextNotice.unavailable"));
      const contents = await host.read({ sessionId, threadId }, itemId, controller.signal);
      const text = contents.map(content => content.content ?? "").join("\n\n");
      return text.slice(0, 2400).split("\n").slice(0, 12).join("\n");
    };
    void load().then(value => {
      if (!controller.signal.aborted) setPreview(value);
    }, cause => { if (!controller.signal.aborted) setError(errorMessage(cause)); });
    return () => controller.abort();
  }, [host, sessionId, threadId, itemId]);
  const open = async () => {
    setOpening(true);
    setError(null);
    try {
      if (!host || !owner || !itemId) throw new Error(t("chat.contextNotice.unavailable"));
      await host.open(owner, itemId);
    }
    catch (cause) { setError(errorMessage(cause)); }
    finally { setOpening(false); }
  };
  return <div className="chat-context-source-body">
    {notice.truncated ? <p>{t("chat.contextNotice.limited")}</p> : null}
    {error ? <div className="chat-inline-error" role="alert">{error}</div>
      : preview === null ? <span role="status">{t("chat.contextNotice.loading")}</span> : null}
    {preview !== null ? <pre className="chat-context-preview">{preview}</pre> : null}
    <div className="chat-context-preview-actions">
      <span>{t("chat.contextNotice.preview")}</span>
      <button type="button" className="chat-context-link chat-context-open" disabled={!host || !owner || !itemId || opening}
        title={t("chat.contextNotice.openHint")} onClick={() => void open()}>
        {t(opening ? "chat.contextNotice.opening" : "chat.contextNotice.open")}
      </button>
    </div>
  </div>;
}
