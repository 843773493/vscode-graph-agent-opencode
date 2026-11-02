import { useCallback, useEffect, useState } from 'react';
import WorkspaceEditorHeader from '../../../web/src/components/workspace/WorkspaceEditorHeader.js';
import WorkspaceAuxiliaryPanel, { type WorkspaceAuxiliaryTab } from '../../../web/src/components/workspace/WorkspaceAuxiliaryPanel.js';
import ResourcePanel from '../../../web/src/components/panels/resourceTree/ResourcePanel.js';
import WorkspaceAttachmentPreview from '../../../web/src/components/workspace/preview/WorkspaceAttachmentPreview.js';
import WorkspaceRuntimePreviewArea, { type WorkspaceRuntimePreviewTab } from '../../../web/src/components/workspace/WorkspaceRuntimePreviewArea.js';
import { useThreadSessionRuntime } from '../../../web/src/hooks/session/useThreadSessionRuntime.js';
import { useSessionResourcePolling } from '../../../web/src/hooks/session/useSessionResourceLoader.js';
import { resourceThreadAssociations } from '../../../web/src/state/display/workspaceResourceDisplay.js';
import { createSessionConnection } from '../../../web/src/api/gateway/sessionConnections.js';
import { getWorkspaceResource, controlWorkspaceResource } from '../../../web/src/api/workspaceResources.js';
import { buildGatewayAttachUrl } from '../../../web/src/utils/attachUrls.js';
import { resourceFileRootUri } from '../../../web/src/utils/workspaceFileReferences.js';
import { workspaceFileUri } from '../workspaceFiles.js';
import { changeFileUri } from '../changeFiles.js';
import { ThreadInput } from '../threadInput.js';
import type { AttachmentRef, InternalDisplayProvenance } from '../../../web/src/types/backend.js';
import type { ChildThreadForkPoint } from '../../../web/src/components/panels/sessionPanels/ChildThreadConversation.js';
import type { IEditorService } from '../../upstream/src/vs/workbench/services/editor/common/editorService.js';
import { TaskView } from './taskView.js';
import NodeDebugWorkbench from '../../../web/src/components/nodeDebug/NodeDebugWorkbench.js';
import { useNodeDebugController } from '../../../web/src/hooks/nodeDebug/useNodeDebugController.js';
import { revealWorkspaceFileEntry } from '../../../web/src/api/workspaceFilesystem.js';
import ChildThreadPanel from '../../../web/src/components/panels/sessionPanels/ChildThreadPanel.js';
import { useChildThreadLoader } from '../../../web/src/hooks/session/useChildThreadLoader.js';
import { useChildThreadConversation } from '../../../web/src/hooks/sessionTurnHistory/useChildThreadConversation.js';
import WorkspaceFilePreviewArea from '../../../web/src/components/workspace/WorkspaceFilePreviewArea.js';
import { useWorkspacePreviewTabs } from '../../../web/src/hooks/workspace/useWorkspacePreviewTabs.js';
import type { FileOpenMenuHandler } from '../../../web/src/components/workspace/fileTree/WorkspaceFileTreeContextMenu.js';
import type { WebUiLayoutSettings } from '../../../web/src/types/backend.js';

type Runtime = ReturnType<typeof useThreadSessionRuntime>;
const resourceTab: Record<ThreadInput['resourceTab'], WorkspaceAuxiliaryTab> = { files: 'files', changes: 'changes', tasks: 'goal', connections: 'resources', debug: 'debug' };
function reportResourceStatus(message: string): void { console.info('会话资源：', message); }

