import { t, formatNumber } from "../../../../shared/core/i18n/index.js";
import React from "react";
import type {
  ConversationModelUsage,
  ConversationTokenUsage,
} from "../../types/frontend";
import { copyTextToClipboard } from "../../utils/clipboard";

const RESPONSE_ACTIONS = [
  // TODO: 接入后端反馈接口后，实现点赞状态持久化与撤销。
  { label: t("chat.actions.helpful"), icon: "thumbsup" },
  // TODO: 接入后端反馈接口后，实现点踩原因收集与状态持久化。
  { label: t("chat.actions.unhelpful"), icon: "thumbsdown" },
] as const;

export default function ResponseActionToolbar({
  responseText,
  tokenUsage,
  modelUsage,
}: {
  responseText: string;
  tokenUsage: ConversationTokenUsage | null;
  modelUsage: ConversationModelUsage | null;
}): React.ReactNode {
  const [copyState, setCopyState] = React.useState<"idle" | "copied" | "error">("idle");
  const resetTimerRef = React.useRef<number | null>(null);

  React.useEffect(() => () => {
    if (resetTimerRef.current !== null) {
      window.clearTimeout(resetTimerRef.current);
    }
  }, []);

  const copyResponse = React.useCallback(async () => {
    if (!responseText) {
      setCopyState("error");
      return;
    }
    try {
      await copyTextToClipboard(responseText);
      setCopyState("copied");
      if (resetTimerRef.current !== null) {
        window.clearTimeout(resetTimerRef.current);
      }
      resetTimerRef.current = window.setTimeout(() => {
        setCopyState("idle");
        resetTimerRef.current = null;
      }, 1600);
    } catch (error) {
      console.error("复制模型回复失败", error);
      setCopyState("error");
    }
  }, [responseText]);

  const copyLabel = copyState === "copied"
    ? t("chat.actions.copied")
    : copyState === "error"
      ? t("chat.actions.copyFailed")
      : t("chat.actions.copy");
  const copyDisabled = responseText.length === 0;
  const tokenUsageTitle = tokenUsage
    ? [
        t("chat.actions.total", formatNumber(tokenUsage.totalTokens)),
        t("chat.actions.input", formatNumber(tokenUsage.inputTokens)),
        t("chat.actions.output", formatNumber(tokenUsage.outputTokens)),
        tokenUsage.cacheReadInputTokens === null
          ? t("chat.actions.noCacheReport")
          : t("chat.actions.cache", formatNumber(tokenUsage.cacheReadInputTokens)),
        t("chat.actions.calls", tokenUsage.reportedModelCalls, tokenUsage.modelCalls),
      ].join(t("chat.listSeparator"))
    : "";
  const modelUsageTitle = modelUsage
    ? t("chat.actions.model", modelUsage.finalModelId)
    : "";
  const usageTitle = [
    t("chat.actions.usageScope"),
    modelUsageTitle,
    tokenUsageTitle || t("chat.actions.usageUnknown"),
    t("chat.actions.costUnknown"),
  ].filter(Boolean).join(t("chat.listSeparator"));

  return (
    <div className="chat-response-actions" role="toolbar" aria-label={t("chat.actions.toolbar")}>
      {/* TODO: 后续接入浏览器语音能力，并补齐播放、暂停和朗读进度状态。 */}
      <button
        type="button"
        className="chat-response-action-button"
        title={t("chat.actions.readAloud")}
        aria-label={t("chat.actions.readAloud")}
        disabled
      >
        <span className="codicon codicon-unmute" aria-hidden="true" />
      </button>
      <button
        type="button"
        className={`chat-response-action-button chat-copy-response${copyState === "copied" ? " is-copied" : ""}${copyState === "error" ? " is-error" : ""}`}
        title={copyDisabled ? t("chat.actions.emptyCopy") : copyLabel}
        aria-label={copyDisabled ? t("chat.actions.emptyCopy") : copyLabel}
        disabled={copyDisabled}
        onClick={() => void copyResponse()}
      >
        <span
          className={`codicon codicon-${copyState === "copied" ? "check" : "copy"}`}
          aria-hidden="true"
        />
      </button>
      {RESPONSE_ACTIONS.map((action) => (
        <button
          key={action.label}
          type="button"
          className="chat-response-action-button"
          title={t("chat.actions.unavailable", action.label)}
          aria-label={t("chat.actions.unavailable", action.label)}
          disabled
        >
          <span className={`codicon codicon-${action.icon}`} aria-hidden="true" />
        </button>
      ))}
      <span className="chat-response-action-status" aria-live="polite">
        {copyState === "idle" ? "" : copyLabel}
      </span>
      <span
        className="chat-response-token-usage"
        title={usageTitle}
        aria-label={usageTitle}
      >
        {modelUsage ? (
          <span className="chat-response-model-id">
            {t("chat.actions.modelBadge", modelUsage.finalModelId)}
          </span>
        ) : null}
        {modelUsage && tokenUsage ? <span aria-hidden="true">·</span> : null}
        {tokenUsage ? (
          <>
            <span>{t("chat.actions.tokensBadge", formatNumber(tokenUsage.totalTokens))}</span>
            <span aria-hidden="true">·</span>
            <span>
              {t("chat.actions.cacheBadge", tokenUsage.cacheReadInputTokens === null
                ? "—"
                : formatNumber(tokenUsage.cacheReadInputTokens))}
            </span>
          </>
        ) : <span>{t("chat.actions.usageUnknown")}</span>}
      </span>
    </div>
  );
}
