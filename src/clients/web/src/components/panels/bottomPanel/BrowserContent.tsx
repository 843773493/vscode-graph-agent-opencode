import { useMemo, useState, type ReactNode } from "react";
import type { ResourceAction, ResourceThreadRef, WorkspaceResourceDTO } from "../../../api/workspaceResources";
import type { WorkspaceResourcesController } from "../../../hooks/workspace/useWorkspaceResources";
import { workspaceResourceDisplay } from "../../../state/display/workspaceResourceDisplay";
import { buildGatewayAttachUrl } from "../../../utils/attachUrls";
import { copyTextToClipboard } from "../../../utils/clipboard";
import { errorMessage } from "../../../utils/errorMessage";
import ResourceTreeRow from "../resourceTree/ResourceTreeRow";
import WorkspaceRuntimePreviewArea from "../../workspace/WorkspaceRuntimePreviewArea";
import { useWarmConfirm } from "../../shell/WarmConfirmProvider";

/** 工作区 Browser 入口；资源行、预览与权威控制器均和现有产品共用。 */
export function BrowserContent({ controller, workspaceId, currentThread, selectedId, onSelect, renderToolbar }: {
  controller: WorkspaceResourcesController;
  workspaceId: string;
  currentThread: ResourceThreadRef | null;
  selectedId: string | null;
  onSelect: (resourceId: string | null) => void;
  renderToolbar: (tools: ReactNode) => ReactNode;
}) {
  const confirm = useWarmConfirm();
  const [notice, setNotice] = useState<string | null>(null);
  const [retention, setRetention] = useState<"thread" | "workspace">("workspace");
  const selected = controller.items.find(item => item.resource_id === selectedId);
  const preview = useMemo(() => {
    if (!selected || selected.status !== "running" || selected.reconciliation_status !== "verified") return null;
    const thread = selected.retention_scope === "workspace" ? undefined
      : selected.thread_associations.find(ref => ref.session_id === currentThread?.session_id && ref.thread_id === currentThread.thread_id);
    if (selected.retention_scope === "thread" && !thread) return null;
    return { previewType: "browser" as const, browserId: selected.resource_id, path: `browser://${selected.resource_id}`,
      name: String(selected.metadata?.title ?? selected.resource_id),
      attachUrl: buildGatewayAttachUrl("browser", workspaceId, selected.resource_id, true, thread) };
  }, [selected, workspaceId, currentThread]);

  const control = async (resource: WorkspaceResourceDTO, action: ResourceAction, thread?: ResourceThreadRef) => {
    if (action === "delete" && !await confirm({ title: "删除浏览器", message: `确认删除 ${String(resource.metadata?.title ?? resource.resource_id)}？删除后只保留历史记录。`, confirmText: "删除", danger: true })) return;
    setNotice(null);
    try { await controller.control(resource, action, thread); }
    catch { /* 控制器已显示原始错误并重读完整资源，保留确认前的 revision 以检测并发。 */ }
  };
  const create = async () => {
    if (!currentThread) throw new Error("新建浏览器需要明确的会话与 Thread");
    setNotice(null);
    try { onSelect((await controller.create(currentThread, retention)).resource_id); }
    catch { /* 控制器已显示创建错误并重读清单。 */ }
  };

  return <section className="workspace-browser-panel native-workspace-panel-product" aria-label="工作区浏览器">
    {renderToolbar(<>
      <span>{controller.items.length} 个浏览器</span>
      <button type="button" disabled={controller.loading} aria-label="刷新工作区浏览器" onClick={() => void controller.refresh()}>刷新</button>
      <select aria-label="浏览器保留范围" value={retention} onChange={event => setRetention(event.target.value as "thread" | "workspace")}>
        <option value="workspace">工作区保留</option><option value="thread">临时浏览器</option>
      </select>
      <button type="button" disabled={!currentThread || controller.mutating} onClick={() => void create()}>新建浏览器</button>
    </>)}
    {controller.error || notice ? <p role="alert" className="resource-notice is-error">{controller.error ?? notice}</p> : null}
    {selectedId && !selected && !controller.loading ? <p role="alert">所选浏览器已不存在：{selectedId}</p> : null}
    {selected?.retention_scope === "thread" && selected.status === "running" && selected.reconciliation_status === "verified" && !preview
      ? <p role="status">临时浏览器仅可在已关联的会话线程中连接；选择资源不会自动关联或启动它。</p> : null}
    <div className="workspace-browser-content">
      <div className="resource-tree workspace-browser-list" role="tree" aria-label="当前工作区浏览器列表">
        {controller.items.map(resource => <ResourceTreeRow key={resource.resource_id} resource={workspaceResourceDisplay(resource)}
          selected={resource.resource_id === selectedId} busy={controller.mutating} previewContext="workspace"
          onControl={(_, id, action) => {
            if (id !== resource.resource_id || (action !== "cancel" && action !== "delete")) throw new Error("工作区 Browser 操作不匹配");
            void control(resource, action === "cancel" ? "stop" : "delete");
          }}
          onOwnerControl={(_, action, ref) => { void control(resource, action, action === "attach" ? currentThread ?? undefined : ref); }}
          canAttachCurrent={Boolean(currentThread && resource.available_actions.includes("attach")
            && !resource.thread_associations.some(ref => ref.session_id === currentThread.session_id && ref.thread_id === currentThread.thread_id)
            && (resource.retention_scope === "workspace" || (resource.source_thread?.session_id === currentThread.session_id && resource.source_thread.thread_id === currentThread.thread_id)))}
          onCopy={id => { void copyTextToClipboard(id).catch(cause => setNotice(errorMessage(cause))); }}
          onOpenBrowser={onSelect} onOpenTerminal={() => { throw new Error("Browser 面板不能打开终端"); }}
          onReplaceBrowser={currentThread ? () => void create() : undefined} />)}
        {!controller.loading && !controller.items.length ? <p>当前工作区还没有浏览器。</p> : null}
      </div>
      {preview ? <WorkspaceRuntimePreviewArea tab={preview} onClose={async () => onSelect(null)} /> : null}
    </div>
  </section>;
}
