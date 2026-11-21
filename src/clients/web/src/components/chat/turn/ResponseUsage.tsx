import { t, formatNumber } from "../../../../../shared/core/i18n/index.js";
import React from "react";
import type { ConversationModelUsage, ConversationTokenUsage } from "../../../types/frontend";

export interface ResponseUsageProps {
  tokenUsage: ConversationTokenUsage | null;
  modelUsage: ConversationModelUsage | null;
}

export function responseUsageDetails({ tokenUsage, modelUsage }: ResponseUsageProps): string {
  return [
    t("chat.actions.usageScope"),
    modelUsage ? t("chat.actions.model", modelUsage.finalModelId) : null,
    ...(tokenUsage
    ? [
        t("chat.actions.total", formatNumber(tokenUsage.totalTokens)),
        t("chat.actions.input", formatNumber(tokenUsage.inputTokens)),
        t("chat.actions.output", formatNumber(tokenUsage.outputTokens)),
        tokenUsage.cacheReadInputTokens === null
          ? t("chat.actions.noCacheReport")
          : t("chat.actions.cache", formatNumber(tokenUsage.cacheReadInputTokens)),
        t("chat.actions.calls", tokenUsage.reportedModelCalls, tokenUsage.modelCalls),
      ]
    : [t("chat.actions.usageUnknown")]),
    t("chat.actions.costUnknown"),
  ].filter(Boolean).join("\n");
}

export default function ResponseUsage({ tokenUsage, modelUsage }: ResponseUsageProps): React.ReactNode {
  if (!tokenUsage && !modelUsage) return null;
  return (
    <span className="chat-response-token-usage" aria-label={responseUsageDetails({ tokenUsage, modelUsage })}>
      {tokenUsage ? <>
        <span className="chat-usage-context">{t("chat.actions.contextBadge", formatNumber(tokenUsage.inputTokens))}</span>
        <span className="chat-usage-total">{t("chat.actions.tokensBadge", formatNumber(tokenUsage.totalTokens))}</span>
      </> : null}
      {tokenUsage?.cacheReadInputTokens != null ?
        <span className="chat-usage-cache">{t("chat.actions.cacheBadge", formatNumber(tokenUsage.cacheReadInputTokens))}</span> : null}
      {modelUsage ? <span className="chat-usage-model">{t("chat.actions.modelBadge", modelUsage.finalModelId)}</span> : null}
    </span>
  );
}
