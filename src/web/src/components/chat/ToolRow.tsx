import React from "react";
import {
  formatToolCardContent,
  toolCollapsedText,
} from "../../state/toolDisplay";
import type { TimelineItem } from "../../state/timelineTypes";
import MarkdownContent from "./MarkdownContent";

type ToolItem = Extract<TimelineItem, { kind: "aggregated_tool" }>;

function toolStatus(item: ToolItem): {
  icon: string;
  label: string;
  className: string;
} {
  if (item.active) {
    return {
      icon: "codicon-loading codicon-modifier-spin",
      label: `正在运行 ${item.toolName}`,
      className: "is-active",
    };
  }
  if (item.failed) {
    return {
      icon: "codicon-error",
      label: `${item.toolName} 执行失败`,
      className: "is-failed",
    };
  }
  return {
    icon: "codicon-check",
    label: `已运行 ${item.toolName}`,
    className: "is-complete",
  };
}

function fallbackContent(item: ToolItem): string {
  return [
    item.inputText ? `**输入**\n\n\`\`\`json\n${item.inputText}\n\`\`\`` : "",
    item.resultText ? `**输出**\n\n\`\`\`text\n${item.resultText}\n\`\`\`` : "",
  ]
    .filter(Boolean)
    .join("\n\n");
}

export default function ToolRow({
  item,
  showRawDetails,
}: {
  item: ToolItem;
  showRawDetails: boolean;
}): React.ReactNode {
  const [open, setOpen] = React.useState(false);
  const status = toolStatus(item);
  const content = formatToolCardContent(item) ?? fallbackContent(item);
  const hasDetails = Boolean(content || showRawDetails);

  return (
    <section className={`chat-tool-row ${status.className}`}>
      <button
        type="button"
        className="chat-tool-summary"
        aria-expanded={open}
        disabled={!hasDetails}
        onClick={() => setOpen((current) => !current)}
      >
        <span className={`codicon ${status.icon}`} aria-hidden="true" />
        <span className="chat-tool-label">{status.label}</span>
        <span className="chat-tool-preview">{toolCollapsedText(item)}</span>
        {hasDetails ? (
          <span
            className={`codicon ${open ? "codicon-chevron-up" : "codicon-chevron-down"}`}
            aria-hidden="true"
          />
        ) : null}
      </button>
      {open ? (
        <div className="chat-tool-details">
          {content ? <MarkdownContent value={content} /> : null}
          {showRawDetails ? (
            <details className="chat-tool-raw">
              <summary>原始数据</summary>
              <pre>{JSON.stringify({ start: item.rawStart, end: item.rawEnd }, null, 2)}</pre>
            </details>
          ) : null}
        </div>
      ) : null}
    </section>
  );
}
