import React from "react";

export const LARGE_USER_MESSAGE_RENDER_LIMIT = 20_000;

function ProgressiveUserMessage({
  content,
  internalLabel,
}: {
  content: string;
  internalLabel: string | null;
}): React.ReactNode {
  const [expanded, setExpanded] = React.useState(false);
  React.useEffect(() => setExpanded(false), [content]);
  const truncated = content.length > LARGE_USER_MESSAGE_RENDER_LIMIT && !expanded;
  const visibleContent = truncated
    ? `${content.slice(0, LARGE_USER_MESSAGE_RENDER_LIMIT)}\n\n…`
    : content;

  return (
    <div className="chat-user-text">
      {internalLabel ? (
        <span className="chat-internal-message-label">{internalLabel}</span>
      ) : null}
      {visibleContent}
      {truncated ? (
        <button
          type="button"
          className="chat-user-show-full"
          onClick={() => setExpanded(true)}
        >
          显示完整输入（{content.length.toLocaleString()} 字符）
        </button>
      ) : null}
    </div>
  );
}

export default React.memo(ProgressiveUserMessage);
