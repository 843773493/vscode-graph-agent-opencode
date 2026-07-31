import type { ReactNode } from "react";

export default function SessionActivityIndicator({
  running,
  unread,
}: {
  running: boolean;
  unread: boolean;
}): ReactNode {
  if (running) {
    return (
      <span
        className="session-activity-indicator running"
        role="status"
        aria-label="会话正在运行"
        title="会话正在运行"
      >
        <span
          className="codicon codicon-loading codicon-modifier-spin"
          aria-hidden="true"
        />
      </span>
    );
  }
  if (unread) {
    return (
      <span
        className="session-activity-indicator unread"
        role="status"
        aria-label="会话有未读结果"
        title="会话有未读结果"
      />
    );
  }
  return null;
}
