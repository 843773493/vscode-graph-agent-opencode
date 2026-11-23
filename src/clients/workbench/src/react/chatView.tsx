import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react';
import { t } from '../../../shared/core/i18n/index.js';
import { useTraceNavigation } from '../../../web/src/hooks/runtime/useTraceNavigation.js';
import { useRequestLogNavigation } from '../../../web/src/hooks/runtime/useRequestLogNavigation.js';
import type { TranscriptViewportController } from '../../../web/src/components/chat/useTranscriptVirtualScroller.js';
import type { TranscriptViewportPosition, TranscriptViewportChangeReason } from '../../../web/src/components/chat/useTranscriptNavigation.js';
import { nodeKey } from '../../../web/src/state/timeline/transcriptNodes.js';
import { EditorPaneSelectionChangeReason } from '../../upstream/src/vs/workbench/common/editor.js';
import type { ThreadLocation } from '../threadPane.js';
import { toDisposable, type IDisposable } from '../../upstream/src/vs/base/common/lifecycle.js';
import ContentViewSlots from '../../../web/src/components/shell/ContentViewSlots.js';
import Composer from '../../../web/src/components/composer/Composer.js';
import type { ModelPickerRequest } from '../../../web/src/state/modelPicker.js';
import { ComposerModelPicker } from '../modelPicker.js';
import { ComposerDiagnosticsContext } from '../../../web/src/components/composer/ComposerDiagnosticsContext.js';
import { ILogService } from '../../upstream/src/vs/platform/log/common/log.js';
import type { ComposerDraftController } from '../../../web/src/hooks/composer/useComposerDraft.js';
import WarmConfirmProvider from '../../../web/src/components/shell/WarmConfirmProvider.js';
import { ComposerContext } from '../../../web/src/hooks/app/appContext.js';
import { useThreadSessionRuntime, type ThreadSessionSeed } from '../../../web/src/hooks/session/useThreadSessionRuntime.js';
import { getSession, listAgents } from '../../../web/src/api.js';
import { getSessionTurnBootstrap } from '../../../web/src/api/session/sessionTurnHistory.js';
import { browserGatewayId, getBrowserUiSettings } from '../../../web/src/hooks/settings/browserUiSettings.js';
import type { GatewayWorkspace, Session } from '../../../web/src/types/backend.js';
import { gateway } from '../agentApi.js';
import { ThreadInput } from '../threadInput.js';
import type { IEditorService } from '../../upstream/src/vs/workbench/services/editor/common/editorService.js';
import type { INotificationService } from '../../upstream/src/vs/platform/notification/common/notification.js';
import { ReactSurface } from './surface.js';
import { ResourceView } from './resourceView.js';
import { DebugOutputContext, type DebugOutputRenderer } from '../../../web/src/components/nodeDebug/NodeDebugConsoleView.js';
import { createDebugOutputRenderer } from '../debug/output.js';
import { WorkspaceCodePreviewContext, type WorkspaceCodePreviewRenderer } from '../../../web/src/components/workspace/preview/WorkspaceCodePreview.js';
import { createCodePreviewRenderer } from './codePreview.js';
import { ChatItemActions } from './itemActions.js';
import { ChatHoverContext, ChatItemMenuContext, ContextNoticeContinuationContext } from '../../../web/src/components/chat/itemContextMenu.js';
import { ChatItemMenu } from './chatItemMenu.js';
import type { IInstantiationService } from '../../upstream/src/vs/platform/instantiation/common/instantiation.js';
import { productStyles } from './productStyles.js';
import viewStyle from './chatView.css?text';
import { WorkspaceFileReferenceProvider } from '../../../web/src/components/workspace/WorkspaceFileReferenceContext.js';
import { resourceFileRootUri } from '../../../web/src/utils/workspaceFileReferences.js';
import { workspaceFileUri } from '../workspaceFiles.js';
import { resizeThreadResources } from '../../../web/src/layout/workbenchLayout.js';
import { installPointerDrag } from '../../../web/src/utils/pointerDrag.js';
import type { FileOpenMenuHandler } from '../../../web/src/components/workspace/fileTree/WorkspaceFileTreeContextMenu.js';
import { threadHistoryCache, type ThreadHistoryOwner } from '../../../web/src/state/threadHistoryCache.js';
import type { SessionTurnTimeline } from '../../../web/src/state/session/turnTimeline.js';
import { HttpRequestError } from '../../../web/src/api/http.js';
import { CachedThreadHistory } from './cachedThreadHistory.js';
import type { SessionForkSourceSelection } from '../../../web/src/hooks/shell/useSessionForkDialog.js';
import type { ICommandService } from '../../upstream/src/vs/platform/commands/common/commands.js';
import { sessionActionCommands } from '../search/sessionActions.js';
import { taskActionCommand } from '../automation/actions.js';
import type { InternalDisplayProvenance } from '../../../web/src/types/backend.js';


