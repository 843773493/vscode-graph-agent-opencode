import type React from 'react';
import type { Session } from '../../types/backend';
import { formatDateTime } from '../../utils/format';
import SessionActivityIndicator from './SessionActivityIndicator';
import { formatRelativeTime as formatLocalizedRelativeTime, t } from '../../../../shared/core/i18n/index';

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
    return t('navigation.sessions.justNow');
  }
  if (elapsedMs < hour) {
    return formatLocalizedRelativeTime(-Math.floor(elapsedMs / minute), 'minute');
  }
  if (elapsedMs < day) {
    return formatLocalizedRelativeTime(-Math.floor(elapsedMs / hour), 'hour');
  }
  return formatLocalizedRelativeTime(-Math.floor(elapsedMs / day), 'day');
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
  running = false,
  unread = false,
}: {
  session: Session;
  isActive: boolean;
  onSelectSession: (sessionId: string) => void;
  onOpenMenu: (session: Session, x: number, y: number) => void;
  expanded?: boolean;
  showToggle?: boolean;
  onToggle?: () => void;
  focus?: boolean;
  running?: boolean;
  unread?: boolean;
}): React.ReactNode {
  const relativeTime = formatRelativeTime(session.updated_at || session.created_at);
  return (
    <button
      type="button"
      className={`session-item${isActive ? ' active' : ''}${focus ? ' session-item-focus' : ''}`}
      data-session-id={session.session_id}
      onClick={() => onSelectSession(session.session_id)}
      onContextMenu={(event) => {
        event.preventDefault();
        event.stopPropagation();
        onOpenMenu(session, event.clientX, event.clientY);
      }}
      title={`${session.title || t('navigation.sessions.untitled')}\n${session.session_id}`}
    >
      <span className="session-title">{session.title || t('navigation.sessions.untitled')}</span>
      <SessionActivityIndicator running={running} unread={unread} />
      <span className="session-time">
        {relativeTime || formatDateTime(session.updated_at || session.created_at) || t('navigation.sessions.now')}
      </span>
      {showToggle ? (
        <span
          className={`codicon codicon-chevron-${expanded ? 'down' : 'right'} session-tree-chevron`}
          aria-hidden="true"
          title={expanded ? t('navigation.sessions.collapseChildren') : t('navigation.sessions.expandChildren')}
          onClick={(event) => {
            event.stopPropagation();
            onToggle?.();
          }}
        />
      ) : null}
    </button>
  );
}
