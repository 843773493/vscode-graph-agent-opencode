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
  if (item.incomplete) {
    return {
      icon: "codicon-debug-stop",
      label: `${item.toolName} 调用未完成`,
      className: "is-incomplete",
    };
  }
  if (item.outcomeUnknown) {
    return {
      icon: "codicon-warning",
      label: `${item.toolName} 结果未知`,
      className: "is-unknown",
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

function ToolRow({
  item,
  showRawDetails,
  onLoadDetails,
}: {
  item: ToolItem;
  showRawDetails: boolean;
  onLoadDetails?: (toolCallId: string) => Promise<void>;
}): React.ReactNode {
  const [open, setOpen] = React.useState(false);
  const [loading, setLoading] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);
  const status = toolStatus(item);
  const hasMaterializedDetails = Boolean(
    item.detailsLoaded
    || item.inputText
    || item.resultText
    || showRawDetails
    || Object.keys(item.rawStart).length > 0
    || Object.keys(item.rawEnd).length > 0,
  );
  const canLoadDetails = Boolean(item.toolCallId && onLoadDetails);
  const hasDetails = hasMaterializedDetails || canLoadDetails;
  const handleClick = async () => {
    if (loading) return;
    if (!item.detailsLoaded && item.toolCallId && onLoadDetails) {
      setLoading(true);
      setError(null);
      try {
        await onLoadDetails(item.toolCallId);
        setOpen(true);
      } catch (loadError) {
        setError(loadError instanceof Error ? loadError.message : String(loadError));
      } finally {
        setLoading(false);
      }
      return;
    }
    setOpen((current) => !current);
  };
  const content = open
    ? formatToolCardContent(item) ?? fallbackContent(item)
    : null;

  return (
    <section className={`chat-tool-row ${status.className}`}>
      <button
        type="button"
        className="chat-tool-summary"
        aria-expanded={open}
        aria-busy={loading}
        disabled={!hasDetails}
        onClick={() => void handleClick()}
      >
        <span className={`codicon ${loading ? "codicon-loading codicon-modifier-spin" : status.icon}`} aria-hidden="true" />
        <span className="chat-tool-label">{status.label}</span>
        <span className="chat-tool-preview">
          {loading ? "正在加载工具详情…" : toolCollapsedText(item)}
        </span>
        {hasDetails ? (
          <span
            className={`codicon ${open ? "codicon-chevron-down" : "codicon-chevron-right"}`}
            aria-hidden="true"
          />
        ) : null}
      </button>
      {open ? (
        <div className="chat-tool-details">
          {error ? (
            <div className="chat-inline-error" role="alert">{error}</div>
          ) : null}
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

export default React.memo(ToolRow);