type SearchNavigation = Exclude<ThreadLocation, { kind: 'viewport' }>;
interface ThreadViewportController extends TranscriptViewportController {
  capturePresentation(): () => void;
  showConversation(preview?: boolean): void;
}

function ThreadConversation({ input, seed, active, editors, groupId, commands, onPickModel, onUpdate, onFileOpenMenu, onError, navigation, onViewportReady, onDraftReady, onPositionChange, onCloseNavigation, toolbar }: {
  input: ThreadInput; seed: ThreadSessionSeed; active: boolean; editors: IEditorService; groupId: number; onUpdate: () => void;
  commands: ICommandService;
  onPickModel(request: ModelPickerRequest): Promise<void>;
  onFileOpenMenu: FileOpenMenuHandler;
  onError: (message: string) => void;
  navigation?: SearchNavigation;
  onViewportReady: (controller: ThreadViewportController | undefined) => void;
  onDraftReady: (controller: ComposerDraftController | undefined) => void;
  onPositionChange: (position: TranscriptViewportPosition, reason: TranscriptViewportChangeReason) => void;
  onCloseNavigation: () => void;
  toolbar: ReactNode;
}) {
  const owner = input.owner;
  const messageNavigation = navigation?.kind === 'message' ? navigation.match : undefined;
  const requestLogNavigation = navigation?.kind === 'request' ? navigation.match : undefined;
  const traceNavigation = navigation?.kind === 'trace' ? navigation.match : undefined;
  const [reportRequest, setReportRequest] = useState<InternalDisplayProvenance | null>(null);
  const [resourceRatio, setResourceRatio] = useState(input.resourceRatio);
  const stopResize = useRef<(() => void) | null>(null);
  useEffect(() => () => stopResize.current?.(), []);
  const onSessionCreated = useCallback(async (session: Session, workspaceKey: string | null) => {
    if (workspaceKey !== owner.workspaceKey || session.workspace_id !== owner.workspaceId || !session.thread_id) throw new Error(t('thread.host.newSessionOwnerMismatch'));
    await editors.openEditor(new ThreadInput({ ...owner, sessionId: session.session_id, threadId: session.thread_id, title: session.title }), { pinned: true, sourceGroup: groupId });
  }, [owner, editors, groupId]);
  const runtime = useThreadSessionRuntime({ owner, seed, active, onSessionCreated, onError });
  const { timeline, history, actions, state, content } = runtime;
  const requestNavigation = useRequestLogNavigation(0, owner.workspaceKey, owner.sessionId, requestLogNavigation);
  const traceLocation = useTraceNavigation(0, owner.workspaceKey, owner.sessionId, owner.threadId, traceNavigation);
  const [outlinePreview, setOutlinePreview] = useState(false);
  const contentView = outlinePreview || navigation?.kind === 'message' ? 'default' : traceLocation ? 'events' : requestNavigation ? 'requests' : state.contentView;
  const savePosition = useCallback((position: TranscriptViewportPosition, reason: TranscriptViewportChangeReason) => {
    if (reason !== 'layout') runtime.views.saveSessionViewState(position);
    if (contentView === 'default') onPositionChange(position, reason);
  }, [runtime.views.saveSessionViewState, onPositionChange, contentView]);
  const showConversation = useCallback((preview = false) => {
    if (preview) { setOutlinePreview(true); return; }
    if (state.contentView !== 'default') void content.switchContentView('default').catch((error: unknown) => onError(String(error)));
    setOutlinePreview(false);
  }, [content.switchContentView, onError, state.contentView]);
  const connectViewport = useCallback((controller: TranscriptViewportController | undefined) => {
    onViewportReady(controller ? { ...controller,
      // 隐藏聊天时不把零尺寸 DOM 当作阅读位置；仍保留 controller 供大纲临时预览。
      capture: () => contentView === 'default' ? controller.capture() : undefined,
      capturePresentation: () => () => setOutlinePreview(outlinePreview),
      showConversation,
    } : undefined);
  }, [contentView, onViewportReady, outlinePreview, showConversation]);
  const fork = (source: SessionForkSourceSelection) => {
    void commands.executeCommand(sessionActionCommands.fork, { owner: { gatewayId: owner.gatewayId, workspaceId: owner.workspaceId },
      sessionId: owner.sessionId, source }).catch(cause => onError(String(cause)));
  };
  useEffect(() => { if (state.status) console.info('会话状态：', state.status); }, [state.status]);
  useEffect(() => {
    if (state.contentView !== 'changes' && state.contentView !== 'resources') return;
    input.resourceTab = state.contentView === 'changes' ? 'changes' : 'connections';
    input.resourcesVisible = true;
    onUpdate();
  }, [input, onUpdate, state.contentView]);
  const openChanges = () => { input.resourcesVisible = true; input.resourceTab = 'changes'; onUpdate(); };
  if (timeline?.restoredFromCache) return <div className="native-thread-conversation">{toolbar}<CachedThreadHistory owner={owner} timeline={timeline}
    error={timeline.error}
    onRetry={history.refreshTurnHistory} /></div>;
  return <WarmConfirmProvider><ContextNoticeContinuationContext.Provider value={history.loadMoreContext}><ComposerContext.Provider value={runtime.composerValue}>
    <WorkspaceFileReferenceProvider apiPort={0} workspaceId={owner.workspaceKey} onOpen={(file, reference) => {
      const root = resourceFileRootUri(file.path);
      if (!root) throw new Error(t('thread.host.referenceRootMissing'));
      void editors.openEditor({ resource: workspaceFileUri(owner, file.path, root), options: {
        sourceGroup: groupId,
        selection: reference.selection ? { startLineNumber: reference.selection.startLine, startColumn: reference.selection.startColumn ?? 1,
          endLineNumber: reference.selection.endLine, endColumn: 1 } : undefined,
      } }).catch((cause: unknown) => onError(t('thread.host.openReferenceFailed', String(cause))));
    }}>
    <div className="native-thread-content">{toolbar}
    <div className={`native-thread-layout${input.resourcesVisible ? ' has-resources' : ''}`}><div className="native-thread-conversation" style={{ flexGrow: input.resourcesVisible ? 1 - resourceRatio : 1 }}>
      {contentView === 'agent' || contentView === 'requests' ? <div className="native-inspection-scope" role="note">
        {t(contentView === 'agent' ? 'thread.host.mainContextScope' : 'thread.host.requestScope', state.currentSession?.title ?? owner.title)}
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
        traceNavigation={traceLocation && { ...traceLocation, recordOnly: navigation?.kind === 'trace' && navigation.recordOnly }} onCloseTraceNavigation={onCloseNavigation}
        requestLogNavigation={requestNavigation && { ...requestNavigation, recordOnly: navigation?.kind === 'request' && navigation.recordOnly }} onCloseRequestLogNavigation={onCloseNavigation}
        onRetryRequestLogs={() => { void content.refreshLLMRequestLogs(owner.sessionId); }}
        conversations={runtime.conversations} activeTurnTimeline={timeline}
        changesHint={null} changesHintLoading={false} activeChangeset={state.activeChangeset}
        sessionViewStates={state.sessionViewStates}
        onLoadAroundTurn={history.loadAroundTurn} onLoadNewerMessages={history.loadNewerTurns} onLoadLatestMessages={history.loadLatestTurns} onLoadOlderMessages={history.loadOlderTurns}
        messageNavigation={messageNavigation}
        onCloseMessageNavigation={onCloseNavigation}
        onViewportReady={connectViewport}
        onViewStateChange={savePosition} onViewStateRestoreStatus={runtime.setStatus}
        onLoadTurnDetails={history.loadTurnDetails}
        onRetainTurnBodies={history.retainTurnBodies}
        onLoadAgentStateMessageRawContent={content.loadAgentStateMessageRawContent}
        onRetryHistory={history.refreshTurnHistory} onReplayTurn={actions.replayTurn} onOpenChanges={openChanges}
        onForkConversation={point => {
          if (point.workspaceId !== owner.workspaceKey || point.sessionId !== owner.sessionId || point.threadId !== owner.threadId) throw new Error(t('thread.host.forkOwnerMismatch'));
          fork(point);
        }}
        onOpenChildReport={source => {
          if (source.source_session_id !== owner.sessionId || !source.source_thread_id || !source.source_execution_id) throw new Error(t('thread.host.reportOwnerMismatch'));
          setReportRequest(source); input.resourcesVisible = true; input.resourceTab = 'threads'; onUpdate();
        }}
        onOpenAttachment={(_sessionId, value) => {
          input.selectedAttachment = { file_id: value.file_id, directory_uri: value.directory_uri,
            name: value.name ?? undefined, content_type: value.content_type ?? undefined };
          input.resourcesVisible = true; input.resourceTab = 'files'; onUpdate();
        }}
        onRemovePending={actions.removePendingRequest} onChangePendingPolicy={actions.updatePendingRequestPolicy} />
      <Composer threadId={owner.threadId} onDraftReady={onDraftReady} onPickModel={onPickModel}
        status={{ conversations: runtime.conversations, timeline,
          tasks: { ...runtime.persistentTasks,
            onManage: () => { input.resourcesVisible = true; input.resourceTab = 'tasks'; onUpdate(); },
            onAction: (task, action) => commands.executeCommand(taskActionCommand(action), {
              owner: { gatewayId: owner.gatewayId, workspaceId: owner.workspaceId }, task,
            }),
          },
          questionOwner: { port: 0, workspaceKey: owner.workspaceKey, sessionId: owner.sessionId, threadId: owner.threadId },
          active, queueActions: { onClear: actions.clearPendingRequests,
            onRemove: actions.removePendingRequest, onChangePolicy: actions.updatePendingRequestPolicy, onReorder: actions.reorderPendingRequests } }} onManageConfiguration={(kind, id, modelId) => {
        void commands.executeCommand(kind === 'providers' ? 'boxteam.settings.modelsProviders' : 'boxteam.settings.agents',
          { gatewayId: owner.gatewayId, workspaceId: owner.workspaceId }, id, modelId)
          .catch(error => onError(error instanceof Error ? error.message : String(error)));
      }} />
    </div>{input.resourcesVisible ? <button type="button" className="layout-sash" aria-label={t('thread.host.resizeResources')}
      role="separator" aria-orientation="vertical" aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(resourceRatio * 100)}
      title={t('thread.host.resizeResourcesHint')} onDoubleClick={() => { input.resourceRatio = 0.42; setResourceRatio(0.42); }}
      onKeyDown={event => {
        if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key) || event.ctrlKey || event.altKey || event.metaKey) return;
        event.preventDefault(); event.stopPropagation();
        const layout = event.currentTarget.parentElement!;
        const leftWidth = layout.querySelector<HTMLElement>('.native-thread-conversation')!.getBoundingClientRect().width;
        const rightWidth = layout.querySelector<HTMLElement>('.native-thread-resources')!.getBoundingClientRect().width;
        const deltaX = event.key === 'Home' ? -leftWidth : event.key === 'End' ? rightWidth : event.key === 'ArrowLeft' ? -20 : 20;
        const ratio = resizeThreadResources({ leftWidth, rightWidth, deltaX });
        input.resourceRatio = ratio; setResourceRatio(ratio);
      }}
      onPointerDown={event => {
        event.preventDefault(); stopResize.current?.();
        const layout = event.currentTarget.parentElement!;
        const leftWidth = layout.querySelector<HTMLElement>('.native-thread-conversation')!.getBoundingClientRect().width;
        const rightWidth = layout.querySelector<HTMLElement>('.native-thread-resources')!.getBoundingClientRect().width;
        const startX = event.clientX;
        stopResize.current = installPointerDrag('is-layout-resizing', move => {
          const ratio = resizeThreadResources({ leftWidth, rightWidth, deltaX: move.clientX - startX });
          input.resourceRatio = ratio; setResourceRatio(ratio);
        }, () => { stopResize.current = null; });
      }} /> : null}
      <ResourceView input={input} runtime={runtime} editors={editors} groupId={groupId} commands={commands} active={active} attachment={input.selectedAttachment ?? null} ratio={resourceRatio} onUpdate={onUpdate} onFileOpenMenu={onFileOpenMenu} onError={onError}
        reportRequest={reportRequest} onForkThread={(threadId, point) => fork({ ...point, threadId })} /></div></div>
    </WorkspaceFileReferenceProvider>
  </ComposerContext.Provider></ContextNoticeContinuationContext.Provider></WarmConfirmProvider>;
}

