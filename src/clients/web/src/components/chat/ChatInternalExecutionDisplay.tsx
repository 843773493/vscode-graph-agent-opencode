import type { ReactNode } from "react";
import type { InternalExecutionDisplayPart } from "../../types/backend";

export default function ChatInternalExecutionDisplay({
  displayPart,
}: {
  displayPart: InternalExecutionDisplayPart;
}): ReactNode {
  return (
    <article className="chat-internal-execution-display" aria-label="会话生成">
      <span className="chat-internal-execution-label">会话生成</span>
      <p className="chat-internal-execution-text">{displayPart.text}</p>
    </article>
  );
}
