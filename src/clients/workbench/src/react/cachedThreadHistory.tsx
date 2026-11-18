import { useMemo } from 'react';
import { t } from '../../../shared/core/i18n/index.js';
import ChatPanel from '../../../web/src/components/panels/ChatPanel.js';
import ThreadStatusBar from '../../../web/src/components/composer/ThreadStatusBar.js';
import { conversationFromTurn } from '../../../web/src/state/conversations/turnProjection.js';
import type { SessionTurnTimeline } from '../../../web/src/state/session/turnTimeline.js';
import type { ThreadOwner } from '../agentApi.js';

const cachedOnly = async () => {};
const unavailable = async (): Promise<never> => { throw new Error(t('thread.cache.actionUnavailable')); };

/** 使用原聊天组件显示历史镜像；校准前禁用会改变会话的操作。 */
export function CachedThreadHistory({ owner, timeline, error, onRetry }: {
  owner: ThreadOwner; timeline: SessionTurnTimeline; error: string | null; onRetry: () => void;
}) {
  const conversations = useMemo(() => timeline.orderedTurnIds.map(id =>
    conversationFromTurn(timeline.turnsById[id], owner.threadId)), [timeline, owner.threadId]);
  return <div className="native-thread-conversation native-thread-cached-history" aria-busy={!error}>
    <fieldset disabled className="native-thread-cached-content">
      <ChatPanel apiPort={0} workspaceId={owner.workspaceKey} sessionId={owner.sessionId} threadId={owner.threadId}
        conversations={conversations} internalExecutions={timeline.internalExecutions} expandDetails={false}
        hasActiveSession={false} hasNewerMessages={false} hasOlderMessages={timeline.hasBefore} loadingNewerMessages={false}
        loadingOlderMessages={false} historyLoading projectionState="ready" historyError={null}
        onLoadAroundTurn={cachedOnly} onLoadNewerMessages={cachedOnly} onLoadOlderMessages={cachedOnly}
        onLoadTurnDetails={cachedOnly} onLoadAgentStateMessageRawContent={unavailable}
        onRetryHistory={onRetry} onReplayTurn={unavailable} onRemovePending={unavailable} onChangePendingPolicy={unavailable} />
    </fieldset>
    <ThreadStatusBar conversations={conversations} timeline={timeline} activeJobId={null} active
      syncError={error} />
    <div className="thread-history-sync panel-feedback" data-thread-history-sync role="status">
      <span className={`codicon ${error ? "codicon-warning" : "codicon-sync codicon-modifier-spin"}`} aria-hidden="true" />
      <span>{t(error ? "thread.status.syncFailed" : "thread.status.syncing")}</span>
      <button type="button" disabled={!error} aria-label={t("thread.status.refresh")} title={t("thread.status.refresh")} onClick={onRetry}>
        <span className="codicon codicon-refresh" aria-hidden="true" />
      </button>
    </div>
  </div>;
}
