import type React from 'react';
import type { Session } from '../../types/backend';
import { formatDateTime } from '../../utils/format';

function formatRelativeTime(value: string | null | undefined): string {
  const time = new Date(value || '').getTime();
  if (!Number.isFinite(time)) {
    return '';
  }
  const elapsedMs = Math.max(0, Date.now() - time);
  const minute = 60_000;
  const hour = 60 * minute;
  const day = 24 * hour;
  if (elapsedMs < minute) {
    return '刚刚';
  }
  if (elapsedMs < hour) {
    return `${Math.floor(elapsedMs / minute)} 分钟前`;
  }
  if (elapsedMs < day) {
    return `${Math.floor(elapsedMs / hour)} 小时前`;
  }
  return `${Math.floor(elapsedMs / day)} 天前`;
}

export default function AgentSessionsSessionButton({
  session,
  isActive,
  onSelectSession,
  onOpenMenu,
  expanded,
  showToggle,
  onToggle,
  focus,
}: {
  session: Session;
  isActive: boolean;
  onSelectSession: (sessionId: string) => void;
  onOpenMenu: (session: Session, x: number, y: number) => void;
  expanded?: boolean;
  showToggle?: boolean;
  onToggle?: () => void;
  focus?: boolean;
}): React.ReactNode {
  const relativeTime = formatRelativeTime(session.updated_at || session.created_at);
  return (
    <button
      type="button"
      className={`session-item${isActive ? ' active' : ''}${focus ? ' session-item-focus' : ''}`}
      onClick={() => onSelectSession(session.session_id)}
      onContextMenu={(event) => {
        event.preventDefault();
        event.stopPropagation();
        onOpenMenu(session, event.clientX, event.clientY);
      }}
      title={`${session.title || '未命名'}\n${session.session_id}`}
    >
      <span className="session-title">{session.title || '未命名'}</span>
      <span className="session-time">
        {relativeTime || formatDateTime(session.updated_at || session.created_at) || 'now'}
      </span>
      {showToggle ? (
        <span
          className={`codicon codicon-chevron-${expanded ? 'down' : 'right'} session-tree-chevron`}
          aria-hidden="true"
          title={expanded ? '折叠子会话' : '展开子会话'}
          onClick={(event) => {
            event.stopPropagation();
            onToggle?.();
          }}
        />
      ) : null}
    </button>
  );
}
