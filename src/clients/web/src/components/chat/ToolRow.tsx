import { t } from "../../../../shared/core/i18n/index.js";
import React from "react";
import { deriveToolRowDisplay } from "../../state/display/toolDisplay";
import type { TimelineItem } from "../../state/timeline/timelineTypes";
import MarkdownContent from "./MarkdownContent";
import { errorMessage } from "../../utils/errorMessage";
import { MessageSearchLocation, MessageSearchLocationContext } from "./messageSearchLocation";

type ToolItem = Extract<TimelineItem, { kind: "aggregated_tool" }>;

function toolStatus(item: ToolItem): {
  icon: string;
  label: string;
  className: string;
} {
  if (item.active) {
    return {
      icon: "codicon-loading codicon-modifier-spin",
      label: t("chat.tool.running", item.toolName),
      className: "is-active",
    };
  }
  if (item.incomplete) {
    return {
      icon: "codicon-debug-stop",
      label: t("chat.tool.incomplete", item.toolName),
      className: "is-incomplete",
    };
  }
  if (item.outcomeUnknown) {
    return {
      icon: "codicon-warning",
      label: t("chat.tool.unknown", item.toolName),
      className: "is-unknown",
    };
  }
  if (item.failed) {
    return {
      icon: "codicon-error",
      label: t("chat.tool.failed", item.toolName),
      className: "is-failed",
    };
  }
  return {
    icon: "codicon-check",
    label: t("chat.tool.completed", item.toolName),
    className: "is-complete",
  };
}

function fallbackContent(item: ToolItem): string {
  return [
    item.inputText ? t("chat.tool.inputBody", item.inputText) : "",
    item.resultText ? t("chat.tool.outputBody", item.resultText) : "",
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
  const { message, tool, onMount } = React.useContext(MessageSearchLocationContext);
  const locatedToolId = tool && item.responseSources?.some(entry => entry.source?.item_id === tool.item_id) ? tool.item_id : undefined;
  const locatedItemId = message?.role === "tool" && item.responseSources?.some(entry => entry.source?.item_id === message.reference?.item_id)
    ? message.reference?.item_id : undefined;
  const [open, setOpen] = React.useState(false);
  React.useEffect(() => { if (locatedItemId || locatedToolId) setOpen(true); }, [locatedItemId, locatedToolId, message, tool]);
  const locateRef = React.useCallback((element: HTMLElement | null) => { if (element && locatedToolId && open) onMount(); }, [locatedToolId, open, onMount]);
  const [loading, setLoading] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);
  const status = toolStatus(item);
  const hasMaterializedDetails = Boolean(
    locatedItemId
    || locatedToolId
    || item.detailsLoaded
    || item.inputText
    || item.resultText
    || showRawDetails
    || Object.keys(item.rawStart).length > 0
    || Object.keys(item.rawEnd).length > 0,
  );
  const canLoadDetails = Boolean(item.toolCallId && onLoadDetails);
  const hasDetails = hasMaterializedDetails || canLoadDetails;
  const display = deriveToolRowDisplay(item, open);
  const handleClick = async () => {
    if (loading) return;
    if (!item.detailsLoaded && item.toolCallId && onLoadDetails) {
      setLoading(true);
      setError(null);
      try {
        await onLoadDetails(item.toolCallId);
      } catch (loadError) {
        setError(errorMessage(loadError));
      } finally {
        setLoading(false);
      }
      // 详情区是错误提示的唯一出口：加载失败也必须展开，否则用户点击后
      // 只看到按钮停止转圈，失败被静默吞掉，表现为「点了没反应」。
      setOpen(true);
      return;
    }
    setOpen((current) => !current);
  };
  const content = open
    ? display.detailsContent ?? fallbackContent(item)
    : null;

  return (
    <section ref={locateRef} data-tool-result-item-id={locatedToolId} className={`chat-tool-row ${status.className}`} data-response-sources={item.responseSources ? JSON.stringify(item.responseSources) : undefined}>
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
          {loading ? t("chat.tool.loading") : display.previewText}
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
          {locatedItemId ? <MessageSearchLocation itemId={locatedItemId} /> : null}
          {error ? (
            <div className="chat-inline-error" role="alert">{error}</div>
          ) : null}
          {content ? <MarkdownContent value={content} /> : null}
          {showRawDetails ? (
            <details className="chat-tool-raw">
              <summary>{t("chat.tool.raw")}</summary>
              <pre>{JSON.stringify({ start: item.rawStart, end: item.rawEnd }, null, 2)}</pre>
            </details>
          ) : null}
        </div>
      ) : null}
    </section>
  );
}

export default React.memo(ToolRow);
