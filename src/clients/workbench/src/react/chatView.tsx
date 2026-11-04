import { gatewayWorkspaceKey } from "../../../web/src/api/workspaceIdentity.js";
import { useCallback, useEffect, useRef, useState } from 'react';
import { useTraceNavigation } from '../../../web/src/hooks/runtime/useTraceNavigation.js';
import { useRequestLogNavigation } from '../../../web/src/hooks/runtime/useRequestLogNavigation.js';
import type { TurnViewportController } from '../../../web/src/components/chat/useTurnVirtualScroller.js';
import type { TurnViewportPosition } from '../../../web/src/components/chat/useTurnAnchorNavigation.js';
import { toDisposable, type IDisposable } from '../../upstream/src/vs/base/common/lifecycle.js';
import ContentViewSlots from '../../../web/src/components/shell/ContentViewSlots.js';
import PendingQueueBar from '../../../web/src/components/chat/pending/PendingQueueBar.js';
import Composer from '../../../web/src/components/composer/Composer.js';
import WarmConfirmProvider from '../../../web/src/components/shell/WarmConfirmProvider.js';
import { ComposerContext } from '../../../web/src/hooks/app/appContext.js';
import { useThreadSessionRuntime, type ThreadSessionSeed } from '../../../web/src/hooks/session/useThreadSessionRuntime.js';
import { getSession, listAgents } from '../../../web/src/api.js';
import { ensureGatewayUserAccess } from '../../../web/src/api/gateway/userAccess.js';
import { browserGatewayId, getBrowserUiSettings } from '../../../web/src/hooks/settings/browserUiSettings.js';
import type { AttachmentRef, Session } from '../../../web/src/types/backend.js';
import { gateway } from '../agentApi.js';
import { ThreadInput } from '../threadInput.js';
import type { IEditorService } from '../../upstream/src/vs/workbench/services/editor/common/editorService.js';
import type { INotificationService } from '../../upstream/src/vs/platform/notification/common/notification.js';
import { ReactSurface } from './surface.js';
import { ResourceView } from './resourceView.js';
import { productStyles } from './productStyles.js';
import viewStyle from './chatView.css?text';
import type { GatewayResolvedThemeConfig } from '../../../web/src/theme/theme.js';
import { WorkspaceFileReferenceProvider } from '../../../web/src/components/workspace/WorkspaceFileReferenceContext.js';
import { resourceFileRootUri } from '../../../web/src/utils/workspaceFileReferences.js';
import { workspaceFileUri } from '../workspaceFiles.js';
import { resizeAdjacentMainAreas } from '../../../web/src/layout/workbenchLayout.js';
import { installPointerDrag } from '../../../web/src/utils/pointerDrag.js';
import type { FileOpenMenuHandler } from '../../../web/src/components/workspace/fileTree/WorkspaceFileTreeContextMenu.js';
import { threadHistoryCache, type ThreadHistoryOwner } from '../../../web/src/state/threadHistoryCache.js';
import type { SessionTurnTimeline } from '../../../web/src/state/session/turnTimeline.js';
import { HttpRequestError } from '../../../web/src/api/http.js';
import { CachedThreadHistory } from './cachedThreadHistory.js';
import SessionContextForkDialog from '../../../web/src/components/overlays/SessionContextForkDialog.js';
import { useSessionForkDialog } from '../../../web/src/hooks/shell/useSessionForkDialog.js';
import { requireGatewayWorkspaceRequestTarget } from '../../../web/src/api/workspaceIdentity.js';
import type { InternalDisplayProvenance } from '../../../web/src/types/backend.js';
import type { TurnMessageMatchDTO, LLMRequestLogMatchDTO, TraceMatchDTO } from '../../../web/src/types/protocol_generated/boxteam/workspace/v2/public.js';

type SearchNavigation = { kind: 'message'; match: TurnMessageMatchDTO } | { kind: 'request'; match: LLMRequestLogMatchDTO } | { kind: 'trace'; match: TraceMatchDTO };
interface ThreadViewportController extends TurnViewportController {
  capturePresentation(): () => void;
}

