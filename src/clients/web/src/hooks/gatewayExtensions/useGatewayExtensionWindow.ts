import type { Dispatch, SetStateAction } from "react";
import type { WorkspaceAuxiliaryTab } from "../../components/workspace/WorkspaceAuxiliaryPanel";
import type { WorkspaceRuntimePreviewTab } from "../../components/workspace/WorkspaceRuntimePreviewArea";
import { createSessionConnection } from "../../api/gateway/sessionConnections";
import type {
  GatewayExtensionResourceEntry,
  useGatewayExtensionResources,
} from "./useGatewayExtensionResources";
import type { SessionResource } from "../../types/backend";
import type { ResourceThreadRef } from "../../api/workspaceResources";
import { resourcePreviewThread, workspaceResourceDisplay } from "../../state/display/workspaceResourceDisplay";
import { buildGatewayAttachUrl } from "../../utils/attachUrls";
import {
  EXTENSION_WINDOW_NAME,
  buildExtensionWindowUrl,
  type ExtensionResourceKind,
} from "../../utils/extensionResourceWindow";

interface UseGatewayExtensionWindowOptions {
  extensionWindowRequested: boolean;
  extensionWindowFallback: boolean;
  setExtensionWindowFallback: Dispatch<SetStateAction<boolean>>;
  auxiliaryTab: WorkspaceAuxiliaryTab;
  sharedPreviewVisible: boolean;
  activeRuntimePreview: WorkspaceRuntimePreviewTab | null;
  sessionResources: SessionResource[];
  extensionResources: ReturnType<typeof useGatewayExtensionResources>;
  openAuxiliaryTab: (tab: WorkspaceAuxiliaryTab) => void;
  openBrowserPreview: (browserId: string, thread?: ResourceThreadRef) => void;
  openTerminalPreview: (terminalId: string, thread?: ResourceThreadRef) => void;
  apiPort: number;
  activeSessionWorkspaceId: string | null;
  activeSessionId: string | null;
  activeMainThread: ResourceThreadRef | null;
  setStatus: (text: string) => void;
}

