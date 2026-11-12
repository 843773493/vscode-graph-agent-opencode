import { t } from "../../../../shared/core/i18n/index.js";
import React from "react";
import type { TimelineItem } from "../../state/timeline/timelineTypes";
import MarkdownContent from "./MarkdownContent";
import ToolRow from "./ToolRow";

type WorkItem =
  | Extract<TimelineItem, { kind: "aggregated_text" }>
  | Extract<TimelineItem, { kind: "aggregated_tool" }>;

function activePreview(items: WorkItem[]): string {
  const latest = items[items.length - 1];
  if (!latest) {
    return t("chat.thinking.view");
  }
  if (latest.kind === "aggregated_tool") {
    if (latest.active) return t("chat.tool.running", latest.toolName);
    if (latest.outcomeUnknown) return t("chat.tool.unknown", latest.toolName);
    if (latest.failed) return t("chat.tool.failed", latest.toolName);
    return t("chat.tool.completed", latest.toolName);
  }
  const plain = latest.text.slice(0, 512)
    .replace(/```[\s\S]*?```/g, " ")
    .replace(/[*_`>#-]/g, " ")
    .replace(/\s+/g, " ")
    .trim();
  return plain.length > 88 ? `${plain.slice(0, 88)}…` : plain || t("chat.thinking.view");
}

export function compactWorkMarkdown(value: string): string {
  const paragraphBreakCount = value.match(/\r?\n\r?\n/g)?.length ?? 0;
  if (paragraphBreakCount >= 4) {
    return value.replace(/\r?\n\r?\n/g, "").trim();
  }
  return value.replace(/\s+/g, " ").trim();
}

function compactWorkText(item: Extract<WorkItem, { kind: "aggregated_text" }>): string {
  return item.partKind === "markdown"
    ? compactWorkMarkdown(item.text)
    : item.text;
}

function ThinkingSection({
  items,
  active,
  completedPreview,
  showRawDetails,
}: {
  items: WorkItem[];
  active: boolean;
  completedPreview: string;
  showRawDetails: boolean;
}): React.ReactNode {
  const [open, setOpen] = React.useState(active);
  const bodyRef = React.useRef<HTMLDivElement | null>(null);

  React.useEffect(() => {
    setOpen(active);
  }, [active]);

  React.useEffect(() => {
    if (active && open && bodyRef.current) {
      bodyRef.current.scrollTop = bodyRef.current.scrollHeight;
    }
  }, [active, items, open]);

  const hasRedactedThinking = items.some(
    (item) => item.kind === "aggregated_text" && item.redacted === true,
  );
  const preview = active ? activePreview(items) : completedPreview;

  return (
    <section className={`chat-thinking ${active ? "is-active" : "is-complete"}`}>
      <button
        type="button"
        className="chat-thinking-toggle"
        aria-expanded={open}
        aria-label={t(open ? "chat.thinking.collapse" : "chat.thinking.expand", preview)}
        onClick={() => setOpen((current) => !current)}
      >
        <span
          className={`codicon ${active ? "codicon-loading codicon-modifier-spin" : "codicon-check"}`}
          aria-hidden="true"
        />
        {active ? <span className="chat-thinking-label">{t("chat.thinking.active")}</span> : null}
        {!open ? (
          <span className="chat-thinking-preview">
            {preview}
          </span>
        ) : null}
        <span
          className={`codicon ${open ? "codicon-chevron-down" : "codicon-chevron-right"}`}
          aria-hidden="true"
        />
      </button>
      {open ? (
        <div ref={bodyRef} className="chat-thinking-body">
          {hasRedactedThinking ? (
            <div className="chat-thinking-notice" role="status">
              <span className="codicon codicon-lock" aria-hidden="true" />
              <span>{t("chat.thinking.redacted")}</span>
            </div>
          ) : null}
          {items.map((item) =>
            item.kind === "aggregated_tool" ? (
              <ToolRow key={item.id} item={item} showRawDetails={showRawDetails} />
            ) : (
              <MarkdownContent key={item.id} value={compactWorkText(item)} />
            ),
          )}
        </div>
      ) : null}
    </section>
  );
}

export default React.memo(ThinkingSection);