function ChatView({ input, initialSession, initialWorkspaces, active, editors, groupId, commands, onPickModel, onUpdate, onFileOpenMenu, onError, onChooseThread, navigation, onViewportReady, onDraftReady, onPositionChange, onCloseNavigation }: { input: ThreadInput; initialSession?: Session; initialWorkspaces: GatewayWorkspace[]; active: boolean; editors: IEditorService; groupId: number; commands: ICommandService; onPickModel(request: ModelPickerRequest): Promise<void>; onUpdate: () => void; onFileOpenMenu: FileOpenMenuHandler; onError: (message: string) => void; onChooseThread: () => Promise<void>; navigation?: SearchNavigation; onViewportReady: (controller: ThreadViewportController | undefined) => void; onDraftReady: (controller: ComposerDraftController | undefined) => void; onPositionChange: (position: TranscriptViewportPosition, reason: TranscriptViewportChangeReason) => void; onCloseNavigation: () => void }) {
  const owner = input.owner;
  const [seed, setSeed] = useState<ThreadSessionSeed | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [cached, setCached] = useState<SessionTurnTimeline | null>(null);
  const [cacheError, setCacheError] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    let disposed = false;
    const controller = new AbortController();
    let cacheOwner: ThreadHistoryOwner | undefined;
    setError(null);
    const cacheLoad = Promise.resolve().then(async () => {
      cacheOwner = { gatewayId: owner.gatewayId, workspaceId: owner.workspaceId, sessionId: owner.sessionId, threadId: owner.threadId };
      const timeline = await threadHistoryCache.read(cacheOwner, JSON.stringify([owner.workspaceKey, owner.sessionId, owner.threadId]));
      if (!disposed) setCached(timeline);
      return timeline;
    }).catch((cause: unknown) => {
      if (!disposed) setCacheError(t('thread.host.readCacheFailed', String(cause)));
      return null;
    });
    const initialize = async () => {
      // 历史首读与宿主初始化并行；沿原 seed 挂载 Composer，不等待历史响应才能输入。
      const bootstrap = getSessionTurnBootstrap(0, owner.sessionId, owner.workspaceKey, controller.signal, owner.threadId).then(value => {
        if (value.session.session_id !== owner.sessionId || value.session.workspace_id !== owner.workspaceId
          || value.thread_id !== owner.threadId) throw new Error(t('thread.host.initializeOwnerMismatch'));
        return value;
      });
      const seed = Promise.all([
        // 目录点击的权威 Session 只用于首次初始化；URI、恢复和重试仍真实读取。
        attempt === 0 && initialSession ? initialSession : getSession(0, owner.sessionId, owner.workspaceKey), listAgents(0, owner.workspaceKey),
        // 首次复用所属 Pane 刚校验的真实目录；显式重试仍重读登记。
        attempt === 0 ? initialWorkspaces : gateway.workspaces(false, controller.signal).then(result => result.items), getBrowserUiSettings(0), browserGatewayId(0),
      ]).then(async ([session, agents, workspaces, settings, browserViewScope]) => {
        if (session.session_id !== owner.sessionId || session.workspace_id !== owner.workspaceId) throw new Error(t('thread.host.initializeOwnerMismatch'));
        const history = await cacheLoad;
        if (!disposed) setSeed({ session, bootstrap, agents, workspaces, settings, browserViewScope, history });
      });
      // 首读可能先于挂载失败；原历史 owner 消费同一拒绝并保留只读缓存及重试出口。
      await Promise.all([seed, bootstrap.catch(() => undefined)]);
    };
    void initialize().catch(async (cause: unknown) => {
      // 已删除的会话不能继续显示浏览器旧记录，清除镜像后明确显示后端错误。
      if (cause instanceof HttpRequestError && (cause.status === 404 || cause.status === 410)) {
        await cacheLoad;
        if (!disposed) setCached(null);
        if (cacheOwner) await threadHistoryCache.remove(cacheOwner).catch((failure: unknown) => {
          if (!disposed) setCacheError(t('thread.host.clearCacheFailed', String(failure)));
        });
      }
      if (!disposed) setError(cause instanceof Error ? cause.message : String(cause));
    });
    return () => { disposed = true; controller.abort(); };
  }, [owner, initialSession, initialWorkspaces, attempt]);
  const retry = () => setAttempt(value => value + 1);
  const toolbar = <header className="native-thread-toolbar" role="toolbar" aria-label={t('thread.host.toolbar')}>
    <button type="button" className="native-thread-picker" title={t('thread.host.selectThread')} aria-label={t('thread.host.selectThread')}
      onClick={() => { void onChooseThread().catch((cause: unknown) => onError(String(cause))); }}>
      <span className="codicon codicon-comment-discussion" aria-hidden="true" /><span className="native-thread-picker-label">{input.getName()}</span>
      <span className="codicon codicon-chevron-down" aria-hidden="true" />
    </button>
    <button type="button" title={t('thread.host.toggleResources')} aria-label={t('thread.host.toggleResources')} aria-expanded={input.resourcesVisible}
      onClick={() => { input.resourcesVisible = !input.resourcesVisible; onUpdate(); }}>
      <span className="codicon codicon-layout-sidebar-right" aria-hidden="true" />
    </button>
  </header>;
  return <div className="thread-history-host panel-feedback-host">{cacheError ? <p className="panel-feedback" role="alert">{cacheError}</p> : null}
    {seed ? <ThreadConversation input={input} seed={seed} active={active} editors={editors} groupId={groupId} commands={commands} onPickModel={onPickModel} onUpdate={onUpdate} onFileOpenMenu={onFileOpenMenu} onError={onError} navigation={navigation} onViewportReady={onViewportReady} onDraftReady={onDraftReady} onPositionChange={onPositionChange} onCloseNavigation={onCloseNavigation} toolbar={toolbar} />
      : <div className="native-thread-conversation">{toolbar}{cached ? <CachedThreadHistory owner={owner} timeline={cached} error={error ? t('thread.host.syncFailed', error) : null} onRetry={retry} />
        : error ? <p role="alert">{t('thread.host.initializeFailed', error)}</p> : <p role="status">{t('thread.host.restoring')}</p>}</div>}</div>;
}