function ThreadConversation({ input, seed, active, editors, onTheme, onUpdate, onFileOpenMenu, onError, navigation, onViewportReady }: {
  input: ThreadInput; seed: ThreadSessionSeed; active: boolean; editors: IEditorService; onTheme: (theme: GatewayResolvedThemeConfig) => void; onUpdate: () => void;
  onFileOpenMenu: FileOpenMenuHandler;
  onError: (message: string) => void;
  navigation?: SearchNavigation;
  onViewportReady: (controller: ThreadViewportController | undefined) => void;
}) {
  const owner = input.owner;
  const messageNavigation = navigation?.kind === 'message' ? navigation.match : undefined;
  const requestLogNavigation = navigation?.kind === 'request' ? navigation.match : undefined;
  const traceNavigation = navigation?.kind === 'trace' ? navigation.match : undefined;
  const [attachment, setAttachment] = useState<AttachmentRef | null>(null);
  const [reportRequest, setReportRequest] = useState<InternalDisplayProvenance | null>(null);
  const [resourceRatio, setResourceRatio] = useState(input.resourceRatio);
  const stopResize = useRef<(() => void) | null>(null);
  useEffect(() => () => stopResize.current?.(), []);
  const onSessionCreated = useCallback(async (session: Session, workspaceKey: string | null) => {
    if (workspaceKey !== owner.workspaceKey || session.workspace_id !== owner.workspaceId || !session.thread_id) throw new Error('新建会话的工作区或主 Thread 归属不一致');
    await editors.openEditor(new ThreadInput({ ...owner, sessionId: session.session_id, threadId: session.thread_id, title: session.title }), { pinned: true });
  }, [owner, editors]);
  const runtime = useThreadSessionRuntime({ owner, seed, active, onSessionCreated, onError });
  const { timeline, history, actions, state, content } = runtime;
  const [dismissedRequest, setDismissedRequest] = useState<LLMRequestLogMatchDTO>();
  const selectedRequest = requestLogNavigation !== dismissedRequest ? requestLogNavigation : undefined;
  const requestNavigation = useRequestLogNavigation(0, owner.workspaceKey, owner.sessionId, selectedRequest);
  const [dismissedTrace, setDismissedTrace] = useState<TraceMatchDTO>();
  const selectedTrace = traceNavigation !== dismissedTrace ? traceNavigation : undefined;
  const traceLocation = useTraceNavigation(0, owner.workspaceKey, owner.sessionId, owner.threadId, selectedTrace);
  const [outlinePreview, setOutlinePreview] = useState(false);
  const contentView = outlinePreview ? 'default' : traceLocation ? 'events' : requestNavigation ? 'requests' : state.contentView;
  const connectViewport = useCallback((controller: TurnViewportController | undefined) => {
    onViewportReady(controller ? { ...controller,
      capturePresentation: () => () => setOutlinePreview(outlinePreview),
      navigate(position, preview) {
        if (preview) setOutlinePreview(true);
        else {
          setDismissedRequest(requestLogNavigation); setDismissedTrace(traceNavigation);
          if (state.contentView !== 'default') void content.switchContentView('default').catch((error: unknown) => onError(String(error)));
          setOutlinePreview(false);
        }
        controller.navigate(position, preview);
      },
    } : undefined);
  }, [content.switchContentView, onError, onViewportReady, outlinePreview, requestLogNavigation, state.contentView, traceNavigation]);
  const fork = useSessionForkDialog({ apiPort: 0,
    sessionsByWorkspace: new Map([[owner.workspaceKey, state.currentSession ? [state.currentSession] : []]]),
    forkSessionContext: runtime.lifecycle.forkSessionContext,
    readSessionSelectionIntent: () => runtime.selectionIntentRef.current,
    invalidateSessionCatalog: () => { void content.refreshSessionResources(owner.sessionId, { force: true }); },
    onForkCreated: async (_source, result) => {
      const target = requireGatewayWorkspaceRequestTarget(state.gatewayWorkspaces, { gatewayId: result.target_gateway_id, workspaceId: result.session.workspace_id });
      await editors.openEditor(new ThreadInput({ gatewayId: result.target_gateway_id, workspaceKey: gatewayWorkspaceKey(target), workspaceId: result.session.workspace_id,
        sessionId: result.session.session_id, threadId: result.thread_id, title: result.session.title }), { pinned: true });
    },
  });
  useEffect(() => { if (state.status) console.info('会话状态：', state.status); }, [state.status]);
  useEffect(() => {
    if (state.contentView !== 'changes' && state.contentView !== 'resources') return;
    input.resourceTab = state.contentView === 'changes' ? 'changes' : 'connections';
    input.resourcesVisible = true;
    onUpdate();
  }, [input, onUpdate, state.contentView]);
  const openChanges = () => { input.resourcesVisible = true; input.resourceTab = 'changes'; onUpdate(); };
  useEffect(() => {
    const theme = state.uiSettings.theme.resolved_theme;
    if (theme) onTheme(theme);
  }, [onTheme, state.uiSettings.theme.resolved_theme]);
  if (timeline?.restoredFromCache) return <CachedThreadHistory owner={owner} timeline={timeline}
    error={timeline.error}
    onRetry={history.refreshTurnHistory} />;
  return <WarmConfirmProvider><ComposerContext.Provider value={runtime.composerValue}>
    <WorkspaceFileReferenceProvider apiPort={0} workspaceId={owner.workspaceKey} onOpen={(file, reference) => {
      const root = resourceFileRootUri(file.path);
      if (!root) throw new Error('消息引用文件缺少后端资源根 URI');
      void editors.openEditor({ resource: workspaceFileUri(owner, file.path, root), options: {
        selection: reference.selection ? { startLineNumber: reference.selection.startLine, startColumn: reference.selection.startColumn ?? 1,
          endLineNumber: reference.selection.endLine, endColumn: 1 } : undefined,
      } }).catch((cause: unknown) => onError(`打开消息引用文件失败：${String(cause)}`));
    }}>
    <div className="native-thread-layout"><div className="native-thread-conversation" style={{ flexGrow: input.resourcesVisible ? 1 - resourceRatio : 1 }}>
      {contentView === 'agent' || contentView === 'requests' ? <div className="native-inspection-scope" role="note">
        {contentView === 'agent' ? '会话主线程上下文' : '会话级 LLM 请求记录'} · {state.currentSession?.title ?? owner.title}
      </div> : null}
      <ContentViewSlots error={null} isBootstrapping={false} onRetryGatewayState={async () => { history.refreshTurnHistory(); }}
        contentView={contentView} apiPort={0} workspaceId={owner.workspaceKey}
        session={state.currentSession} sessionId={owner.sessionId} hasActiveSession activeSessionCacheKey={runtime.scopeKey}
        expandedDetails={state.expandDetails}
        agentStateJsonl={state.agentStateJsonl} agentStateMessageCount={state.agentStateMessageCount}
        agentStateLoadedAt={state.agentStateLoadedAt} agentStateLoading={state.agentStateLoading} agentStateError={state.agentStateError}
        receivedEvents={runtime.receivedEvents} activeTraceHistory={runtime.traceHistory}
        onLoadOlderTraceHistory={runtime.traces.loadOlder} onRetryTraceHistory={runtime.traces.refresh}
        requestLogs={state.llmRequestLogs} requestLogsLoading={state.llmRequestLogsLoading}
        requestLogsError={state.llmRequestLogsError} requestLogsLoadedAt={state.llmRequestLogsLoadedAt}
        traceNavigation={traceLocation} onCloseTraceNavigation={() => setDismissedTrace(traceNavigation)}
        requestLogNavigation={requestNavigation} onCloseRequestLogNavigation={() => setDismissedRequest(requestLogNavigation)}
        onRetryRequestLogs={() => { void content.refreshLLMRequestLogs(owner.sessionId); }}
        conversations={runtime.conversations} activeTurnTimeline={timeline}
        changesHint={null} changesHintLoading={false} activeChangeset={state.activeChangeset}
        gatewayUserViewStates={state.gatewayUserViewStates}
        onLoadAroundTurn={history.loadAroundTurn} onLoadNewerMessages={history.loadNewerTurns} onLoadOlderMessages={history.loadOlderTurns}
        messageNavigation={messageNavigation}
        onViewportReady={connectViewport}
        onViewStateChange={runtime.views.saveSessionViewState} onViewStateRestoreStatus={runtime.setStatus}
        onLoadTurnDetails={history.loadTurnDetails}
        onLoadToolDetails={(turnId, toolCallId) => history.loadTurnDetails([turnId], `tool:${turnId}:${toolCallId}`, false, ['tool_call', 'tool_result'], [toolCallId])}
        onLoadAgentStateMessageRawContent={content.loadAgentStateMessageRawContent}
        onRetryHistory={history.refreshTurnHistory} onReplayTurn={actions.replayTurn} onOpenChanges={openChanges}
        onForkConversation={point => {
          if (point.workspaceId !== owner.workspaceKey || point.sessionId !== owner.sessionId || point.threadId !== owner.threadId) throw new Error('消息分支来源与当前 Thread 不一致');
          void fork.forkSession(owner.workspaceKey, owner.sessionId, point);
        }}
        onOpenChildReport={source => {
          if (source.source_session_id !== owner.sessionId || !source.source_thread_id || !source.source_execution_id) throw new Error('报告来源不属于当前会话的精确子线程执行');
          setReportRequest(source); input.resourcesVisible = true; input.resourceTab = 'connections'; onUpdate();
        }}
        onOpenAttachment={(_sessionId, value) => {
          setAttachment(value); input.resourcesVisible = true; input.resourceTab = 'files'; onUpdate();
        }}
        onRemovePending={actions.removePendingRequest} onChangePendingPolicy={actions.updatePendingRequestPolicy} />
      <PendingQueueBar conversations={runtime.conversations} onClear={actions.clearPendingRequests}
        onRemove={actions.removePendingRequest} onChangePolicy={actions.updatePendingRequestPolicy} />
      <Composer threadId={owner.threadId} />
    </div>{input.resourcesVisible ? <button type="button" className="layout-sash" aria-label="调整会话资源宽度"
      title="拖拽调整会话资源宽度，双击还原" onDoubleClick={() => { input.resourceRatio = 0.42; setResourceRatio(0.42); }}
      onPointerDown={event => {
        event.preventDefault(); stopResize.current?.();
        const layout = event.currentTarget.parentElement!;
        const leftWidth = layout.querySelector<HTMLElement>('.native-thread-conversation')!.getBoundingClientRect().width;
        const rightWidth = layout.querySelector<HTMLElement>('.native-thread-resources')!.getBoundingClientRect().width;
        const startX = event.clientX;
        stopResize.current = installPointerDrag('is-layout-resizing', move => {
          const ratios = resizeAdjacentMainAreas({ ratios: { agent_sessions: 1, chat: 1 - resourceRatio, workspace_preview: resourceRatio, auxiliary: 1 },
            left: 'chat', right: 'workspace_preview', leftWidth, rightWidth, deltaX: move.clientX - startX });
          input.resourceRatio = ratios.workspace_preview; setResourceRatio(ratios.workspace_preview);
        }, () => { stopResize.current = null; });
      }} /> : null}
      <ResourceView input={input} runtime={runtime} editors={editors} active={active} attachment={attachment} ratio={resourceRatio} onUpdate={onUpdate} onFileOpenMenu={onFileOpenMenu} onError={onError}
        reportRequest={reportRequest} onForkThread={(threadId, point) => { void fork.forkSession(owner.workspaceKey, owner.sessionId, { ...point, threadId }); }} /></div>
    <SessionContextForkDialog state={fork.forkDialog} onCancel={fork.closeForkDialog} onSubmit={fork.submitForkDialog} onSelectionChange={fork.updateForkDialogSelection} />
    </WorkspaceFileReferenceProvider>
  </ComposerContext.Provider></WarmConfirmProvider>;
}

