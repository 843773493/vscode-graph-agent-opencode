import React from "react";
import type { AttachmentRef } from "../types/backend";
import { escapeHtml } from "../utils/format";
import { renderMarkdown } from "../utils/markdown";
import AttachmentList from "./AttachmentList";

export interface EventCardProps {
  title: string;
  kind:
    | "message"
    | "trace"
    | "system"
    | "response"
    | "thought"
    | "tool_call"
    | "tool_result"
    | "error";
  tone: "running" | "done" | "danger";
  time: string;
  summary: string;
  content: string;
  collapsedContent?: string;
  raw: Record<string, unknown>;
  index: number;
  attachments?: AttachmentRef[];
  defaultOpen?: boolean;
  showRawDetails?: boolean;
}

export default function EventCard({
  title,
  kind,
  tone,
  time,
  summary,
  content,
  collapsedContent,
  raw,
  index,
  attachments = [],
  defaultOpen = false,
  showRawDetails = true,
}: EventCardProps): React.ReactNode {
  const [open, setOpen] = React.useState(defaultOpen);
  React.useEffect(() => {
    if (defaultOpen) {
      setOpen(true);
    }
  }, [defaultOpen]);
  const collapsedText =
    collapsedContent ?? (content || summary || "（无可读内容）");

  return (
    <article
      className={`event-card event-card-${kind} tone-${tone} ${open ? "is-open" : "is-collapsed"}`}
    >
      <div className="event-card-head">
        <div className="event-card-title-row">
          <span
            className={`event-card-indicator event-card-indicator-${tone}`}
          />
          <span className="event-card-title">{escapeHtml(title)}</span>
        </div>
        <div className="event-card-head-right">
          <span className="badge neutral event-card-time">
            {escapeHtml(time || `#${index + 1}`)}
          </span>
          <button
            type="button"
            className="event-card-toggle"
            aria-expanded={open}
            aria-label={open ? "折叠" : "展开"}
            onClick={() => setOpen((prev) => !prev)}
          >
            {open ? "−" : "+"}
          </button>
        </div>
      </div>
      {!open ? (
        <>
          <div
            className="event-card-summary event-card-summary-collapsed"
            dangerouslySetInnerHTML={{ __html: renderMarkdown(collapsedText) }}
          />
          {attachments.length > 0 && (
            <AttachmentList attachments={attachments} />
          )}
        </>
      ) : (
        <div className="event-card-body">
          {summary && (
            <div className="event-card-summary">{escapeHtml(summary)}</div>
          )}
          {content ? (
            <div
              className="event-card-content"
              aria-label={kind === "response" ? `${title}正文：${content}` : undefined}
              dangerouslySetInnerHTML={{ __html: renderMarkdown(content) }}
            />
          ) : !summary && attachments.length === 0 ? (
            <div className="event-card-empty">（无可读内容）</div>
          ) : null}
          {attachments.length > 0 && (
            <AttachmentList attachments={attachments} />
          )}
          {showRawDetails ? (
            <details className="event-card-details">
              <summary>原始数据</summary>
              <pre>{escapeHtml(JSON.stringify(raw, null, 2))}</pre>
            </details>
          ) : null}
        </div>
      )}
    </article>
  );
}
