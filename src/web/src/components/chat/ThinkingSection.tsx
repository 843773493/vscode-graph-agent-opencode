import React from "react";
import type { TimelineItem } from "../../state/timelineTypes";
import MarkdownContent from "./MarkdownContent";
import ToolRow from "./ToolRow";

type WorkItem =
  | Extract<TimelineItem, { kind: "aggregated_text" }>
  | Extract<TimelineItem, { kind: "aggregated_tool" }>;

function collapsedPreview(items: WorkItem[]): string {
  const latest = items[items.length - 1];
  if (!latest) {
    return "查看思考过程";
  }
  if (latest.kind === "aggregated_tool") {
    return latest.active
      ? `正在运行 ${latest.toolName}`
      : latest.failed
        ? `${latest.toolName} 执行失败`
        : `已运行 ${latest.toolName}`;
  }
  const plain = latest.text
    .replace(/```[\s\S]*?```/g, " ")
    .replace(/[*_`>#-]/g, " ")
    .replace(/\s+/g, " ")
    .trim();
  return plain.length > 88 ? `${plain.slice(0, 88)}…` : plain || "查看思考过程";
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

export default function ThinkingSection({
  items,
  active,
  showRawDetails,
}: {
  items: WorkItem[];
  active: boolean;
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

  return (
    <section className={`chat-thinking ${active ? "is-active" : "is-complete"}`}>
      <button
        type="button"
        className="chat-thinking-toggle"
        aria-expanded={open}
        onClick={() => setOpen((current) => !current)}
      >
        <span
          className={`codicon ${active ? "codicon-loading codicon-modifier-spin" : "codicon-check"}`}
          aria-hidden="true"
        />
        <span className="chat-thinking-label">
          {active ? "正在思考" : "已完成思考"}
        </span>
        {!open ? (
          <span className="chat-thinking-preview">{collapsedPreview(items)}</span>
        ) : null}
        <span
          className={`codicon ${open ? "codicon-chevron-up" : "codicon-chevron-down"}`}
          aria-hidden="true"
        />
      </button>
      {open ? (
        <div ref={bodyRef} className="chat-thinking-body">
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
