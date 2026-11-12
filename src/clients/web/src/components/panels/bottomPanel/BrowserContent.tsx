import { t } from "../../../../../shared/core/i18n/index.js";
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
    if (action === "delete" && !await confirm({
      title: t("bottom.browser.deleteTitle"),
      message: t("bottom.browser.deleteMessage", String(resource.metadata?.title ?? resource.resource_id)),
      confirmText: t("bottom.browser.deleteConfirm"),
      danger: true,
    })) return;
    setNotice(null);
    try { await controller.control(resource, action, thread); }
    catch { /* 控制器已显示原始错误并重读完整资源，保留确认前的 revision 以检测并发。 */ }
  };
  const create = async () => {
    if (!currentThread) throw new Error(t("bottom.browser.createNeedsThread"));
    setNotice(null);
    try { onSelect((await controller.create(currentThread, retention)).resource_id); }
    catch { /* 控制器已显示创建错误并重读清单。 */ }
  };

  return <section className="workspace-browser-panel native-workspace-panel-product" aria-label={t("bottom.browser.region")}>
    {renderToolbar(<>
      <span>{t("bottom.browser.count", controller.items.length)}</span>
      <button type="button" disabled={controller.loading} aria-label={t("bottom.browser.refreshWorkspace")} onClick={() => void controller.refresh()}>{t("bottom.common.refresh")}</button>
      <select aria-label={t("bottom.browser.retentionScope")} value={retention} onChange={event => setRetention(event.target.value as "thread" | "workspace")}>
        <option value="workspace">{t("bottom.common.keepForWorkspace")}</option><option value="thread">{t("bottom.common.temporaryBrowser")}</option>
      </select>
      <button type="button" disabled={!currentThread || controller.mutating} onClick={() => void create()}>{t("bottom.browser.create")}</button>
    </>)}
    {controller.error || notice ? <p role="alert" className="resource-notice is-error">{controller.error ?? notice}</p> : null}
    {selectedId && !selected && !controller.loading ? <p role="alert">{t("bottom.browser.selectedMissing", selectedId)}</p> : null}
    {selected?.retention_scope === "thread" && selected.status === "running" && selected.reconciliation_status === "verified" && !preview
      ? <p role="status">{t("bottom.browser.threadOnlyNotice")}</p> : null}
    <div className="workspace-browser-content">
      <div className="resource-tree workspace-browser-list" role="tree" aria-label={t("bottom.browser.list")}>
        {controller.items.map(resource => <ResourceTreeRow key={resource.resource_id} resource={workspaceResourceDisplay(resource)}
          selected={resource.resource_id === selectedId} busy={controller.mutating} previewContext="workspace"
          onControl={(_, id, action) => {
            if (id !== resource.resource_id || (action !== "cancel" && action !== "delete")) throw new Error(t("bottom.browser.actionMismatch"));
            void control(resource, action === "cancel" ? "stop" : "delete");
          }}
          onOwnerControl={(_, action, ref) => { void control(resource, action, action === "attach" ? currentThread ?? undefined : ref); }}
          canAttachCurrent={Boolean(currentThread && resource.available_actions.includes("attach")
            && !resource.thread_associations.some(ref => ref.session_id === currentThread.session_id && ref.thread_id === currentThread.thread_id)
            && (resource.retention_scope === "workspace" || (resource.source_thread?.session_id === currentThread.session_id && resource.source_thread.thread_id === currentThread.thread_id)))}
          onCopy={id => { void copyTextToClipboard(id).catch(cause => setNotice(errorMessage(cause))); }}
          onOpenBrowser={onSelect} onOpenTerminal={() => { throw new Error(t("bottom.browser.cannotOpenTerminal")); }}
          onReplaceBrowser={currentThread ? () => void create() : undefined} />)}
        {!controller.loading && !controller.items.length ? <p>{t("bottom.browser.empty")}</p> : null}
      </div>
      {preview ? <WorkspaceRuntimePreviewArea tab={preview} onClose={async () => onSelect(null)} /> : null}
    </div>
  </section>;
}