/** 文件树、变更审阅和资源操作均使用旧前端完整组件。 */
export function ResourceView({ input, runtime, editors, active, attachment, ratio, onUpdate, onFileOpenMenu, reportRequest, onForkThread, onError }: {
  input: ThreadInput; runtime: Runtime; editors: IEditorService; active: boolean; attachment: AttachmentRef | null; ratio: number; onUpdate: () => void;
  onFileOpenMenu: FileOpenMenuHandler;
  reportRequest: InternalDisplayProvenance | null;
  onForkThread: (threadId: string, point: ChildThreadForkPoint) => void;
  onError: (message: string) => void;
}) {
  const owner = input.owner;
  const { state, content } = runtime;
  const tab = resourceTab[input.resourceTab];
  const [order, setOrder] = useState<WorkspaceAuxiliaryTab[]>(['files', 'changes', 'goal', 'debug', 'resources']);
  const [search, setSearch] = useState(false);
  const [collapse, setCollapse] = useState(0);
  const [fileTreeSettingsVersion, setFileTreeSettingsVersion] = useState(0);
  const [expanded, setExpanded] = useState(input.fileTreeExpandedPaths);
  const persistExpanded = (paths: string[]) => { input.fileTreeExpandedPaths = paths; setExpanded(paths); };
  const [preview, setPreview] = useState<WorkspaceRuntimePreviewTab | null>(null);
  const thread = { session_id: owner.sessionId, thread_id: owner.threadId };
  const visible = input.resourcesVisible && active;
  const [markdownSource, setMarkdownSource] = useState(false);
  const persistPreview = useCallback((layout: WebUiLayoutSettings) => { input.filePreviewLayout = layout; }, [input]);
  const filePreview = useWorkspacePreviewTabs({ apiPort: 0, workspaceId: owner.routeId,
    settingsLoaded: state.uiSettingsLoaded, restoredLayout: input.filePreviewLayout,
    onPersistLayout: persistPreview, onStatusChange: reportResourceStatus });
  const filePreviewVisible = tab === 'files' && filePreview.visible;
  const debug = useNodeDebugController({ apiPort: 0, workspaceId: owner.routeId, sessionId: owner.sessionId,
    threadId: owner.threadId, enabled: visible && tab === 'debug', onStatusChange: reportResourceStatus });
  const children = useChildThreadLoader({ apiPort: 0, workspaceId: owner.routeId, sessionId: owner.sessionId, enabled: visible && tab === 'resources' });
  const childConversation = useChildThreadConversation({ apiPort: 0, workspaceId: owner.routeId, sessionId: owner.sessionId,
    threads: children.threads, enabled: visible && tab === 'resources' });
  const { selectThreadForOwner } = childConversation;
  useEffect(() => {
    if (!reportRequest) return;
    if (reportRequest.source_session_id !== owner.sessionId || !reportRequest.source_thread_id || !reportRequest.source_execution_id) throw new Error('报告请求与会话资源归属不一致');
    selectThreadForOwner({ workspaceId: owner.routeId, sessionId: owner.sessionId }, reportRequest.source_thread_id, 'report', reportRequest.source_execution_id);
  }, [reportRequest, owner.routeId, owner.sessionId, selectThreadForOwner]);
  useEffect(() => {
    if (!visible) return;
    if (tab === 'changes') void content.refreshSessionChanges(owner.sessionId);
  }, [tab, visible, owner.sessionId, content.refreshSessionChanges]);
  useSessionResourcePolling({ active: visible && tab === 'resources', workspaceId: owner.routeId,
    sessionId: owner.sessionId, refresh: content.refreshSessionResources, onStatusChange: reportResourceStatus });
  const openRuntime = (kind: 'terminal' | 'browser', id: string) => {
    const path = `${kind}://${id}`;
    const base = { path, name: id, attachUrl: buildGatewayAttachUrl(kind, owner.routeId, id, true, thread) };
    setPreview(kind === 'terminal' ? { ...base, previewType: kind, terminalId: id } : { ...base, previewType: kind, browserId: id });
    input.resourceTab = 'connections'; onUpdate();
  };
  const resourceItems = state.sessionResources.filter(item => {
    if (item.kind === 'terminal' || item.kind === 'browser') return resourceThreadAssociations(item).some(candidate => candidate.session_id === owner.sessionId && candidate.thread_id === owner.threadId);
    return item.metadata.thread_id === owner.threadId || (!item.metadata.thread_id && state.currentSession?.thread_id === owner.threadId);
  });
  const openDebugThread = (threadId: string) => {
    void editors.openEditor(new ThreadInput({ ...owner, threadId, title: threadId }), {
      pinned: true, viewState: { resourcesVisible: true, resourceTab: 'debug' },
    }).catch((cause: unknown) => onError(`打开调试 Thread 失败：${String(cause)}`));
  };
  return <div className="native-thread-resources" hidden={!input.resourcesVisible} style={{ flexGrow: ratio }}>
    <WorkspaceEditorHeader auxiliaryTab={tab} tabOrder={order} onReorderAuxiliaryTabs={setOrder} onSelectAuxiliaryTab={value => {
      input.resourceTab = value === 'goal' ? 'tasks' : value === 'resources' ? 'connections' : value;
      onUpdate();
    }} />
    <div className={`workspace-editor-body${filePreviewVisible ? ' has-shared-preview' : ''}`}>
    {filePreviewVisible ? <WorkspaceFilePreviewArea context="files" visible flexRatio={1} apiPort={0}
      workspaceId={owner.routeId} workspaceName={state.gatewayWorkspaces.find(item => item.workspace_id === owner.routeId)?.name ?? ''}
      sessionTitle={state.currentSession?.title ?? owner.title} tabs={filePreview.tabs} activePath={filePreview.activePath}
      loadingPath={filePreview.loadingPath} error={filePreview.error} editingPath={filePreview.editingPath}
      draftContent={filePreview.draftContent} savingPath={filePreview.savingPath} hasUnsavedEdit={filePreview.hasUnsavedEdit}
      markdownSourceVisible={markdownSource} onMarkdownSourceChange={setMarkdownSource}
      onBeginEdit={filePreview.beginWorkspaceFileEdit} onDraftChange={filePreview.setDraftContent}
      onCancelEdit={() => void filePreview.cancelWorkspaceFileEdit()} onSaveEdit={filePreview.saveWorkspaceFileEdit}
      onOpenWorkspacePath={filePreview.openWorkspaceFilePath}
      onClose={() => { if (filePreview.activePath) void filePreview.closeWorkspaceFilePreview(filePreview.activePath); }} /> : null}
    <WorkspaceAuxiliaryPanel visible={input.resourcesVisible} flexRatio={1} tab={tab} apiPort={0} workspaceId={owner.routeId}
      workspaceFileTreeReady={visible} workspaceName={state.gatewayWorkspaces.find(item => item.workspace_id === owner.routeId)?.name ?? ''}
      sessionId={owner.sessionId} sessionTitle={state.currentSession?.title ?? owner.title} activeFilePath={filePreview.activePath}
      sessionChangesets={state.sessionChangesets} selectedChangesetId={state.selectedChangesetId} activeChangeset={state.activeChangeset}
      sessionChangesLoading={state.sessionChangesLoading} sessionChangesError={state.sessionChangesError} sessionChangesLoadedAt={state.sessionChangesLoadedAt}
      searchOpen={search} collapseVersion={collapse} settingsVersion={fileTreeSettingsVersion}
      expandedFileTreePaths={expanded} onExpandedFileTreePathsChange={persistExpanded}
      onToggleSearch={() => setSearch(value => !value)} onCollapseAll={() => setCollapse(value => value + 1)}
      onSelectSessionChangeset={id => void content.refreshSessionChanges(owner.sessionId, id)}
      onRefreshSessionChanges={() => void content.refreshSessionChanges(owner.sessionId, state.selectedChangesetId, { refreshList: true })}
      onReviewSessionChangeFile={content.reviewSessionChangeFile}
      onOpenSessionChangeFile={file => {
        const id = state.selectedChangesetId;
        if (!id) { onError('无法打开更改审阅：更改范围尚未加载'); return; }
        void editors.openEditor({ original: { resource: changeFileUri(owner, id, file, 'before') }, modified: { resource: changeFileUri(owner, id, file, 'after') }, label: file.file_path, options: { pinned: true } }).catch((cause: unknown) => onError(`打开更改审阅失败：${String(cause)}`));
      }}
      onOpenFile={filePreview.openWorkspaceFilePreview} onFileOpenMenu={onFileOpenMenu} onStatusChange={reportResourceStatus}
      attachmentPreview={attachment ? <WorkspaceAttachmentPreview attachment={attachment} apiPort={0} workspaceId={owner.routeId} sessionId={owner.sessionId}
        onDirectoryShortcutUpdated={() => setFileTreeSettingsVersion(value => value + 1)} /> : null}
      taskPanel={<TaskView owner={owner} sessionId={owner.sessionId} editors={editors} active={visible && tab === 'goal'} />}
      runtimePreview={preview ? <WorkspaceRuntimePreviewArea tab={preview} onClose={async () => setPreview(null)} /> : null}
      debugPanel={<NodeDebugWorkbench apiPort={0} workspaceId={owner.routeId} sessionId={owner.sessionId}
        threadId={owner.threadId} mainThreadId={state.currentSession?.thread_id ?? null} activeFilePath={null}
        nodeDebugController={debug} sessions={state.currentSession ? [state.currentSession] : []} compact
        onSelectThread={openDebugThread}
        onOpenWorkspacePath={async path => {
          const file = await revealWorkspaceFileEntry(0, path, owner.routeId);
          const root = resourceFileRootUri(file.path);
          if (!root) throw new Error('调试源码定位响应缺少后端文件根 URI');
          await editors.openEditor({ resource: workspaceFileUri(owner, file.path, root) });
        }} onStatusChange={reportResourceStatus} />}
      resourcePanel={<><ResourcePanel inlinePreview resources={resourceItems} loading={state.sessionResourcesLoading} error={state.sessionResourcesError}
        loadedAt={state.sessionResourcesLoadedAt} sessionId={owner.sessionId} workspaceId={owner.routeId} mainThread={thread}
        activePreviewPath={preview?.path ?? null} onRefresh={() => void content.refreshSessionResources(owner.sessionId)}
        onControl={content.controlSessionResource} onOpenTerminalPreview={id => openRuntime('terminal', id)} onOpenBrowserPreview={id => openRuntime('browser', id)}
        onCloseResourcePreview={async () => setPreview(null)}
        onOwnerControl={async (id, action, target) => {
          try { await controlWorkspaceResource(0, owner.routeId, await getWorkspaceResource(0, owner.routeId, id), action, target ?? thread); }
          finally { await content.refreshSessionResources(owner.sessionId, { force: true }); }
        }}
        onCreateConnection={async (kind, target) => {
          if (target.session_id !== owner.sessionId || target.thread_id !== owner.threadId) throw new Error('新建连接的 Thread 归属不一致');
          const created = await createSessionConnection(0, owner.routeId, target, kind);
          const result = await content.refreshSessionResources(owner.sessionId, { force: true });
          if (result !== 'loaded') throw new Error('连接已创建，但资源重读未完成');
          openRuntime(created.kind, created.resourceId);
        }} />
        <ChildThreadPanel {...children} sessionId={owner.sessionId} ownerLabel={state.currentSession?.title ?? owner.title}
          mainThreadId={state.currentSession?.thread_id ?? null} activeThreadId={owner.threadId}
          onRefresh={() => { void children.refresh(); }} conversation={childConversation}
          onForkFromThread={onForkThread}
          onOpenThread={child => { void editors.openEditor(new ThreadInput({ ...owner, threadId: child.thread_id,
            title: child.title ?? child.thread_id }), { pinned: true }).catch((cause: unknown) => onError(`打开子 Thread 失败：${String(cause)}`)); }}
          onSelectThread={openDebugThread} />
      </>} />
    </div>
  </div>;
}