function ChatView({ input, active, editors, onTheme, onUpdate, onFileOpenMenu, onError, navigation, onViewportReady }: { input: ThreadInput; active: boolean; editors: IEditorService; onTheme: (theme: GatewayResolvedThemeConfig) => void; onUpdate: () => void; onFileOpenMenu: FileOpenMenuHandler; onError: (message: string) => void; navigation?: SearchNavigation; onViewportReady: (controller: ThreadViewportController | undefined) => void }) {
  const owner = input.owner;
  const [seed, setSeed] = useState<ThreadSessionSeed | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [cached, setCached] = useState<SessionTurnTimeline | null>(null);
  const [cacheError, setCacheError] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    let disposed = false;
    let cacheOwner: ThreadHistoryOwner | undefined;
    setError(null);
    const cacheLoad = Promise.resolve().then(async () => {
      cacheOwner = { gatewayId: owner.gatewayId, workspaceId: owner.workspaceId, sessionId: owner.sessionId, threadId: owner.threadId };
      const timeline = await threadHistoryCache.read(cacheOwner, JSON.stringify([owner.workspaceKey, owner.sessionId, owner.threadId]));
      if (!disposed) setCached(timeline);
      return timeline;
    }).catch((cause: unknown) => {
      if (!disposed) setCacheError(`读取历史缓存失败：${String(cause)}`);
      return null;
    });
    const initialize = async () => {
      await gateway.assertWorkspace(owner);
      const [session, agents, access, workspaces, settings, browserViewScope] = await Promise.all([
        getSession(0, owner.sessionId, owner.workspaceKey), listAgents(0, owner.workspaceKey),
        ensureGatewayUserAccess(0), gateway.workspaces(), getBrowserUiSettings(0), browserGatewayId(0),
      ]);
      if (session.session_id !== owner.sessionId || session.workspace_id !== owner.workspaceId) throw new Error('会话编辑器初始化归属不一致');
      const history = await cacheLoad;
      if (!disposed) setSeed({ session, agents, access, workspaces: workspaces.items, settings, browserViewScope, history });
    };
    void initialize().catch(async (cause: unknown) => {
      // 已删除的会话不能继续显示浏览器旧记录，清除镜像后明确显示后端错误。
      if (cause instanceof HttpRequestError && (cause.status === 404 || cause.status === 410)) {
        await cacheLoad;
        if (!disposed) setCached(null);
        if (cacheOwner) await threadHistoryCache.remove(cacheOwner).catch((failure: unknown) => {
          if (!disposed) setCacheError(`清除历史缓存失败：${String(failure)}`);
        });
      }
      if (!disposed) setError(cause instanceof Error ? cause.message : String(cause));
    });
    return () => { disposed = true; };
  }, [owner, attempt]);
  const retry = () => setAttempt(value => value + 1);
  return <>{cacheError ? <p role="alert">{cacheError}</p> : null}
    {seed ? <ThreadConversation input={input} seed={seed} active={active} editors={editors} onTheme={onTheme} onUpdate={onUpdate} onFileOpenMenu={onFileOpenMenu} onError={onError} navigation={navigation} onViewportReady={onViewportReady} />
      : cached ? <CachedThreadHistory owner={owner} timeline={cached} error={error ? `会话同步失败：${error}` : null} onRetry={retry} />
        : error ? <p role="alert">会话初始化失败：{error}</p> : <p role="status">正在恢复会话…</p>}</>;
}

