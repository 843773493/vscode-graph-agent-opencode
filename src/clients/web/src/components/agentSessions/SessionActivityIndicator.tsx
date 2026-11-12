import { t } from '../../../../shared/core/i18n/index.js';
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
        aria-label={t('navigation.activity.running')}
        title={t('navigation.activity.running')}
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
        aria-label={t('navigation.activity.unread')}
        title={t('navigation.activity.unread')}
      />
    );
  }
  return null;
}
