import React from "react";
import type { ConversationTokenUsage } from "../../types/frontend";

const RESPONSE_ACTIONS = [
  // TODO: 接入后端反馈接口后，实现点赞状态持久化与撤销。
  { label: "有帮助", icon: "thumbsup" },
  // TODO: 接入后端反馈接口后，实现点踩原因收集与状态持久化。
  { label: "没有帮助", icon: "thumbsdown" },
] as const;

async function writeClipboardText(text: string): Promise<void> {
  if (navigator.clipboard?.writeText) {
    await navigator.clipboard.writeText(text);
    return;
  }

  // TODO: 浏览器前端强制使用 HTTPS 后，移除非安全 HTTP 上下文的兼容分支。
  const textarea = document.createElement("textarea");
  textarea.value = text;
  textarea.setAttribute("readonly", "");
  textarea.style.position = "fixed";
  textarea.style.opacity = "0";
  document.body.appendChild(textarea);
  textarea.select();
  let copied = false;
  try {
    copied = document.execCommand("copy");
  } finally {
    textarea.remove();
  }
  if (!copied) {
    throw new Error("浏览器拒绝写入剪贴板");
  }
}

export default function ResponseActionToolbar({
  responseText,
  tokenUsage,
  canRegenerate,
  onRegenerate,
}: {
  responseText: string;
  tokenUsage: ConversationTokenUsage | null;
  canRegenerate: boolean;
  onRegenerate: () => void;
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
      await writeClipboardText(responseText);
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
    ? "已复制"
    : copyState === "error"
      ? "复制失败"
      : "复制";
  const numberFormatter = React.useMemo(
    () => new Intl.NumberFormat("zh-CN"),
    [],
  );
  const tokenUsageTitle = tokenUsage
    ? [
        `总 token：${numberFormatter.format(tokenUsage.totalTokens)}`,
        `输入：${numberFormatter.format(tokenUsage.inputTokens)}`,
        `输出：${numberFormatter.format(tokenUsage.outputTokens)}`,
        tokenUsage.cacheReadInputTokens === null
          ? "缓存命中：上游未报告"
          : `缓存命中：${numberFormatter.format(tokenUsage.cacheReadInputTokens)}`,
        `模型调用：${tokenUsage.reportedModelCalls}/${tokenUsage.modelCalls}`,
      ].join("；")
    : "";

  return (
    <div className="chat-response-actions" role="toolbar" aria-label="回复操作">
      {/* TODO: 后续接入浏览器语音能力，并补齐播放、暂停和朗读进度状态。 */}
      <button
        type="button"
        className="chat-response-action-button"
        title="朗读（暂未开放）"
        aria-label="朗读（暂未开放）"
        disabled
      >
        <span className="codicon codicon-unmute" aria-hidden="true" />
      </button>
      <button
        type="button"
        className={`chat-response-action-button chat-copy-response${copyState === "copied" ? " is-copied" : ""}${copyState === "error" ? " is-error" : ""}`}
        title={copyLabel}
        aria-label={copyLabel}
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
          title={`${action.label}（暂未开放）`}
          aria-label={`${action.label}（暂未开放）`}
          disabled
        >
          <span className={`codicon codicon-${action.icon}`} aria-hidden="true" />
        </button>
      ))}
      {canRegenerate ? (
        // TODO: 后续补齐可选模型、重试参数和多候选回复；当前只重新生成最后回复。
        <button
          type="button"
          className="chat-response-action-button"
          title="重新生成最后回复"
          aria-label="重新生成最后回复"
          onClick={onRegenerate}
        >
          <span className="codicon codicon-refresh" aria-hidden="true" />
        </button>
      ) : null}
      <span className="chat-response-action-status" aria-live="polite">
        {copyState === "idle" ? "" : copyLabel}
      </span>
      {tokenUsage ? (
        <span
          className="chat-response-token-usage"
          title={tokenUsageTitle}
          aria-label={tokenUsageTitle}
        >
          <span>总计 {numberFormatter.format(tokenUsage.totalTokens)}</span>
          <span aria-hidden="true">·</span>
          <span>
            缓存 {tokenUsage.cacheReadInputTokens === null
              ? "—"
              : numberFormatter.format(tokenUsage.cacheReadInputTokens)}
          </span>
        </span>
      ) : null}
    </div>
  );
}