/** 扩展窗口模式与 Gateway 扩展资源的编排：可见性、预览标签选择与打开/退出动作。 */
export function useGatewayExtensionWindow({
  extensionWindowRequested,
  extensionWindowFallback,
  setExtensionWindowFallback,
  auxiliaryTab,
  sharedPreviewVisible,
  activeRuntimePreview,
  sessionResources,
  extensionResources,
  openAuxiliaryTab,
  openBrowserPreview,
  openTerminalPreview,
  apiPort,
  activeSessionWorkspaceId,
  activeSessionId,
  activeMainThread,
  setStatus,
}: UseGatewayExtensionWindowOptions) {
  const openExtensionWindow = (kind: ExtensionResourceKind, resourceId?: string) => {
    if (extensionWindowRequested) {
      if (kind === "debug") {
        openAuxiliaryTab("debug");
        return;
      }
      const entry = extensionResources.entries.find(
        (candidate) =>
          candidate.resource.kind === kind &&
          candidate.resource.resource_id === resourceId,
      );
      if (entry) {
        extensionResources.select(entry.key);
      }
      return;
    }

    const url = buildExtensionWindowUrl({
      kind,
      resourceId,
      workspaceId: activeSessionWorkspaceId,
      sessionId: activeSessionId,
    });
    const extensionWindow = window.open(url, EXTENSION_WINDOW_NAME);
    if (!extensionWindow) {
      setExtensionWindowFallback(true);
      openAuxiliaryTab(kind === "debug" ? "debug" : "resources");
      if (kind === "browser") {
        const resource = sessionResources.find((candidate) => candidate.kind === "browser" && candidate.resource_id === resourceId);
        if (!resource) throw new Error("浏览器不在当前会话的权威关联清单中");
        openBrowserPreview(resource.resource_id, resourcePreviewThread(resource, activeSessionId ?? ""));
      } else if (kind === "terminal") {
        const resource = sessionResources.find((candidate) => candidate.kind === "terminal" && candidate.resource_id === resourceId);
        if (!resource) throw new Error("终端不在当前会话的权威关联清单中");
        openTerminalPreview(resource.resource_id, resourcePreviewThread(resource, activeSessionId ?? ""));
      }
      setStatus("扩展窗口未能打开，已在当前页面切换为扩展窗口模式；请检查浏览器弹窗权限。");
      return;
    }
    extensionWindow.focus();
    setStatus("已打开扩展窗口；后续扩展内容将在此窗口内切换。");
  };
  const openExtensionResource = (entry: GatewayExtensionResourceEntry) => {
    extensionResources.select(entry.key);
    setStatus(
      `已切换到 ${entry.gateway_name} · ${entry.workspace_name} · ${entry.session_title}`,
    );
  };
  const createExtensionReplacement = async (entry: GatewayExtensionResourceEntry) => {
    if (
      !activeSessionId || activeSessionWorkspaceId !== entry.workspace_id
      || activeMainThread?.session_id !== activeSessionId
    ) throw new Error("新建浏览器需要先解析同工作区当前会话的主线程");
    const created = await createSessionConnection(
      apiPort,
      entry.workspace_id,
      activeMainThread,
      "browser",
    );
    await extensionResources.refresh();
    setStatus(`已新建浏览器：${created.resourceId}`);
  };
  const selectedExtensionEntry = extensionResources.selectedEntry;
  const extensionPreviewEntry = selectedExtensionEntry &&
    (selectedExtensionEntry.resource.kind === "browser" ||
      selectedExtensionEntry.resource.kind === "terminal")
    ? selectedExtensionEntry
    : null;
  const extensionCanOpen = extensionPreviewEntry?.resource.status === "running"
    && extensionPreviewEntry.resource.reconciliation_status === "verified"
    && (extensionPreviewEntry.resource.retention_scope === "workspace" || extensionPreviewEntry.resource.thread_associations.length > 0);
  const extensionPreviewResource = extensionPreviewEntry && extensionCanOpen
    ? workspaceResourceDisplay(extensionPreviewEntry.resource)
    : null;
  const extensionPreviewTab: WorkspaceRuntimePreviewTab | null = extensionPreviewEntry && extensionPreviewResource
    ? extensionPreviewEntry.resource.kind === "browser"
      ? {
          previewType: "browser",
          path: `gateway-resource://${extensionPreviewEntry.key}`,
          name: extensionPreviewResource.name,
          scopeLabel: `${extensionPreviewEntry.gateway_name} · ${extensionPreviewEntry.workspace_name} · ${extensionPreviewEntry.session_title}`,
          browserId: extensionPreviewEntry.resource.resource_id,
          attachUrl: buildGatewayAttachUrl(
            "browser",
            extensionPreviewEntry.workspace_id,
            extensionPreviewEntry.resource.resource_id,
            true,
            resourcePreviewThread(extensionPreviewResource, extensionPreviewEntry.session_id),
          ),
        }
      : {
          previewType: "terminal",
          path: `gateway-resource://${extensionPreviewEntry.key}`,
          name: extensionPreviewResource.name,
          scopeLabel: `${extensionPreviewEntry.gateway_name} · ${extensionPreviewEntry.workspace_name} · ${extensionPreviewEntry.session_title}`,
          terminalId: extensionPreviewEntry.resource.resource_id,
          attachUrl: buildGatewayAttachUrl(
            "terminal",
            extensionPreviewEntry.workspace_id,
            extensionPreviewEntry.resource.resource_id,
            true,
            resourcePreviewThread(extensionPreviewResource, extensionPreviewEntry.session_id),
          ),
        }
    : null;
  const handleExitExtensionWindow = () => {
    if (extensionWindowRequested) {
      if (window.opener && !window.opener.closed) {
        window.close();
        return;
      }
      const standardUrl = new URL(window.location.href);
      standardUrl.pathname = "/";
      standardUrl.search = "";
      standardUrl.hash = "";
      window.location.assign(standardUrl.toString());
      return;
    }
    setExtensionWindowFallback(false);
  };
  const extensionWindowVisible = extensionWindowRequested || extensionWindowFallback;
  const extensionDebugSplitActive = extensionWindowVisible &&
    auxiliaryTab === "debug" &&
    sharedPreviewVisible;
  const activeRuntimePreviewResource = activeRuntimePreview
    ? sessionResources.find((resource) =>
        resource.resource_id === (
          activeRuntimePreview.previewType === "browser"
            ? activeRuntimePreview.browserId
            : activeRuntimePreview.terminalId
        )
        && resource.kind === activeRuntimePreview.previewType
        && resource.status === "running",
      ) ?? null
    : null;
  const runtimePreviewTab = extensionWindowRequested
    ? extensionPreviewTab
    : extensionWindowFallback && activeRuntimePreviewResource
      ? activeRuntimePreview
      : null;

  return {
    openExtensionWindow,
    openExtensionResource,
    createExtensionReplacement,
    handleExitExtensionWindow,
    extensionWindowVisible,
    extensionDebugSplitActive,
    runtimePreviewTab,
  };
}