export class ChatSurface {
  private readonly surface: ReactSurface;
  private active: boolean;
  private navigation?: SearchNavigation;
  private viewport?: ThreadViewportController;
  private previewGeneration = 0;
  constructor(container: HTMLElement, private readonly input: ThreadInput, private readonly editors: IEditorService, visible: boolean,
    private readonly onPresentationChange: () => void, private readonly onFileOpenMenu: FileOpenMenuHandler,
    private readonly notifications: INotificationService) {
    this.surface = new ReactSurface(container, [...productStyles, viewStyle]);
    this.active = visible; this.render();
  }
  update(): void { this.render(); }
  revealMessage(match: TurnMessageMatchDTO): void { this.navigation = { kind: 'message', match: { ...match } }; this.render(); }
  revealRequestLog(match: LLMRequestLogMatchDTO): void { this.navigation = { kind: 'request', match: { ...match } }; this.render(); }
  revealTrace(match: TraceMatchDTO): void { this.navigation = { kind: 'trace', match: { ...match } }; this.render(); }
  setVisible(visible: boolean): void { this.active = visible; this.render(); }
  captureViewState(): IDisposable {
    const position = this.requireViewport().capture();
    const restore = this.requireViewport().capturePresentation();
    return toDisposable(() => {
      if (!this.viewport) return;
      // 原 Provider 随后才释放旧预览；恢复属于新导航，不能被旧释放动作取消。
      ++this.previewGeneration;
      if (position) this.viewport.navigate(position, true);
      restore();
    });
  }
  revealTurn(turnId: string, preview = false): IDisposable {
    const generation = ++this.previewGeneration;
    const position: TurnViewportPosition = { turn_anchor: turnId, scroll_offset: 0, follow_latest: false };
    this.requireViewport().navigate(position, preview);
    return toDisposable(() => { if (generation === this.previewGeneration) this.viewport?.cancel(); });
  }
  private requireViewport(): ThreadViewportController {
    if (!this.viewport) throw new Error('Thread 历史视口尚未就绪，请稍后重试');
    return this.viewport;
  }
  private readonly onViewportReady = (controller: ThreadViewportController | undefined) => { this.viewport = controller; };
  focus(): void { this.surface.focus('textarea'); }
  dispose(): void { this.viewport?.cancel(); this.viewport = undefined; this.surface.dispose(); }
  private readonly onUpdate = () => { this.onPresentationChange(); this.render(); };
  private readonly onTheme = (theme: GatewayResolvedThemeConfig) => this.surface.applyTheme(theme, this.notifications);
  private readonly onError = (message: string) => this.notifications.error(`${this.input.owner.title} (${this.input.owner.sessionId}/${this.input.owner.threadId})：${message}`);
  private render(): void { this.surface.render(<ChatView input={this.input} editors={this.editors} active={this.active} onTheme={this.onTheme} onUpdate={this.onUpdate} onFileOpenMenu={this.onFileOpenMenu} onError={this.onError} navigation={this.navigation} onViewportReady={this.onViewportReady} />); }
}
