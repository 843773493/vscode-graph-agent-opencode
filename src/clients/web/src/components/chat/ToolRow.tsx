import { useTranscriptDisclosure, useDisclosureFocus } from "./TranscriptViewContext";
import { nodeAttributes, nodeKey, partNode, type TranscriptNodeKey } from "../../state/timeline/transcriptNodes";
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

function ToolRow({
  item,
  showRawDetails,
  onLoadDetails, parentNode,
}: {
  item: ToolItem;
  showRawDetails: boolean;
  onLoadDetails?: (toolCallId: string) => Promise<void>;
  parentNode?: TranscriptNodeKey;
}): React.ReactNode {
  const { message, tool, item: locatedItem, onMount, close } = React.useContext(MessageSearchLocationContext);
  const navigationTarget = locatedItem ?? tool ?? message;
  const locatedToolId = tool && item.responseSources?.some(entry => entry.source?.item_id === tool.item_id) ? tool.item_id : undefined;
  const locatedItemId = message?.role === "tool" && item.responseSources?.some(entry => entry.source?.item_id === message.reference?.item_id)
    ? message.reference?.item_id : undefined;
  const source = item.responseSources?.[0];
  const key = source ? partNode(source, "header") : nodeKey({ kind: "stream", id: item.id }, "header");
  const [open, setOpen] = useTranscriptDisclosure(key);
  const focus = useDisclosureFocus(open);
  const itemLocated = locatedItem && item.responseSources?.some(entry => entry.source.item_id === locatedItem.item_id);
  const containsTarget = Boolean(locatedItemId || locatedToolId || itemLocated);
  React.useEffect(() => { if (containsTarget) setOpen(true); }, [navigationTarget, containsTarget, setOpen]);
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
  React.useEffect(() => {
    if (!open || loading || error || item.detailsLoaded || !item.toolCallId || !onLoadDetails) return;
    setLoading(true);
    void onLoadDetails(item.toolCallId).catch(cause => setError(errorMessage(cause)))
      .finally(() => setLoading(false));
  }, [open, item.detailsLoaded, item.toolCallId, onLoadDetails, error]);
  const handleClick = () => { if (open && containsTarget) close(); setError(null); setOpen(!open); };
  const content = open
    ? display.detailsContent
    : null;

  const inputSource = item.responseSources?.find(part => part.kind === "tool_call");
  const resultSource = item.responseSources?.find(part => part.kind === "tool_result");
  return (
    <section ref={element => { focus.root.current = element; locateRef(element); }} data-tool-result-item-id={locatedToolId} className={`chat-tool-row ${status.className}`} data-response-sources={item.responseSources ? JSON.stringify(item.responseSources) : undefined}>
      <button ref={focus.header}
        type="button"
        className="chat-tool-summary" {...nodeAttributes(key, parentNode)} data-transcript-members={JSON.stringify(item.responseSources?.map(part => partNode(part).source_ref) ?? [key.source_ref])}
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
          {content && (inputSource || content.input) ? <div {...nodeAttributes(inputSource ? partNode(inputSource,"tool_input") : nodeKey({kind: "stream", id: item.id}, "tool_input"), key)}>
            <MarkdownContent value={content.input} />
          </div> : null}
          {content && (resultSource || content.result) ? <div {...nodeAttributes(resultSource ? partNode(resultSource,"tool_output") : nodeKey({kind: "stream", id: item.id}, "tool_output"), key)}>
            <MarkdownContent value={content.result} />
          </div> : null}
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
