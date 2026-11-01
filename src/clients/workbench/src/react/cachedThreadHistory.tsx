import { useMemo } from 'react';
import ChatPanel from '../../../web/src/components/panels/ChatPanel.js';
import ChatHistoryPageHeader from '../../../web/src/components/chat/ChatHistoryPageHeader.js';
import { conversationFromTurn } from '../../../web/src/state/conversations/turnProjection.js';
import type { SessionTurnTimeline } from '../../../web/src/state/session/turnTimeline.js';
import type { ThreadOwner } from '../agentApi.js';

const cachedOnly = async () => {};
const unavailable = async (): Promise<never> => { throw new Error('历史缓存尚未同步，暂不可执行此操作'); };

/** 使用原聊天组件显示历史镜像；校准前禁用会改变会话的操作。 */
export function CachedThreadHistory({ owner, timeline, error, onRetry }: {
  owner: ThreadOwner; timeline: SessionTurnTimeline; error: string | null; onRetry: () => void;
}) {
  const conversations = useMemo(() => timeline.orderedTurnIds.map(id =>
    conversationFromTurn(timeline.turnsById[id], owner.threadId)), [timeline, owner.threadId]);
  return <div className="native-thread-conversation native-thread-cached-history">
    <div className="native-thread-status" role="status">{error ? '已显示上次历史缓存，同步未完成' : '已显示上次历史缓存，正在同步会话…'}</div>
    {error ? <ChatHistoryPageHeader projectionState="ready" hasOlderMessages={false}
      loadingOlderMessages={false} error={error} onRetry={onRetry} /> : null}
    <fieldset disabled className="native-thread-cached-content">
      <ChatPanel apiPort={0} workspaceId={owner.routeId} sessionId={owner.sessionId} threadId={owner.threadId}
        conversations={conversations} internalExecutions={timeline.internalExecutions} expandDetails={false}
        hasActiveSession={false} hasNewerMessages={false} hasOlderMessages={timeline.hasBefore} loadingNewerMessages={false}
        loadingOlderMessages={false} historyLoading projectionState="ready" historyError={null}
        onLoadAroundTurn={cachedOnly} onLoadNewerMessages={cachedOnly} onLoadOlderMessages={cachedOnly}
        onLoadTurnDetails={cachedOnly} onLoadAgentStateMessageRawContent={unavailable}
        onRetryHistory={onRetry} onReplayTurn={unavailable} onRemovePending={unavailable} onChangePendingPolicy={unavailable} />
    </fieldset>
  </div>;
}