export class ChatSurface {
  private readonly log: ILogService;
  private readonly surface: ReactSurface;
  private readonly renderDebugOutput: DebugOutputRenderer;
  private readonly renderCodePreview: WorkspaceCodePreviewRenderer;
  private readonly modelPicker: ComposerModelPicker;
  private readonly itemMenu: ChatItemMenu;
  private readonly itemActions: ChatItemActions;
  private active: boolean;
  private location?: ThreadLocation;
  private viewport?: ThreadViewportController;
  private draft?: ComposerDraftController;
  private previewGeneration = 0;
  private previewPosition?: TranscriptViewportPosition;
  private pendingNavigation?: { position: TranscriptViewportPosition; preview: boolean };
  constructor(container: HTMLElement, private readonly input: ThreadInput, private readonly editors: IEditorService, visible: boolean,
    private readonly onFileOpenMenu: FileOpenMenuHandler,
    private readonly notifications: INotificationService,
    private readonly commands: ICommandService,
    private readonly initialWorkspaces: GatewayWorkspace[],
    instantiation: IInstantiationService,
    private readonly groupId: number,
    private readonly onChooseThread: () => Promise<void>,
    private readonly onSelectionChange: (reason: EditorPaneSelectionChangeReason) => void,
    private readonly initialSession?: Session) {
    this.log = instantiation.invokeFunction(accessor => accessor.get(ILogService));
    this.renderDebugOutput = createDebugOutputRenderer(instantiation);
    this.renderCodePreview = createCodePreviewRenderer(instantiation);
    this.modelPicker = instantiation.createInstance(ComposerModelPicker);
    this.itemMenu = instantiation.createInstance(ChatItemMenu);
    this.itemActions = instantiation.createInstance(ChatItemActions);
    this.surface = new ReactSurface(container, [...productStyles, viewStyle]);
    this.active = visible; this.render();
  }
  captureDraft(): ComposerDraftController {
    if (!this.draft) throw new Error(t('thread.host.draftNotReady'));
    return this.draft;
  }
  update(): void { this.render(); }
  getLocation(): ThreadLocation | undefined { return this.location; }
  revealLocation(location: ThreadLocation, reason = EditorPaneSelectionChangeReason.NAVIGATION): void {
    // 在真正跳转前刷新来源位置；预览期间使用已捕获的原位置。
    if (reason === EditorPaneSelectionChangeReason.NAVIGATION && (!this.location || this.location.kind === 'viewport')) {
      const position = this.previewPosition ?? this.viewport?.capture();
      if (position) {
        this.location = { kind: 'viewport', position };
        this.onSelectionChange(EditorPaneSelectionChangeReason.USER);
      }
    }
    this.location = structuredClone(location);
    this.previewPosition = undefined;
    this.pendingNavigation = undefined;
    this.onSelectionChange(reason);
    if (location.kind === 'viewport') {
      // 原 viewState 恢复只还原阅读位置；不能切走已有日志页或触发主动定位提示。
      const restoring = reason === EditorPaneSelectionChangeReason.PROGRAMMATIC;
      if (this.viewport) {
        if (!restoring) this.viewport.showConversation();
        this.viewport.navigate(location.position, restoring);
      } else this.pendingNavigation = { position: location.position, preview: restoring };
    } else if (location.kind === 'message') this.viewport?.showConversation();
    this.render();
  }
  private readonly onCloseNavigation = () => {
    this.location = undefined;
    this.pendingNavigation = undefined;
    const position = this.viewport?.capture();
    if (position) this.onPositionChange(position, 'layout');
    this.render();
  };
  setVisible(visible: boolean): void { if (!visible) { this.itemMenu.hide(); this.modelPicker.hide(); } this.active = visible; this.render(); }
  captureViewState(): IDisposable {
    const position = this.requireViewport().capture();
    this.previewPosition = position;
    const restore = this.requireViewport().capturePresentation();
    return toDisposable(() => {
      // 原 Provider 随后才释放旧预览；恢复属于新导航，不能被旧释放动作取消。
      ++this.previewGeneration;
      this.previewPosition = undefined;
      if (position) {
        if (this.viewport) this.viewport.navigate(position, true);
        else this.pendingNavigation = { position, preview: true };
      }
      restore();
    });
  }
  revealTurn(turnId: string, preview = false): IDisposable {
    const generation = ++this.previewGeneration;
    const position: TranscriptViewportPosition = { reading_anchor: {
      node_key: nodeKey({ kind: 'turn', id: turnId }, 'header'),
      content_position: { block: 0, offset_px: 0 }, viewport_offset_px: 0,
    }, follow_latest: false };
    const viewport = this.requireViewport();
    if (preview) { viewport.showConversation(true); viewport.navigate(position, true); }
    else this.revealLocation({ kind: 'viewport', position });
    return toDisposable(() => { if (generation === this.previewGeneration) this.viewport?.cancel(); });
  }
  private requireViewport(): ThreadViewportController {
    if (!this.viewport) throw new Error(t('thread.host.viewportNotReady'));
    return this.viewport;
  }
  private configurationControl?: 'providers' | 'agents';
  private readonly onDraftReady = (controller: ComposerDraftController | undefined) => {
    this.draft = controller;
    if (controller && this.configurationControl) this.focusConfigurationControl(this.configurationControl);
  };
  private readonly onViewportReady = (controller: ThreadViewportController | undefined) => {
    this.viewport = controller;
    if (!controller) return;
    if (this.pendingNavigation) {
      const { position, preview } = this.pendingNavigation; this.pendingNavigation = undefined;
      if (!preview) controller.showConversation();
      controller.navigate(position, preview);
    } else if (this.location?.kind === 'message') controller.showConversation();
    else if (!this.location) {
      const position = controller.capture();
      if (position) this.onPositionChange(position, 'layout');
    }
  };
  private readonly onPositionChange = (position: TranscriptViewportPosition, reason: TranscriptViewportChangeReason) => {
    // 定位完成和布局保持不覆盖精确 Item／日志引用；普通滚动只更新原历史当前项。
    if (reason !== 'scroll' && this.location && this.location.kind !== 'viewport') return;
    const wasNavigation = this.location && this.location.kind !== 'viewport';
    this.location = { kind: 'viewport', position };
    this.onSelectionChange(EditorPaneSelectionChangeReason.PROGRAMMATIC);
    if (wasNavigation) this.render();
  };
  focus(): void { this.surface.focus('textarea'); }
  focusConfigurationControl(kind: 'providers' | 'agents'): void {
    this.configurationControl = this.surface.focus(kind === 'providers' ? '.composer-model-pill' : '.composer-agent-button') ? undefined : kind;
  }
  dispose(): void { this.modelPicker.dispose(); this.itemMenu.dispose(); this.itemActions.dispose(); this.viewport?.cancel(); this.viewport = undefined; this.draft = undefined; this.surface.dispose(); }
  private readonly onPickModel = (request: ModelPickerRequest) => this.modelPicker.show(this.input.owner, request);
  private readonly onUpdate = () => this.render();
  private readonly onError = (message: string) => this.notifications.error(t('thread.host.ownerError', this.input.owner.title, this.input.owner.sessionId, this.input.owner.threadId, message));
  private readonly reportComposerDiagnostic = (message: string) => this.log.error(
    `[workspace=${this.input.owner.workspaceId} session=${this.input.owner.sessionId} thread=${this.input.owner.threadId}] ${message}`);
  private readonly showItemMenu = (request: import('../../../web/src/components/chat/itemContextMenu.js').ChatItemMenuRequest): void => {
    this.itemMenu.show({ ...request, actions: [...request.actions, ...this.itemActions.actions(request, this.input.owner, this.groupId, (result, owner) => {
      if (owner.sessionId !== this.input.owner.sessionId || owner.threadId !== this.input.owner.threadId) {
        const options: import('../threadPane.js').ThreadEditorOptions = { boxteamLogRecord: true,
          ...(result.request_log ? { boxteamRequestLog: result.request_log } : { boxteamTrace: result.trace }) };
        void this.editors.openEditor(new ThreadInput(owner), options, this.groupId).catch((cause: unknown) => this.onError(String(cause)));
      } else if (result.request_log) this.revealLocation({ kind: 'request', match: result.request_log, recordOnly: true });
      else if (result.trace) this.revealLocation({ kind: 'trace', match: result.trace, recordOnly: true });
    })] });
  };
  private render(): void { this.surface.render(<ChatHoverContext.Provider value={this.itemMenu.bindHover}><ChatItemMenuContext.Provider value={this.showItemMenu}><ComposerDiagnosticsContext.Provider value={this.reportComposerDiagnostic}><WorkspaceCodePreviewContext.Provider value={this.renderCodePreview}><DebugOutputContext.Provider value={this.renderDebugOutput}><ChatView input={this.input} initialSession={this.initialSession} initialWorkspaces={this.initialWorkspaces} editors={this.editors} groupId={this.groupId} commands={this.commands} onPickModel={this.onPickModel} active={this.active} onUpdate={this.onUpdate} onFileOpenMenu={this.onFileOpenMenu} onError={this.onError} onChooseThread={this.onChooseThread} navigation={this.location?.kind === 'viewport' ? undefined : this.location} onViewportReady={this.onViewportReady} onDraftReady={this.onDraftReady} onPositionChange={this.onPositionChange} onCloseNavigation={this.onCloseNavigation} /></DebugOutputContext.Provider></WorkspaceCodePreviewContext.Provider></ComposerDiagnosticsContext.Provider></ChatItemMenuContext.Provider></ChatHoverContext.Provider>); }
}
